"""严格 YAML 加载：**重复键直接报错**，不允许静默覆盖。

为什么需要它
------------
PyYAML 遇到同一个映射里出现两次同名键，既不报错也不警告，直接让后者覆盖前者。
本体数据里最容易踩的写法是给一条边挂两段 `props`：

```yaml
  props:
    target_level_m: 340.0      # ← 被下面那行静默吃掉
  props:
    clause: "调度规程 3.3.1.2"
```

丢的是目标水位这种关键值，而且库里、台上、测试里都不会有任何提示——
直到有人问「干旱年维持多少水位」才发现答错。**这类错误必须在加载时就炸掉**，
而不是等到答案错了再回头翻 YAML。

用法：所有吃 YAML 的地方都走 `load_yaml_strict`；写文件的场景（保留注释）用 ruamel。
"""
from __future__ import annotations

from typing import Any

import yaml

__all__ = ["DuplicateKeyError", "load_yaml_strict"]


class DuplicateKeyError(ValueError):
    """同一个映射里出现重复键。带文件名与行号，方便直接定位。"""

    def __init__(self, key: Any, line: int, first_line: int, source: str = "") -> None:
        where = f"{source} 第 {line} 行" if source else f"第 {line} 行"
        self.key = key
        self.line = line
        self.first_line = first_line
        super().__init__(
            f"YAML 重复键 {key!r}（{where}，首次出现在第 {first_line} 行）。"
            "同一个映射里同名键会互相覆盖，后者静默吃掉前者——请合并成一段。"
        )


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader + 映射重复键检查。"""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: dict[Any, int] = {}
    for key_node, _value_node in node.value:
        try:
            key = loader.construct_object(key_node, deep=True)
        except yaml.YAMLError:  # 复杂键构造失败时交给父类去报它的错
            continue
        try:
            hash(key)
        except TypeError:  # 不可哈希的键（极少见），不做去重判断
            continue
        if key in seen:
            raise DuplicateKeyError(key, key_node.start_mark.line + 1, seen[key])
        seen[key] = key_node.start_mark.line + 1
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def load_yaml_strict(text: str, *, source: str = "") -> Any:
    """解析 YAML 文本；出现重复键、语法错误都抛异常。

    `source` 只用于错误信息（通常是文件名），让报错能直接指到文件。
    """
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except DuplicateKeyError as exc:
        if source:
            raise DuplicateKeyError(exc.key, exc.line, exc.first_line, source) from None
        raise
    except yaml.YAMLError as exc:
        if source:
            raise yaml.YAMLError(f"{source}: {exc}") from exc
        raise
