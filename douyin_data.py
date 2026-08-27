"""抖音资料输入的读取与校验，不包含任何页面操作。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 不应作为抖音商品属性匹配的业务输入字段。每项均为 Excel 键中可能出现的别名。
RESERVED_FIELD_ALIAS_NAMES = frozenset(
    {
        "商品分类",
        "商品标题",
        "商品名称",
        "导购短标题",
        "品牌",
        "货号",
        "商家外部编码",
        "款式编码",
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


@dataclass(frozen=True)
class MaterialComponent:
    name: str
    percentage: int


@dataclass(frozen=True)
class DouyinFields:
    short_title: str
    attributes: Dict[str, Any]
    materials: Tuple[MaterialComponent, ...]
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


def _normalized_decimal(value: Any, label: str) -> Decimal:
    text = _cell_text(value).replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise DouyinDataError(f"{label}不是有效数字：{text!r}") from exc
    if not number.is_finite():
        raise DouyinDataError(f"{label}不是有效数字：{text!r}")
    return number


def _normalize_price(value: Any) -> str:
    number = _normalized_decimal(value, "价格")
    if number < 0:
        raise DouyinDataError("价格不能小于 0")
    return format(number.normalize(), "f")


def _parse_stock(value: Any, label: str) -> int:
    number = _normalized_decimal(value, label)
    if number < 0 or number != number.to_integral_value():
        raise DouyinDataError(f"{label}必须是大于或等于 0 的整数")
    return int(number)


def parse_materials(value: Any) -> Tuple[MaterialComponent, ...]:
    """解析“棉（100%）”或“棉/100%”格式的面料成分。"""
    text = _cell_text(value)
    match = re.fullmatch(r"\s*(.+?)\s*(?:[（(]\s*(\d+)\s*%\s*[）)]|/\s*(\d+)\s*%)\s*", text)
    if not match:
        raise DouyinDataError(f"面料材质格式不正确：{text!r}，请使用“棉（100%）”或“棉/100%”")
    name = match.group(1).strip()
    percentage = int(match.group(2) or match.group(3))
    if not name:
        raise DouyinDataError("面料材质名称不能为空")
    if not 0 <= percentage <= 100:
        raise DouyinDataError("面料材质百分比必须在 0 到 100 之间")
    return (MaterialComponent(name, percentage),)


def _split_nonempty(value: Any, separator: str, label: str) -> Tuple[str, ...]:
    values = tuple(part.strip() for part in _cell_text(value).split(separator) if part.strip())
    if not values:
        raise DouyinDataError(f"{label}不能为空")
    return values


def _douyin_attributes(fields: Dict[str, Any]) -> Dict[str, str]:
    """保留可用于后续抖音页面属性匹配的非空 Excel 键值。"""
    attributes: Dict[str, str] = {}
    for key, value in fields.items():
        if set(key_aliases(key)).intersection(RESERVED_FIELD_ALIASES):
            continue
        text = _cell_text(value)
        if text:
            attributes[key] = text
    return attributes


def parse_douyin_fields(fields: Dict[str, Any]) -> DouyinFields:
    """从 Excel 键值映射提取并校验抖音表单所需的业务字段。"""
    short_title = _cell_text(_required_value(fields, "导购短标题", "导购短标题"))
    materials = parse_materials(
        _required_value(fields, "面料材质", "面料材质", "水洗标", "吊牌图", "面料", "面料俗称")
    )
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
    """读取抖音资料所需的水洗标、尺码表和身高体重推荐表图片。"""
    wash_label_images = _images_in(product_dir / "水洗标图片")
    if not wash_label_images:
        raise DouyinDataError(f"水洗标图片文件夹中没有可用图片：{product_dir / '水洗标图片'}")
    return DouyinAssets(
        wash_label_images=wash_label_images,
        size_chart_image=_exactly_one_image(product_dir, "尺码信息表"),
        height_weight_image=_exactly_one_image(product_dir, "身高体重推荐表"),
    )
