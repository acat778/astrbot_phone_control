"""当前 Android 用户的已安装应用清单、筛选与分页。"""

import re

from .apps import app_label_for_package
from .errors import PhoneControlError


SCOPES = {"third_party": "第三方", "system": "系统", "all": "全部"}


def parse_installed_packages(output):
    """校验包管理器完整输出，支持单段系统包名。"""
    packages = set()
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.fullmatch(r"package:([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)", line)
        if match is None:
            raise PhoneControlError("读取已安装 App 失败：包管理器返回无效清单")
        packages.add(match[1])
    return sorted(packages)


async def list_apps(adb, *, scope="third_party", query="", offset=0, limit=50, policy=None):
    """实时读取包管理器；展示名称来自插件应用名称表，未知名称明确标记。"""
    if scope not in SCOPES:
        raise PhoneControlError("scope 需为 third_party、system 或 all")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise PhoneControlError("offset 需为非负整数，limit 需为 1–200 的整数")
    if not isinstance(query, str) or len(query) > 200:
        raise PhoneControlError("query 需为不超过 200 字的名称或包名关键词")
    flag = {"third_party": " -3", "system": " -s", "all": ""}[scope]
    output = await adb.shell("pm list packages --user current" + flag, timeout=20)
    packages = parse_installed_packages(output)
    keyword = query.strip().casefold()
    matches = [(package, app_label_for_package(package)) for package in packages]
    matches = [(package, label) for package, label in matches
               if not keyword or keyword in package.casefold() or keyword in label.casefold()]
    page = matches[offset:offset + limit]
    lines = [f"已安装{SCOPES[scope]} App（当前 Android 用户，共 {len(packages)} 个，匹配 {len(matches)} 个，按包名排序）："]
    if not page:
        lines.append("当前页没有匹配应用。")
    for package, label in page:
        name = f"{label}（{package}）" if label else f"{package}（名称未收录）"
        if policy is None:
            lines.append("- " + name)
        else:
            try:
                kind = await policy.classify(package)
                lines.append(f"- {name}：{kind['category_label']} · {'允许打开' if kind['allowed'] else '拒绝打开'}；{kind['reason']}")
            except PhoneControlError as exc:
                lines.append(f"- {name}：未确认 · 拒绝打开；{exc}")
    if page:
        lines.append(f"本页 {offset + 1}–{offset + len(page)}，limit={limit}。")
    if offset + len(page) < len(matches):
        lines.append(f"还有应用，请使用 offset={offset + len(page)} 继续读取；命令可加 --page {offset // limit + 2}。")
    lines.append("名称来自插件名称表；打开应用时按安装元数据校验类型，金融类或类型未确认时拒绝打开。")
    return "\n".join(lines)
