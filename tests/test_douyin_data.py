import json
import tempfile
import unittest
from pathlib import Path

import kuaimai_erp
from douyin_data import (
    DouyinDataError,
    MaterialComponent,
    field_lookup,
    key_aliases,
    normalize_key,
    parse_douyin_fields,
    parse_materials,
    read_douyin_assets,
)


CURRENT_PRODUCT_FIELDS = {
    "导购短标题": "重磅洗水宽松多口袋工装裤",
    "厚度": "常规款",
    "面料材质/水洗标/吊牌图/面料": "棉（100%）",
    "尺码": "S/M/L/XL/2XL",
    "价格/京东价/市场价/售卖价/售价": 586,
    "现货库存": 0,
    "预售库存": 100,
    "运费设置": "新疆，西藏，不包邮-T恤，裤子，装饰品/新疆西藏不包邮T恤裤子装饰品",
    "SKU分类": "单品",
}


class DouyinFieldParsingTests(unittest.TestCase):
    def test_normalizes_full_width_slash_delimited_key_aliases_for_lookup(self):
        self.assertEqual(key_aliases("  货号／ 商家外部编码 "), ("货号", "商家外部编码"))
        self.assertEqual(field_lookup({"价格／ 售价": 586}, "售价"), ("价格／ 售价", 586))

    def test_normalize_key_removes_whitespace_and_colons_throughout(self):
        self.assertEqual(normalize_key(" 商 品：标: 题 "), "商品标题")

    def test_parses_current_product_fields_and_preserves_attributes(self):
        result = parse_douyin_fields(CURRENT_PRODUCT_FIELDS)

        self.assertEqual(result.short_title, "重磅洗水宽松多口袋工装裤")
        self.assertEqual(result.materials, (MaterialComponent("棉", 100),))
        self.assertEqual(result.sizes, ("S", "M", "L", "XL", "2XL"))
        self.assertEqual(result.price, "586")
        self.assertEqual(result.spot_stock, 0)
        self.assertEqual(result.presale_stock, 100)
        self.assertEqual(
            result.freight_aliases,
            ("新疆，西藏，不包邮-T恤，裤子，装饰品", "新疆西藏不包邮T恤裤子装饰品"),
        )
        self.assertEqual(result.attributes["厚度"], "常规款")
        self.assertNotIn("导购短标题", result.attributes)
        self.assertNotIn("尺码", result.attributes)
        self.assertNotIn("价格/京东价/市场价/售卖价/售价", result.attributes)
        self.assertNotIn("现货库存", result.attributes)
        self.assertNotIn("面料材质/水洗标/吊牌图/面料", result.attributes)
        self.assertNotIn("SKU分类", result.attributes)

    def test_attributes_retain_only_nonblank_douyin_product_attributes_as_strings(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        fields.update(
            {
                "流行元素/款式细节": 430,
                "货号/商家外部编码": "NGBL-10588",
                "商品标题/商品名称": "商品标题",
                "空白属性": "   ",
                "空值属性": None,
            }
        )

        result = parse_douyin_fields(fields)

        self.assertEqual(result.attributes, {"厚度": "常规款", "流行元素/款式细节": "430"})

    def test_parses_supported_material_syntaxes(self):
        self.assertEqual(parse_materials("棉（100%）"), (MaterialComponent("棉", 100),))
        self.assertEqual(parse_materials("棉/100%"), (MaterialComponent("棉", 100),))

    def test_uses_standalone_material_common_name_as_material_source(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        del fields["面料材质/水洗标/吊牌图/面料"]
        fields["面料俗称"] = "棉/100%"

        self.assertEqual(parse_douyin_fields(fields).materials, (MaterialComponent("棉", 100),))

    def test_rejects_missing_required_fields(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        del fields["导购短标题"]

        with self.assertRaisesRegex(DouyinDataError, "导购短标题"):
            parse_douyin_fields(fields)

    def test_rejects_missing_and_blank_sizes_stocks_and_freight(self):
        required_fields = (
            ("尺码", "尺码"),
            ("现货库存", "现货库存"),
            ("预售库存", "预售库存"),
            ("运费设置", "运费设置"),
        )
        for key, label in required_fields:
            with self.subTest(field=key, state="missing"):
                fields = dict(CURRENT_PRODUCT_FIELDS)
                del fields[key]
                with self.assertRaisesRegex(DouyinDataError, label):
                    parse_douyin_fields(fields)
            with self.subTest(field=key, state="blank"):
                fields = dict(CURRENT_PRODUCT_FIELDS)
                fields[key] = "  "
                with self.assertRaisesRegex(DouyinDataError, label):
                    parse_douyin_fields(fields)

    def test_rejects_negative_and_fractional_stocks(self):
        for key, label in (("现货库存", "现货库存"), ("预售库存", "预售库存")):
            for value in (-1, "1.5"):
                with self.subTest(field=key, value=value):
                    fields = dict(CURRENT_PRODUCT_FIELDS)
                    fields[key] = value
                    with self.assertRaisesRegex(DouyinDataError, label):
                        parse_douyin_fields(fields)

    def test_rejects_non_finite_price(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        fields["价格/京东价/市场价/售卖价/售价"] = "NaN"

        with self.assertRaisesRegex(DouyinDataError, "价格不是有效数字"):
            parse_douyin_fields(fields)

    def test_rejects_invalid_price(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        fields["价格/京东价/市场价/售卖价/售价"] = "五百八十六"

        with self.assertRaisesRegex(DouyinDataError, "价格不是有效数字"):
            parse_douyin_fields(fields)

    def test_rejects_negative_price(self):
        fields = dict(CURRENT_PRODUCT_FIELDS)
        fields["价格/京东价/市场价/售卖价/售价"] = -0.01

        with self.assertRaisesRegex(DouyinDataError, "价格不能小于 0"):
            parse_douyin_fields(fields)

    def test_rejects_invalid_material_percentages(self):
        with self.assertRaisesRegex(DouyinDataError, "0 到 100"):
            parse_materials("棉（101%）")


class DouyinAssetDiscoveryTests(unittest.TestCase):
    def _write_image(self, directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b"image")
        return path

    def test_discovers_assets_with_naturally_sorted_wash_labels(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            product_dir = Path(temporary_directory)
            tenth = self._write_image(product_dir / "水洗标图片", "10.png")
            first = self._write_image(product_dir / "水洗标图片", "2.png")
            second = self._write_image(product_dir / "水洗标图片", "11.png")
            size_chart = self._write_image(product_dir / "尺码信息表", "size.jpg")
            height_weight = self._write_image(product_dir / "身高体重推荐表", "recommendation.webp")

            result = read_douyin_assets(product_dir)

            self.assertEqual(result.wash_label_images, (first, tenth, second))
            self.assertEqual(result.size_chart_image, size_chart)
            self.assertEqual(result.height_weight_image, height_weight)

    def test_requires_exactly_one_size_and_recommendation_image(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            product_dir = Path(temporary_directory)
            self._write_image(product_dir / "水洗标图片", "label.jpg")
            recommendation = self._write_image(product_dir / "身高体重推荐表", "1.jpg")

            with self.assertRaisesRegex(DouyinDataError, "尺码信息表.*恰好 1 张"):
                read_douyin_assets(product_dir)

            self._write_image(product_dir / "尺码信息表", "1.jpg")
            recommendation.unlink()
            with self.assertRaisesRegex(DouyinDataError, "身高体重推荐表.*恰好 1 张"):
                read_douyin_assets(product_dir)

            self._write_image(product_dir / "身高体重推荐表", "1.jpg")
            self._write_image(product_dir / "尺码信息表", "2.jpg")

            with self.assertRaisesRegex(DouyinDataError, "尺码信息表.*恰好 1 张"):
                read_douyin_assets(product_dir)

            (product_dir / "尺码信息表" / "2.jpg").unlink()
            self._write_image(product_dir / "身高体重推荐表", "2.jpg")
            with self.assertRaisesRegex(DouyinDataError, "身高体重推荐表.*恰好 1 张"):
                read_douyin_assets(product_dir)


class KuaimaiIntegrationTests(unittest.TestCase):
    def test_read_excel_fields_and_product_summary_keep_douyin_data_serializable(self):
        rows = (("导购短标题", "短标题", None), ("现货库存", 0, None))
        fields = kuaimai_erp.read_excel_fields(rows)
        self.assertEqual(fields, {"导购短标题": "短标题", "现货库存": 0})

        product = kuaimai_erp.ProductData(
            excel_path=Path("/input/产品信息.xlsx"),
            product_dir=Path("/input"),
            title="标题",
            style_code="款号",
            base_price="586",
            main_images=[Path("/input/main.png")],
            main_images_34=[],
            detail_images=[],
            sku_images=[],
            douyin_fields=parse_douyin_fields(CURRENT_PRODUCT_FIELDS),
            douyin_assets=read_douyin_assets_for_summary(),
        )

        summary = kuaimai_erp.product_summary(product)
        self.assertEqual(summary["douyin_assets"]["wash_label_images"], ["/input/wash.png"])
        self.assertEqual(summary["douyin_assets"]["size_chart_image"], "/input/size.png")
        json.dumps(summary, ensure_ascii=False)


def read_douyin_assets_for_summary():
    from douyin_data import DouyinAssets

    return DouyinAssets(
        wash_label_images=(Path("/input/wash.png"),),
        size_chart_image=Path("/input/size.png"),
        height_weight_image=Path("/input/recommendation.png"),
    )
