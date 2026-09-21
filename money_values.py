"""Conservative normalization for monetary values read from Excel."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata


class MoneyValueError(ValueError):
    """Raised when an Excel value is not an unambiguous amount."""


def normalize_money_value(value: object) -> str:
    """Return a plain decimal string while accepting common currency notation.

    Only an optional currency symbol, a correctly grouped decimal number, and
    an optional trailing ``元`` are accepted.  Text such as ``690元起`` remains
    invalid so automation never turns an ambiguous price into a write.
    """

    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    match = re.fullmatch(
        r"(?:[¥￥]\s*)?"
        r"((?:\d+(?:\.\d+)?)|(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?))"
        r"\s*(?:元)?",
        text,
    )
    if match is None:
        raise MoneyValueError(f"不是有效金额：{text!r}")
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation as exc:
        raise MoneyValueError(f"不是有效金额：{text!r}") from exc
    if not number.is_finite() or number < 0:
        raise MoneyValueError(f"不是有效金额：{text!r}")
    return format(number.normalize(), "f")


__all__ = ["MoneyValueError", "normalize_money_value"]
