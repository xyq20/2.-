"""跨平台表单同步的纯校验规则。

页面适配器只负责读取/写入 DOM；是否已满足期望值由这里统一判断，
后续抖音以外的平台可以复用同一套“先回读、相同则跳过”语义。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Tuple


def split_or_values(value: object) -> Tuple[str, ...]:
    """将 ``A/B/C`` 解析为按优先级排列的 OR 候选。"""
    return tuple(part.strip() for part in str(value).split("/") if part.strip())


def matches_any(
    actual_values: Iterable[object],
    expected_values: Iterable[object],
    normalize: Callable[[object], str],
) -> bool:
    """当前任一非空值命中任一期望候选时返回 True。"""
    expected = {normalize(value) for value in expected_values if normalize(value)}
    return any(normalize(value) in expected for value in actual_values if normalize(value))


def sequences_match(
    actual_values: Iterable[object],
    expected_values: Iterable[object],
    normalize: Callable[[object], str],
) -> bool:
    """忽略顺序比较两个值集合，适用于真正的多选字段。"""
    return sorted(normalize(value) for value in actual_values) == sorted(
        normalize(value) for value in expected_values
    )
