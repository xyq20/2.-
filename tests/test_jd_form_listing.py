"""Tests for JD platform form filling and verification."""

from __future__ import annotations

import pytest

from jd_data import JdFields, parse_jd_fields
from jd_form_listing import (
    JD_BRAND,
    JD_CATEGORY_PATH,
    JD_DELIVERY_TEMPLATE,
    JD_SKU_THICKNESS,
    JdFormListing,
    JdFormListingError,
    _category_parts,
    _kilograms,
    _material_name,
    _material_percentage,
    _numeric_equal,
)


def test_numeric_equal():
    assert _numeric_equal("100", "100")
    assert _numeric_equal("100.00", "100")
    assert _numeric_equal("0.43", "0.430")
    assert _numeric_equal("586", "586.00")
    assert not _numeric_equal("100", "101")
    assert not _numeric_equal("abc", "123")


def test_material_name():
    assert _material_name("棉") == "棉"
    assert _material_name("棉（100%）") == "棉"
    assert _material_name("棉 (100%)") == "棉"
    assert _material_name("涤纶（50%）") == "涤纶"
    assert _material_name("棉 ( 100 % )") == "棉"


def test_material_percentage():
    assert _material_percentage("棉（100%）") == "100"
    assert _material_percentage("棉 (50%)") == "50"
    assert _material_percentage("涤纶50%") == "50"
    assert _material_percentage("棉") is None
    assert _material_percentage("100") is None


def test_kilograms():
    assert _kilograms("430g") == "0.43"
    assert _kilograms("430克") == "0.43"
    assert _kilograms("0.43kg") == "0.43"
    assert _kilograms("0.43公斤") == "0.43"
    assert _kilograms("1000g") == "1"
    assert _kilograms("1kg") == "1"

    with pytest.raises(JdFormListingError, match="无法换算"):
        _kilograms("abc")

    with pytest.raises(JdFormListingError, match="必须大于 0"):
        _kilograms("0g")


def test_category_parts():
    assert _category_parts("服饰内衣>男装>男士休闲裤>男士休闲直筒裤") == (
        "服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤"
    )
    assert _category_parts("服饰内衣/男装/男士休闲裤") == (
        "服饰内衣", "男装", "男士休闲裤"
    )
    assert _category_parts("服饰内衣 > 男装") == ("服饰内衣", "男装")


def test_parse_jd_fields():
    fields = {
        "品牌": "NEIGBORL",
        "货号": "NGBL-10588",
        "产地": "中国大陆",
        "克重": "430g",
        "京东价": "586",
        "库存": "100",
        "面料": "棉",
        "材质": "棉（100%）",
        "裤长": "长裤",
        "": "",  # Empty key
        "空值": None,  # None value
    }

    jd_fields = parse_jd_fields(fields)

    assert isinstance(jd_fields, JdFields)
    assert jd_fields.fields["品牌"] == "NEIGBORL"
    assert jd_fields.fields["货号"] == "NGBL-10588"
    assert jd_fields.fields["产地"] == "中国大陆"
    assert jd_fields.fields["克重"] == "430g"
    assert jd_fields.fields["京东价"] == "586"
    assert jd_fields.fields["库存"] == "100"
    assert jd_fields.fields["面料"] == "棉"
    assert jd_fields.fields["材质"] == "棉（100%）"
    assert jd_fields.fields["裤长"] == "长裤"

    # Empty keys and None values should be filtered out
    assert "" not in jd_fields.fields
    assert "空值" not in jd_fields.fields


def test_jd_fields_immutable():
    fields = {"品牌": "NEIGBORL", "货号": "NGBL-10588"}
    jd_fields = parse_jd_fields(fields)

    # FrozenFields should be immutable
    with pytest.raises(AttributeError):
        jd_fields.fields["品牌"] = "OTHER"

    with pytest.raises(AttributeError):
        jd_fields.fields.new_field = "value"


def test_constants():
    """Verify that constants match CLAUDE.md requirements."""
    assert JD_CATEGORY_PATH == ("服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤")
    assert JD_BRAND == "NEIGBORL"
    assert JD_DELIVERY_TEMPLATE == "48小时发货"
    assert JD_SKU_THICKNESS == "常规"


def test_json_category_nodes():
    """Test JSON category node extraction."""
    payload = {
        "data": {
            "categories": [
                {"id": 1, "name": "服饰内衣", "children": []},
                {"id": 2, "name": "男装", "children": []},
                {"id": 3, "name": "男士休闲直筒裤", "parent": "男士休闲裤"},
            ]
        }
    }

    nodes = JdFormListing._json_category_nodes(payload, "男士休闲直筒裤")
    assert len(nodes) == 1
    assert nodes[0]["name"] == "男士休闲直筒裤"


def test_json_contains_category():
    """Test category detection in JSON payload."""
    payload = {"categories": [{"name": "男士休闲直筒裤"}]}

    assert JdFormListing._json_contains_category(payload, "男士休闲直筒裤")
    assert not JdFormListing._json_contains_category(payload, "不存在的类目")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
