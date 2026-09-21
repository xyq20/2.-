"""抖音资料输入的读取与校验，不包含任何页面操作。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MATERIAL_COMPONENT_PATTERN = re.compile(
    r"\s*(?P<name>[^/()（）]+?)\s*(?:[（(]\s*(?P<parenthesized>\d+)\s*%\s*[）)]|/\s*(?P<slashed>\d+)\s*%)"
)
COMPACT_MATERIAL_COMPONENT_PATTERN = re.compile(
    r"\s*(?P<name>[^/()（）\d]+?)\s*"
    r"(?:[（(]\s*(?P<parenthesized>\d+)\s*%\s*[）)]|(?P<bare>\d+)\s*%)\s*"
)

# 不应作为抖音商品属性匹配的业务输入字段。每项均为 Excel 键中可能出现的别名。
RESERVED_FIELD_ALIAS_NAMES = frozenset(
    {
        "商品分类",
        "商品标题",
        "商品名称",
        "导购短标题",
        "品牌",
        "吊牌价",
        "价格",
        "京东价",
        "市场价",
        "售卖价",
        "售价",
        "基本售价",
        "拼单价",
        "单买价",
        "满件折扣",
        "尺码",
        "SKU分类",
        "现货库存",
        "预售库存",
        "运费设置",
        "运费模板",
        "面料材质",
        "水洗标",
        "吊牌图",
        "面料",
        "面料俗称",
        "店铺中分类",
        "拍下减库存",
        "商品状态",
        "发布类型",
    }
)


class DouyinDataError(ValueError):
    """可直接向用户展示的抖音资料输入错误。"""


class FrozenAttributes(Mapping[str, str]):
    """可按字典读取、但不可变的抖音商品属性映射。"""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("抖音商品属性不可修改")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("抖音商品属性不可修改")

    def __getitem__(self, key: str) -> str:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __deepcopy__(self, _memo: Dict[int, Any]) -> "FrozenAttributes":
        return self


@dataclass(frozen=True)
class MaterialComponent:
    name: str
    percentage: int


@dataclass(frozen=True)
class DouyinFields:
    short_title: str
    attributes: Mapping[str, str]
    materials: Tuple[MaterialComponent, ...]
    materials_text: str
    sizes: Tuple[str, ...]
    price: str
    spot_stock: int
    presale_stock: int
    freight_aliases: Tuple[str, ...]


@dataclass(frozen=True)
class DouyinAssets:
    wash_label_images: Tuple[Path, ...]
    size_chart_image: Path
    height_weight_image: Path


def normalize_key(value: Any) -> str:
    """按页面字段匹配需要规范化 Excel 键名。"""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s:：]+", "", text).casefold()


RESERVED_FIELD_ALIASES = frozenset(normalize_key(alias) for alias in RESERVED_FIELD_ALIAS_NAMES)


def key_aliases(key: Any) -> Tuple[str, ...]:
    """将 Excel 中用 / 分隔的同义键拆开并规范化。"""
    normalized_key = unicodedata.normalize("NFKC", "" if key is None else str(key))
    aliases = tuple(normalize_key(part) for part in normalized_key.split("/") if normalize_key(part))
    return aliases or (normalize_key(key),)


def field_lookup(fields: Dict[str, Any], *aliases: str) -> Optional[Tuple[str, Any]]:
    """按完整键或斜杠分隔别名查找第一个字段，保留原始键和值。"""
    expected = {normalize_key(alias) for alias in aliases}
    for key, value in fields.items():
        if expected.intersection(key_aliases(key)):
            return key, value
    return None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _required_value(fields: Dict[str, Any], label: str, *aliases: str) -> Any:
    found = field_lookup(fields, *aliases)
    if found is None or not _cell_text(found[1]):
        raise DouyinDataError(f"Excel 中缺少必填抖音字段：{label}")
    return found[1]


def _normalized_decimal(value: Any, label: str, *, allow_grouped_thousands: bool = False) -> Decimal:
    original_text = _cell_text(value)
    text = original_text
    ungrouped_number = r"-?\d+(?:\.\d+)?"
    grouped_number = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    pattern = rf"(?:{ungrouped_number}|{grouped_number})" if allow_grouped_thousands else ungrouped_number
    if not re.fullmatch(pattern, text):
        raise DouyinDataError(f"{label}不是有效数字：{text!r}")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise DouyinDataError(f"{label}不是有效数字：{text!r}") from exc
    if not number.is_finite():
        raise DouyinDataError(f"{label}不是有效数字：{text!r}")
    return number


def _normalize_price(value: Any) -> str:
    text = _cell_text(value)
    normalized_value = text[:-1].strip() if text.endswith("元") else text
    number = _normalized_decimal(
        normalized_value, "价格", allow_grouped_thousands=True
    )
    if number < 0:
        raise DouyinDataError("价格不能小于 0")
    return format(number.normalize(), "f")


def _parse_stock(value: Any, label: str) -> int:
    number = _normalized_decimal(value, label)
    if number < 0 or number != number.to_integral_value():
        raise DouyinDataError(f"{label}必须是大于或等于 0 的整数")
    return int(number)


def parse_materials(value: Any) -> Tuple[MaterialComponent, ...]:
    """解析面料成分：逗号拆成多行，斜杠表示同一行的候选值。"""
    original_text = _cell_text(value)
    text = original_text.replace("／", "/")
    if not text:
        raise DouyinDataError("面料材质不能为空")

    def parse_component(candidate: str) -> Optional[MaterialComponent]:
        candidate = candidate.strip()
        match = COMPACT_MATERIAL_COMPONENT_PATTERN.fullmatch(candidate)
        if match is None:
            match = MATERIAL_COMPONENT_PATTERN.fullmatch(candidate)
        if match is None:
            return None
        name = match.group("name").strip()
        if not name:
            return None
        percentage_text = (
            match.groupdict().get("parenthesized")
            or match.groupdict().get("bare")
            or match.groupdict().get("slashed")
        )
        if percentage_text is None:
            return None
        percentage = int(percentage_text)
        if not 0 <= percentage <= 100:
            raise DouyinDataError("面料材质百分比必须在 0 到 100 之间")
        return MaterialComponent(name, percentage)

    def first_candidate(part: str) -> Optional[MaterialComponent]:
        # Try the complete expression first so ``棉/100%`` remains one
        # name/percentage expression rather than two OR candidates.
        parsed = parse_component(part)
        if parsed is not None:
            return parsed

        pieces = [piece.strip() for piece in part.split("/")]
        for index, piece in enumerate(pieces):
            parsed = parse_component(piece)
            if parsed is not None:
                return parsed
            # Also recognize ``棉/100%/棉布``: the first two slash pieces
            # are the expression and anything after them is an alternative.
            if index + 1 < len(pieces):
                parsed = parse_component("/".join(pieces[: index + 2]))
                if parsed is not None:
                    return parsed
        return None

    parts = [part.strip() for part in re.split(r"[,，、;；]", text)]
    if any(not part for part in parts):
        raise DouyinDataError(f"面料材质格式不正确：{original_text!r}")

    components = []
    for part in parts:
        component = first_candidate(part)
        if component is None:
            raise DouyinDataError(
                f"面料材质格式不正确：{original_text!r}，请使用“棉（100%）”、"
                "“棉/100%”或“棉94%，氨纶6%”"
            )
        components.append(component)

    if sum(component.percentage for component in components) != 100:
        raise DouyinDataError(
            f"面料材质百分比合计必须为 100：{original_text!r}"
        )
    return tuple(components)


def _split_nonempty(value: Any, separator: str, label: str) -> Tuple[str, ...]:
    values = tuple(part.strip() for part in _cell_text(value).split(separator) if part.strip())
    if not values:
        raise DouyinDataError(f"{label}不能为空")
    return values


def _douyin_attributes(fields: Dict[str, Any]) -> FrozenAttributes:
    """保留可用于后续抖音页面属性匹配的非空 Excel 键值。"""
    attributes: Dict[str, str] = {}
    for key, value in fields.items():
        if set(key_aliases(key)).intersection(RESERVED_FIELD_ALIASES):
            continue
        text = _cell_text(value)
        if text:
            attributes[key] = text
    return FrozenAttributes(attributes)


def parse_douyin_fields(fields: Dict[str, Any]) -> DouyinFields:
    """从 Excel 键值映射提取并校验抖音表单所需的业务字段。"""
    short_title = _cell_text(_required_value(fields, "导购短标题", "导购短标题"))
    materials_value = _required_value(
        fields, "面料材质", "面料材质", "水洗标", "吊牌图", "面料", "面料俗称"
    )
    materials_text = _cell_text(materials_value)
    materials = parse_materials(materials_text)
    sizes = _split_nonempty(_required_value(fields, "尺码", "尺码"), "/", "尺码")
    price = _normalize_price(
        _required_value(fields, "价格", "价格", "京东价", "市场价", "售卖价", "售价")
    )
    spot_stock = _parse_stock(_required_value(fields, "现货库存", "现货库存"), "现货库存")
    presale_stock = _parse_stock(_required_value(fields, "预售库存", "预售库存"), "预售库存")
    freight_aliases = _split_nonempty(
        _required_value(fields, "运费设置", "运费设置", "运费模板"), "/", "运费设置"
    )
    return DouyinFields(
        short_title=short_title,
        attributes=_douyin_attributes(fields),
        materials=materials,
        materials_text=materials_text,
        sizes=sizes,
        price=price,
        spot_stock=spot_stock,
        presale_stock=presale_stock,
        freight_aliases=freight_aliases,
    )


def _natural_key(path: Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def _images_in(directory: Path) -> Tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES),
            key=_natural_key,
        )
    )


def _exactly_one_image(product_dir: Path, folder_name: str) -> Path:
    directory = product_dir / folder_name
    files = _images_in(directory)
    if len(files) != 1:
        raise DouyinDataError(f"{folder_name}文件夹必须恰好 1 张支持的图片，当前为 {len(files)} 张：{directory}")
    return files[0]


def read_douyin_assets(product_dir: Path) -> DouyinAssets:
    """读取抖音资料图片。

    水洗标图片只按 ``水洗标图片`` 文件夹内的图片文件读取，文件名不参与
    匹配；因此 ``1.png``、``吊牌正面.jpg`` 或其他任意图片文件名都等价。
    """
    wash_label_images = _images_in(product_dir / "水洗标图片")
    return DouyinAssets(
        wash_label_images=wash_label_images,
        size_chart_image=_exactly_one_image(product_dir, "尺码信息表"),
        height_weight_image=_exactly_one_image(product_dir, "身高体重推荐表"),
    )
