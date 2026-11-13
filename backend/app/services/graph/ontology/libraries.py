"""图谱库：一个库 = 一份 TBox（YAML）+ 属于它的实例与关系。

**为什么用 YAML 文件当"库"，而不是新建一张库表**

- TBox 本来就是版本化数据（能 diff、能回滚、能随代码走），和"库"是一一对应的；
- 实例表与关系表已经有 `profile_id` 列，天然按库隔离，不需要中间映射表；
- 建库 = 生成一份新 YAML，删库 = 删掉那份 YAML（库里有数据就禁止删）。
  这样"库"的真相永远在文件里，不会出现「数据库说有这么个库，但 TBox 文件早没了」。

界面上看到的中文库名来自 YAML 的 `display_name`；没写就回落到 profile_id。
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import settings
from app.services.graph.ontology.registry import (
    DEFAULT_PROFILE,
    PROFILES_DIR,
    ProfileError,
    load_profile,
)

logger = logging.getLogger(__name__)

# profile_id 直接当文件名用，必须是安全短标识（同时也是 API 里的 ?profile= 取值）
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# 新建库时可选的模板：None = 空白库（只有最基本的参数类型）
TEMPLATE_BLANK = "blank"


@dataclass(frozen=True)
class LibraryInfo:
    profile_id: str
    display_name: str
    description: str
    file: str
    entity_type_count: int
    relation_type_count: int
    # 这个库声明了哪些实体类型：图库是按类型收口的，算「库里有多少节点」要用到
    entity_types: tuple[str, ...] = ()
    # YAML 解析失败时带上原因：界面上把坏库列出来并标红，比直接消失好排查
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("entity_types", None)  # 内部用，不进接口响应
        return data


def validate_profile_id(profile_id: str) -> str:
    """校验库标识：小写字母开头，只允许 a-z0-9_，2~40 位。"""
    value = (profile_id or "").strip()
    if not _SLUG_RE.match(value):
        raise ProfileError(
            f"库标识 {value!r} 不合法：只能用小写字母开头、a-z / 0-9 / 下划线，2~40 位"
        )
    return value


def library_path(profile_id: str) -> Path:
    return PROFILES_DIR / f"{validate_profile_id(profile_id)}.yaml"


def list_libraries() -> list[LibraryInfo]:
    """扫 profiles 目录，把所有库列出来（含解析失败的，带 error 说明）。"""
    out: list[LibraryInfo] = []
    if not PROFILES_DIR.exists():
        return out
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        try:
            reg = load_profile(path, register=False)
        except Exception as exc:  # noqa: BLE001 - 坏库要能列出来，不能整页 500
            logger.warning("图谱库 %s 解析失败：%s", path.name, exc)
            out.append(
                LibraryInfo(
                    profile_id=path.stem,
                    display_name=path.stem,
                    description="",
                    file=path.name,
                    entity_type_count=0,
                    relation_type_count=0,
                    error=str(exc),
                )
            )
            continue
        out.append(
            LibraryInfo(
                profile_id=reg.profile_id,
                display_name=reg.library_name,
                description=reg.description,
                file=path.name,
                entity_type_count=len(reg.entity_types),
                relation_type_count=len(reg.relation_types),
                entity_types=tuple(reg.entity_types),
            )
        )
    # 当前生效的库排最前，其余按名字排
    out.sort(key=lambda i: (i.profile_id != settings.ontology_profile, i.display_name))
    return out


def library_exists(profile_id: str) -> bool:
    try:
        return library_path(profile_id).exists()
    except ProfileError:
        return False


def create_library(
    profile_id: str,
    display_name: str,
    description: str = "",
    *,
    template: str = DEFAULT_PROFILE,
) -> LibraryInfo:
    """从模板复制出一份新 TBox。`template="blank"` 时生成最小骨架。

    只改文件头三行（profile_id / display_name / description），**保留模板里的注释与顺序**——
    注释是写给维护者看的，整份重写 YAML 会把它们全丢掉。
    """
    pid = validate_profile_id(profile_id)
    path = library_path(pid)
    if path.exists():
        raise ProfileError(f"图谱库 {pid!r} 已存在")

    if template == TEMPLATE_BLANK:
        text = _blank_profile_text(pid, display_name, description)
    else:
        src = PROFILES_DIR / f"{validate_profile_id(template)}.yaml"
        if not src.exists():
            raise ProfileError(f"模板 {template!r} 不存在")
        text = _retitle(src.read_text(encoding="utf-8"), pid, display_name, description)

    path.write_text(text, encoding="utf-8", newline="\n")
    # 立刻解析一次：YAML 写错要在创建时就报出来，而不是等用户点进图谱页才炸
    try:
        reg = load_profile(path, register=False)
    except Exception:
        path.unlink(missing_ok=True)  # 回滚：半成品库不该留在磁盘上
        raise
    return LibraryInfo(
        profile_id=reg.profile_id,
        display_name=reg.library_name,
        description=reg.description,
        file=path.name,
        entity_type_count=len(reg.entity_types),
        relation_type_count=len(reg.relation_types),
        entity_types=tuple(reg.entity_types),
    )


def import_library(text: str, *, overwrite: bool = False) -> LibraryInfo:
    """导入一份现成的 TBox YAML（换机器带走、或别人给的库）。

    默认**不覆盖**同名库：覆盖会连带把已有的实例/关系晾在一边，
    属于"看起来成功了实际上毁了数据"的操作，必须显式要求。
    """
    from app.services.graph.ontology.excel_schema import parse_yaml_profile

    meta = parse_yaml_profile(text)
    pid = validate_profile_id(meta["profile_id"])
    path = library_path(pid)
    if path.exists() and not overwrite:
        raise ProfileError(f"图谱库 {pid!r} 已存在；如需替换请显式选择覆盖")

    backup = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        reg = load_profile(path, register=False)
    except Exception:
        if backup is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(backup, encoding="utf-8", newline="\n")
        raise
    return LibraryInfo(
        profile_id=reg.profile_id,
        display_name=reg.library_name,
        description=reg.description,
        file=path.name,
        entity_type_count=len(reg.entity_types),
        relation_type_count=len(reg.relation_types),
        entity_types=tuple(reg.entity_types),
    )


def read_library_text(profile_id: str) -> tuple[str, str]:
    """导出用：返回 (文件名, YAML 原文)。"""
    path = library_path(profile_id)
    if not path.exists():
        raise ProfileError(f"图谱库 {profile_id!r} 不存在")
    return path.name, path.read_text(encoding="utf-8")


def delete_library(profile_id: str) -> None:
    """删库只删 YAML；调用方必须先确认库里没有实例/关系。"""
    pid = validate_profile_id(profile_id)
    if pid == DEFAULT_PROFILE:
        raise ProfileError(f"默认图谱库 {pid!r} 不能删除")
    if pid == settings.ontology_profile:
        raise ProfileError(f"图谱库 {pid!r} 正在被系统配置使用（ONTOLOGY_PROFILE），不能删除")
    path = library_path(pid)
    if not path.exists():
        raise ProfileError(f"图谱库 {pid!r} 不存在")
    path.unlink()


# ---------------- 内部：模板生成 ----------------

_HEAD_KEYS = ("profile_id", "display_name", "description")


def _retitle(text: str, profile_id: str, display_name: str, description: str) -> str:
    """替换文件头的 profile_id/display_name/description，其余原样保留。"""
    values = {
        "profile_id": profile_id,
        "display_name": display_name,
        "description": description,
    }
    lines = text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        key = stripped.split(":", 1)[0].strip() if ":" in stripped else ""
        # 只认顶格且非注释的头部键，别动 entity_types 里嵌套的同名键
        is_head = key in _HEAD_KEYS and not line[:1].isspace() and not stripped.startswith("#")
        if is_head:
            if key in seen:
                continue  # 已经在文件头补过，丢弃后面的重复行
            out.append(f"{key}: {values[key]}")
            seen.add(key)
            if key == "profile_id":
                # 把缺的两项紧跟 profile_id 补上（避免落到文件末尾变成游离键）
                for extra in ("display_name", "description"):
                    if extra not in seen:
                        out.append(f"{extra}: {values[extra]}")
                        seen.add(extra)
            continue
        out.append(line)
    for missing in _HEAD_KEYS:
        if missing not in seen:
            out.append(f"{missing}: {values[missing]}")
    return "\n".join(out) + "\n"


def _blank_profile_text(profile_id: str, display_name: str, description: str) -> str:
    return f"""# ============================================================
# 空白图谱库（TBox）
# ------------------------------------------------------------
# 只保留最基本的「参数」类型，其余按需要在「本体管理」里加。
# 具体取值不是本体，是数据（实例），不要写在这里。
# ============================================================
version: 1
profile_id: {profile_id}
display_name: {display_name}
description: {description}
domain: generic

entity_types:
  - name: Parameter
    display_name: 参数
    uid_prefix: pm
    scope: local
    attributes:
      - {{ name: group, type: enum, values: [通用], required: true }}
      - {{ name: unit, type: string }}

relation_types: []
"""
