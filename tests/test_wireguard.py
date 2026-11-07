"""WireGuard 组网单元测试：密钥、helper 配置、状态解析、配置卡与热应用分类。

本文件只覆盖纯逻辑（不启动子进程、不依赖真实 AstrBot）；helper 生命周期用 fake
helper 的用例见 ``tests/test_wireguard_lifecycle.py``。
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from core.config import PhoneConfig
from core.config_apply import (
    ACTION_NONE,
    ACTION_RESTART,
    ACTION_START,
    ACTION_STOP,
    RECONCILE_KEYS,
    build_apply_plan,
    config_field_paths,
)
from core.wireguard import (
    ADB_OVER_WG_TARGET_PORT,
    DEFAULT_ADB_LOCAL_PORT,
    DEFAULT_LISTEN_PORT,
    PERSISTENT_KEEPALIVE_SECONDS,
    STATUS_MAX_AGE_SECONDS,
    WireGuardConfigError,
    WireGuardKeyStore,
    WireGuardManager,
    WireGuardSettings,
    build_helper_config,
    clamp_private_key,
    decode_key,
    evaluate_health,
    generate_private_key,
    is_valid_key,
    load_status_file,
    parse_endpoint,
    public_key_from_private,
    status_age_seconds,
    summarize_forwards,
    summarize_peers,
    _x25519_rfc7748,
    _BASE_POINT,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# RFC 7748 6.1 测试向量（X25519 公钥推导正确性的已知答案）。
_RFC7748_ALICE_PRIVATE_HEX = (
    "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
)
_RFC7748_ALICE_PUBLIC_HEX = (
    "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
)
_RFC7748_BOB_PRIVATE_HEX = (
    "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb"
)
_RFC7748_BOB_PUBLIC_HEX = (
    "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
)

SERVER_PRIVATE = generate_private_key()
SERVER_PUBLIC = public_key_from_private(SERVER_PRIVATE)


def _settings(**overrides) -> WireGuardSettings:
    base = dict(
        enabled=True,
        mode="client",
        listen_port=DEFAULT_LISTEN_PORT,
        interface_address="10.66.0.2/24",
        server_endpoint="192.168.250.50:51820",
        public_endpoint="",
        server_public_key=SERVER_PUBLIC,
        preshared_key="",
        phone_wg_ip="10.66.0.3",
        adb_local_port=DEFAULT_ADB_LOCAL_PORT,
        auto_adb=False,
    )
    base.update(overrides)
    return WireGuardSettings(**base)


def _keys(tmp_path: Path) -> dict[str, dict[str, str]]:
    return WireGuardKeyStore(tmp_path / "keys.json").ensure_all()


# ----------------------------------------------------------------------
# X25519 密钥
# ----------------------------------------------------------------------


def test_clamp_private_key_wireguard_bits() -> None:
    raw = bytes(range(32))
    clamped = clamp_private_key(raw)
    assert len(clamped) == 32
    assert clamped[0] & 0b111 == 0
    assert clamped[31] & 0b1000_0000 == 0
    assert clamped[31] & 0b0100_0000 == 0b0100_0000
    # 输入不被原地修改
    assert raw[0] == 0 and raw[31] == 31


def test_clamp_private_key_rejects_wrong_length() -> None:
    with pytest.raises(WireGuardConfigError):
        clamp_private_key(b"x" * 31)


def test_rfc7748_reference_implementation_vectors() -> None:
    """纯 Python 备用实现必须与 RFC 7748 已知公钥一致。"""
    alice_private = bytes.fromhex(_RFC7748_ALICE_PRIVATE_HEX)
    bob_private = bytes.fromhex(_RFC7748_BOB_PRIVATE_HEX)
    alice_public = _x25519_rfc7748(alice_private, _BASE_POINT)
    bob_public = _x25519_rfc7748(bob_private, _BASE_POINT)
    assert alice_public.hex() == _RFC7748_ALICE_PUBLIC_HEX
    assert bob_public.hex() == _RFC7748_BOB_PUBLIC_HEX
    # 双方共享密钥一致（X25519 交换性质）
    shared_a = _x25519_rfc7748(alice_private, bob_public)
    shared_b = _x25519_rfc7748(bob_private, alice_public)
    assert shared_a == shared_b


def test_cryptography_x25519_matches_rfc7748_when_available() -> None:
    """AstrBot 容器自带 cryptography：优先路径必须与 RFC 7748 向量一致。"""
    pytest.importorskip("cryptography")
    from core.wireguard import _x25519_cryptography

    scalar = clamp_private_key(bytes.fromhex(_RFC7748_ALICE_PRIVATE_HEX))
    assert _x25519_cryptography(scalar).hex() == _RFC7748_ALICE_PUBLIC_HEX


def test_public_key_from_private_matches_rfc7748_vector() -> None:
    private_b64 = base64.b64encode(
        bytes.fromhex(_RFC7748_ALICE_PRIVATE_HEX)
    ).decode("ascii")
    expected = base64.b64encode(
        bytes.fromhex(_RFC7748_ALICE_PUBLIC_HEX)
    ).decode("ascii")
    assert public_key_from_private(private_b64) == expected


def test_generate_private_key_is_clamped_and_base64() -> None:
    key = generate_private_key()
    raw = decode_key(key)
    assert len(raw) == 32
    assert raw == clamp_private_key(raw)
    public = public_key_from_private(key)
    assert is_valid_key(public)
    assert public_key_from_private(key) == public  # 推导稳定


def test_decode_key_rejects_invalid_values() -> None:
    for bad in ("", "not-base64!!", base64.b64encode(b"x" * 31).decode()):
        with pytest.raises(WireGuardConfigError):
            decode_key(bad)
    # 缺省填充可被容忍
    raw = bytes(range(32))
    unpadded = base64.b64encode(raw).decode().rstrip("=")
    assert decode_key(unpadded) == raw


# ----------------------------------------------------------------------
# 密钥持久化
# ----------------------------------------------------------------------


def test_key_store_generates_three_roles_with_0600(tmp_path: Path) -> None:
    store = WireGuardKeyStore(tmp_path / "wireguard" / "keys.json")
    keys = store.ensure_all()
    assert set(keys) == {"client", "server", "phone"}
    for role, entry in keys.items():
        assert is_valid_key(entry["private_key"]), role
        assert public_key_from_private(entry["private_key"]) == entry["public_key"]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["keys"]) == {"client", "server", "phone"}


def test_key_store_is_stable_across_reloads(tmp_path: Path) -> None:
    store = WireGuardKeyStore(tmp_path / "keys.json")
    first = store.ensure_all()
    second = WireGuardKeyStore(tmp_path / "keys.json").ensure_all()
    assert first == second


def test_key_store_recovers_corrupt_file(tmp_path: Path) -> None:
    store = WireGuardKeyStore(tmp_path / "keys.json")
    store.path.write_text("{not json", encoding="utf-8")
    keys = store.ensure_all()
    assert set(keys) == {"client", "server", "phone"}
    backups = list(tmp_path.glob("keys.json.corrupt-*"))
    assert backups, "损坏的密钥文件应改名保留"
    assert backups[0].read_text(encoding="utf-8") == "{not json"


def test_key_store_repairs_tampered_public_key(tmp_path: Path) -> None:
    store = WireGuardKeyStore(tmp_path / "keys.json")
    keys = store.ensure_all()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["keys"]["client"]["public_key"] = base64.b64encode(b"x" * 32).decode()
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    repaired = store.ensure_all()
    assert repaired["client"]["public_key"] == keys["client"]["public_key"]
    assert repaired["client"]["private_key"] == keys["client"]["private_key"]


# ----------------------------------------------------------------------
# 配置卡解析（core.config）
# ----------------------------------------------------------------------


def test_wireguard_config_defaults_client_and_server() -> None:
    client = PhoneConfig.from_mapping(None).wireguard
    assert client.enabled is False
    assert client.mode == "client"
    assert client.interface_address == "10.66.0.2/24"
    assert client.phone_wg_ip == "10.66.0.3"
    assert client.listen_port == DEFAULT_LISTEN_PORT
    assert client.adb_local_port == DEFAULT_ADB_LOCAL_PORT
    assert client.auto_adb is False
    assert client.server_endpoint == ""
    assert client.public_endpoint == ""

    server = PhoneConfig.from_mapping({"wireguard": {"mode": "server"}}).wireguard
    assert server.mode == "server"
    assert server.interface_address == "10.66.0.1/24"
    assert server.phone_wg_ip == "10.66.0.2"


def test_wireguard_config_parses_explicit_values() -> None:
    config = PhoneConfig.from_mapping(
        {
            "wireguard": {
                "enabled": True,
                "mode": "server",
                "listen_port": 51821,
                "interface_address": "10.90.0.1/24",
                "public_endpoint": "192.168.250.50:51821",
                "preshared_key": SERVER_PUBLIC,
                "phone_wg_ip": "10.90.0.2",
                "adb_local_port": 16666,
                "auto_adb": True,
            }
        }
    )
    wg = config.wireguard
    assert wg.enabled is True
    assert wg.mode == "server"
    assert wg.listen_port == 51821
    assert wg.interface_address == "10.90.0.1/24"
    assert wg.public_endpoint == "192.168.250.50:51821"
    assert wg.phone_wg_ip == "10.90.0.2"
    assert wg.adb_local_port == 16666
    assert wg.auto_adb is True


def test_wireguard_config_clamps_invalid_values() -> None:
    warnings: list[str] = []
    config = PhoneConfig.from_mapping(
        {
            "wireguard": {
                "enabled": "not-bool",
                "mode": "peer",
                "listen_port": 70000,
                "adb_local_port": -3,
            }
        },
        warn=warnings.append,
    )
    wg = config.wireguard
    assert wg.enabled is False
    assert wg.mode == "client"
    assert wg.listen_port == 65535
    assert wg.adb_local_port == 0
    assert warnings


# ----------------------------------------------------------------------
# helper 配置 JSON
# ----------------------------------------------------------------------


def test_build_helper_config_client(tmp_path: Path) -> None:
    settings = _settings()
    config = build_helper_config(settings, _keys(tmp_path))
    assert config["log_level"] == "info"
    interface = config["interface"]
    assert interface["addresses"] == ["10.66.0.2/24"]
    assert interface["listen_port"] == 0
    assert interface["mtu"] == 1420
    assert is_valid_key(interface["private_key"])
    peer = config["peers"][0]
    assert peer["public_key"] == SERVER_PUBLIC
    assert peer["endpoint"] == "192.168.250.50:51820"
    assert peer["allowed_ips"] == ["10.66.0.0/24"]
    assert peer["preshared_key"] == ""
    assert peer["persistent_keepalive_seconds"] == PERSISTENT_KEEPALIVE_SECONDS
    assert config["forwards"] == [
        {
            "listen": "127.0.0.1:15555",
            "target": f"10.66.0.3:{ADB_OVER_WG_TARGET_PORT}",
        }
    ]


def test_build_helper_config_server(tmp_path: Path) -> None:
    keys = _keys(tmp_path)
    settings = _settings(
        mode="server",
        listen_port=51821,
        interface_address="10.66.0.1/24",
        phone_wg_ip="10.66.0.2",
        public_endpoint="192.168.250.50:51821",
        server_endpoint="",
        server_public_key="",
        auto_adb=True,
    )
    config = build_helper_config(settings, keys)
    interface = config["interface"]
    assert interface["addresses"] == ["10.66.0.1/24"]
    assert interface["listen_port"] == 51821
    peer = config["peers"][0]
    assert peer["public_key"] == keys["phone"]["public_key"]
    assert peer["endpoint"] == ""
    assert peer["allowed_ips"] == ["10.66.0.2/32"]
    assert peer["persistent_keepalive_seconds"] == 0
    assert config["forwards"][0]["target"] == f"10.66.0.2:{ADB_OVER_WG_TARGET_PORT}"


def test_build_helper_config_preshared_key_and_disabled_forward(tmp_path: Path) -> None:
    settings = _settings(preshared_key=SERVER_PUBLIC, adb_local_port=0)
    config = build_helper_config(settings, _keys(tmp_path))
    assert config["peers"][0]["preshared_key"] == SERVER_PUBLIC
    assert config["forwards"] == []


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"server_endpoint": ""}, "server_endpoint"),
        ({"server_endpoint": "192.168.250.50"}, "端口"),
        ({"server_endpoint": "wg.example.com:51820"}, "必须是 IP 地址"),
        ({"server_public_key": ""}, "server_public_key"),
        ({"server_public_key": "bad"}, "server_public_key"),
        ({"preshared_key": "bad"}, "preshared_key"),
        ({"phone_wg_ip": "10.99.0.3"}, "不在 interface_address 网段"),
        ({"phone_wg_ip": "not-an-ip"}, "phone_wg_ip"),
        ({"interface_address": "10.66.0.2"}, "网段"),
        ({"interface_address": "fd00::2/64"}, "IPv4"),
        ({"adb_local_port": -1}, "adb_local_port"),
    ],
)
def test_build_helper_config_client_validation_errors(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    with pytest.raises(WireGuardConfigError) as info:
        build_helper_config(_settings(**overrides), _keys(tmp_path))
    assert message in str(info.value)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"public_endpoint": ""}, "public_endpoint"),
        ({"listen_port": 0}, "listen_port"),
    ],
)
def test_build_helper_config_server_validation_errors(
    tmp_path: Path, overrides: dict, message: str
) -> None:
    base = dict(
        mode="server",
        server_endpoint="",
        server_public_key="",
        public_endpoint="192.168.250.50:51820",
    )
    base.update(overrides)
    settings = _settings(**base)
    with pytest.raises(WireGuardConfigError) as info:
        build_helper_config(settings, _keys(tmp_path))
    assert message in str(info.value)


# ----------------------------------------------------------------------
# 状态文件解析与健康判定
# ----------------------------------------------------------------------


SAMPLE_STATUS = {
    "running": True,
    "version": "0.1.0",
    "interface": {
        "listen_port": 51820,
        "addresses": ["10.66.0.1/24"],
        "public_key": "interface-public-key",
    },
    "peers": [
        {
            "public_key": "peer-a",
            "endpoint": "1.2.3.4:51820",
            "allowed_ips": ["10.66.0.2/32"],
            "last_handshake_unix": 1792510000,
            "rx_bytes": 1234,
            "tx_bytes": 5678,
        }
    ],
    "forwards": [
        {
            "listen": "127.0.0.1:15555",
            "target": "10.66.0.2:5555",
            "active_connections": 1,
            "total_connections": 7,
            "last_error": "",
        }
    ],
    "updated_at_unix": 1792510001,
}


def test_load_status_file_valid_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    path.write_text(json.dumps(SAMPLE_STATUS), encoding="utf-8")
    status, error = load_status_file(path)
    assert error == ""
    assert status == SAMPLE_STATUS

    missing = load_status_file(tmp_path / "missing.json")
    assert missing == (None, "状态文件不存在")
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert load_status_file(empty) == (None, "状态文件为空")
    broken = tmp_path / "broken.json"
    broken.write_text("{bad json", encoding="utf-8")
    status, error = load_status_file(broken)
    assert status is None and "不是合法 JSON" in error
    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    assert load_status_file(array) == (None, "状态文件结构不是 JSON 对象")


def test_status_age_uses_updated_at_or_mtime(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    status = {"updated_at_unix": 1000}
    assert status_age_seconds(status, path, now=1010) == 10
    # updated_at 缺失时退回文件 mtime
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (500, 500))
    assert status_age_seconds({}, path, now=530) == 30
    assert status_age_seconds({"updated_at_unix": "bad"}, path, now=530) == 30
    assert status_age_seconds({}, tmp_path / "missing.json", now=530) is None


def test_evaluate_health_cases() -> None:
    fresh = {"running": True, "updated_at_unix": 1}
    assert evaluate_health(running=True, status=fresh, status_age=1)["ok"] is True
    not_running = evaluate_health(running=False, status=fresh, status_age=1)
    assert not_running["ok"] is False and "未运行" in not_running["reason"]
    missing = evaluate_health(
        running=True, status=None, status_error="状态文件不存在", status_age=None
    )
    assert missing["ok"] is False and missing["reason"] == "状态文件不存在"
    reported_stopped = evaluate_health(
        running=True, status={"running": False, "updated_at_unix": 1}, status_age=1
    )
    assert reported_stopped["ok"] is False
    stale = evaluate_health(
        running=True, status=fresh, status_age=STATUS_MAX_AGE_SECONDS + 1
    )
    assert stale["ok"] is False and "过期" in stale["reason"]
    no_time = evaluate_health(running=True, status=fresh, status_age=None)
    assert no_time["ok"] is False and "更新时间" in no_time["reason"]


def test_summarize_status_helpers() -> None:
    peers = summarize_peers(SAMPLE_STATUS)
    assert peers["count"] == 1
    assert peers["last_handshake_unix"] == 1792510000
    assert peers["rx_bytes"] == 1234
    assert peers["tx_bytes"] == 5678
    forwards = summarize_forwards(SAMPLE_STATUS)
    assert forwards and forwards[0]["active_connections"] == 1
    assert summarize_peers(None)["count"] == 0
    assert summarize_forwards(None) == []


# ----------------------------------------------------------------------
# 状态展示与导入用配置文本
# ----------------------------------------------------------------------


def _manager(tmp_path: Path, settings: WireGuardSettings) -> WireGuardManager:
    return WireGuardManager(
        data_dir=tmp_path / "data", settings_provider=lambda: settings
    )


def test_config_text_client_shows_server_peer_snippet(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _settings())
    text = manager.config_text()
    assert "服务器侧" in text
    client_public = manager.keys.ensure_all()["client"]["public_key"]
    assert f"PublicKey = {client_public}" in text
    assert "AllowedIPs = 10.66.0.2/32" in text
    assert "192.168.250.50:51820" in text


def test_config_text_server_shows_phone_ini(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        _settings(
            mode="server",
            listen_port=51821,
            interface_address="10.66.0.1/24",
            phone_wg_ip="10.66.0.2",
            public_endpoint="192.168.250.50:51821",
            server_endpoint="",
            server_public_key="",
        ),
    )
    text = manager.config_text()
    keys = manager.keys.ensure_all()
    assert "[Interface]" in text
    assert f"PrivateKey = {keys['phone']['private_key']}" in text
    assert "Address = 10.66.0.2/24" in text
    assert f"PublicKey = {keys['server']['public_key']}" in text
    assert "Endpoint = 192.168.250.50:51821" in text
    assert "AllowedIPs = 10.66.0.0/24" in text
    assert f"PersistentKeepalive = {PERSISTENT_KEEPALIVE_SECONDS}" in text


def test_config_text_server_host_only_public_endpoint_uses_listen_port(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path,
        _settings(
            mode="server",
            listen_port=51822,
            interface_address="10.66.0.1/24",
            phone_wg_ip="10.66.0.2",
            public_endpoint="wg.example.com",
            server_endpoint="",
            server_public_key="",
        ),
    )
    assert "Endpoint = wg.example.com:51822" in manager.config_text()


def test_status_text_and_brief_when_disabled(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _settings(enabled=False))
    assert "已关闭" in manager.status_text()
    assert "未启用" in manager.brief_text()
    status = manager.status()
    assert status["running"] is False
    assert status["enabled"] is False


def test_parse_endpoint_variants() -> None:
    assert parse_endpoint("192.168.250.50:51820") == ("192.168.250.50", 51820)
    assert parse_endpoint("wg.example.com") == ("wg.example.com", 0)
    assert parse_endpoint("[fd00::1]:51820") == ("fd00::1", 51820)
    assert parse_endpoint("") == ("", 0)
    with pytest.raises(WireGuardConfigError):
        parse_endpoint("192.168.250.50", required_port=True)
    with pytest.raises(WireGuardConfigError):
        parse_endpoint("192.168.250.50:70000")
    with pytest.raises(WireGuardConfigError):
        parse_endpoint("bad host:51820")


# ----------------------------------------------------------------------
# 配置热应用分类
# ----------------------------------------------------------------------


def test_wireguard_fields_are_reconcile_classified() -> None:
    wireguard_paths = {
        path for path in config_field_paths() if path.startswith("wireguard.")
    }
    assert wireguard_paths
    assert wireguard_paths <= RECONCILE_KEYS


def _wireguard_config(**overrides) -> PhoneConfig:
    return PhoneConfig.from_mapping({"wireguard": overrides})


def test_wireguard_enable_and_disable_actions() -> None:
    enable = build_apply_plan(_wireguard_config(), _wireguard_config(enabled=True))
    assert enable.wireguard_changed is True
    assert enable.wireguard_action == ACTION_START
    assert enable.changed is True
    assert any("WireGuard" in note for note in enable.notes)

    disable = build_apply_plan(_wireguard_config(enabled=True), _wireguard_config())
    assert disable.wireguard_action == ACTION_STOP
    assert any("WireGuard" in note for note in disable.notes)


def test_wireguard_settings_change_while_enabled_restarts() -> None:
    previous = _wireguard_config(
        enabled=True, server_endpoint="192.168.250.50:51820"
    )
    current = _wireguard_config(
        enabled=True, server_endpoint="192.168.250.51:51820"
    )
    plan = build_apply_plan(previous, current)
    assert plan.wireguard_changed is True
    assert plan.wireguard_action == ACTION_RESTART
    assert any("重启" in note for note in plan.notes)


def test_wireguard_change_while_disabled_has_no_runtime_action() -> None:
    plan = build_apply_plan(
        _wireguard_config(server_endpoint="a:1"),
        _wireguard_config(server_endpoint="b:2"),
    )
    assert plan.wireguard_changed is True
    assert plan.wireguard_action == ACTION_NONE
    assert any("未启用" in note for note in plan.notes)


def test_wireguard_unchanged_has_no_action() -> None:
    config = _wireguard_config(enabled=True)
    plan = build_apply_plan(config, config)
    assert plan.wireguard_changed is False
    assert plan.wireguard_action == ACTION_NONE


# ----------------------------------------------------------------------
# 配置 schema 与接线事实
# ----------------------------------------------------------------------


def test_conf_schema_has_network_with_wireguard_between_control_and_verification() -> None:
    schema = json.loads(
        (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    keys = list(schema)
    assert "network" in keys
    assert keys.index("control") < keys.index("network") < keys.index("verification")
    network = schema["network"]["items"]
    assert network["enabled"]["default"] is False
    card = network["wireguard"]
    assert card["condition"] == {"enabled": True, "provider": "wireguard"}
    assert card["type"] == "object"
    items = card["items"]
    assert items["mode"]["default"] == "client"
    assert items["mode"]["options"] == ["client", "server"]
    assert items["listen_port"]["default"] == DEFAULT_LISTEN_PORT
    assert items["adb_local_port"]["default"] == DEFAULT_ADB_LOCAL_PORT
    assert items["auto_adb"]["default"] is False
    for name in (
        "mode",
        "listen_port",
        "interface_address",
        "server_endpoint",
        "public_endpoint",
        "server_public_key",
        "preshared_key",
        "phone_wg_ip",
        "adb_local_port",
        "auto_adb",
    ):
        assert name in items, name
        assert items[name].get("description")
        assert items[name].get("hint")
    assert "client" in items["server_endpoint"]["hint"]
    assert "server" in items["public_endpoint"]["hint"]
    # client/server 配置项按角色条件显隐（AstrBot schema condition 为同级 flat key 严格相等）
    assert items["server_endpoint"]["condition"] == {
        "mode": "client",
    }
    assert items["server_public_key"]["condition"] == {
        "mode": "client",
    }
    assert items["listen_port"]["condition"] == {"mode": "server"}
    assert items["public_endpoint"]["condition"] == {"mode": "server"}
    # 未开启 WireGuard 时其余字段全部隐藏。
    for name in ("mode", "preshared_key", "interface_address", "phone_wg_ip"):
        assert "condition" not in items[name]
    # 展示顺序：先角色，再按 client/server 分组，最后通用项
    names = list(items)
    assert names.index("mode") < names.index("server_endpoint") < names.index("listen_port")
    assert names.index("public_endpoint") < names.index("preshared_key")


def test_tool_registry_and_help_wiring() -> None:
    registry_text = (PLUGIN_ROOT / "core" / "tools" / "registry.py").read_text(
        encoding="utf-8"
    )
    assert '"phone_wireguard"' in registry_text
    assert "PhoneWireguardTool(service=service)" in registry_text
    tools_init = (PLUGIN_ROOT / "core" / "tools" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "PhoneWireguardTool" in tools_init
    shared_text = (PLUGIN_ROOT / "core" / "plugin" / "shared.py").read_text(
        encoding="utf-8"
    )
    from core.tools.registry import TOOL_NAMES
    assert "phone_task_template" in TOOL_NAMES
    assert f"{len(TOOL_NAMES)} 个 phone_* 工具" in shared_text
    assert '"phone_files"' in registry_text and '"phone_record_media"' in registry_text
    assert "/phone wg [status|start|stop|restart|config]" in shared_text
    main_text = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    assert 'frozenset({"stop", "verify", "wg"})' in main_text
