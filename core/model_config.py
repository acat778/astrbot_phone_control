"""模型配置归组、持久化迁移与原生配置下拉选项。"""

import copy
import json
from pathlib import Path

from .config_storage import _atomic_write, _read


MODEL_SOURCES = {
    "asr_provider_id": (("media", "asr_provider_id"),),
    "vision_provider_id": (("media", "vision_provider_id"), ("perception", "ocr_provider_id")),
    "app_memory_embedding_provider_id": (("prompt", "app_memory_embedding_provider_id"),),
}


def migrate_models(raw):
    """集中模型选择，明确选择优先；空选择继承已配置的模型。"""
    before = copy.deepcopy(dict(raw))
    model = raw.setdefault("model", {})
    if not isinstance(model, dict):
        return False
    for key, sources in MODEL_SOURCES.items():
        for group, field in sources:
            card = raw.get(group)
            if isinstance(card, dict):
                value = card.pop(field, "")
                if not str(model.get(key) or "").strip() and str(value or "").strip():
                    model[key] = value
    memory = raw.setdefault("memory", {})
    if isinstance(memory, dict):
        for group in ("model", "recall"):
            source = raw.get(group)
            if isinstance(source, dict):
                value = source.pop("embedding_provider_id", "")
                if not memory.get("embedding_provider_id") and value:
                    memory["embedding_provider_id"] = value
    control = raw.get("control")
    if isinstance(control, dict) and "minitouch_enabled" not in control:
        touch = control.get("minitouch", {})
        control["minitouch_enabled"] = bool(isinstance(touch, dict) and touch.get("binary_path"))
    auto = raw.get("auto")
    if isinstance(auto, dict):
        auto.pop("share_target", None)
    return raw != before


def migrate_model_file(path):
    """在框架整理 schema 前备份并原子归组已保存的配置。"""
    path = Path(path)
    value, original = _read(path, optional=True)
    if not original or not migrate_models(value):
        return False
    backup = path.with_name(path.name + ".models-backup")
    if not backup.exists():
        _atomic_write(backup, original)
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
    return True


def populate_embedding_options(config, context):
    """按实际 Embedding Provider 填充原生 schema 下拉，不修改安装文件。"""
    schema = getattr(config, "schema", None)
    if not isinstance(schema, dict):
        return
    from .semantic_recall import _provider_id_of

    getter = getattr(context, "get_all_embedding_providers", None)
    providers = getter() if callable(getter) else []
    options = [""] + sorted({_provider_id_of(p) for p in providers})
    for group, key in (("memory", "embedding_provider_id"), ("model", "app_memory_embedding_provider_id")):
        items = schema.get(group, {}).get("items", {})
        if key in items:
            items[key]["options"] = options


def populate_failover_options(config, context):
    """多选框按用途展示AstrBot已配置模型及其供应商。"""
    from .model_failover import SLOTS, provider_id, providers_for, supplier_id

    schema = getattr(config, "schema", None)
    if not isinstance(schema, dict):
        return
    for slot, (group, key, kind) in SLOTS.items():
        providers = providers_for(context, kind)
        if slot == "asr":
            providers += providers_for(context, "chat")
        fields = schema.get(group, {}).get("items", {}).get(key, {}).get("items", {})
        if not fields:
            continue
        fields["models"]["options"] = sorted(
            {provider_id(p) for p in providers if provider_id(p)}
        )
        fields["suppliers"]["options"] = sorted(
            {supplier_id(p) for p in providers if supplier_id(p)}
        )
