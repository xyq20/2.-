from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional, Tuple


@dataclass(frozen=True)
class PlatformSpec:
    cli_name: str
    platform_id: str
    display_name: str
    tab_label: str
    lifecycle: str
    enabled_in_all: bool
    supports_inspect: bool
    save_policy: str
    publish_allowed: bool
    discovery_adapter: Optional[str]


PLATFORM_SPECS: Tuple[PlatformSpec, ...] = (
    PlatformSpec(
        cli_name="base",
        platform_id="base",
        display_name="基础资料",
        tab_label="基础资料",
        lifecycle="implemented",
        enabled_in_all=False,
        supports_inspect=False,
        save_policy="allowed",
        publish_allowed=False,
        discovery_adapter=None,
    ),
    PlatformSpec(
        cli_name="douyin",
        platform_id="fxg",
        display_name="抖音资料",
        tab_label="抖音资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=False,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter=None,
    ),
    PlatformSpec(
        cli_name="taobao",
        platform_id="tb",
        display_name="淘宝资料",
        tab_label="淘宝资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=False,
        save_policy="explicit_once",
        publish_allowed=True,
        discovery_adapter=None,
    ),
    PlatformSpec(
        cli_name="tmall",
        platform_id="tm",
        display_name="天猫资料",
        tab_label="天猫资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=True,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter="tm_listing:TmallListing",
    ),
    PlatformSpec(
        cli_name="pdd",
        platform_id="pdd",
        display_name="拼多多资料",
        tab_label="拼多多资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=True,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter="pdd_listing:PddListing",
    ),
    PlatformSpec(
        cli_name="wxsph",
        platform_id="wxsph",
        display_name="微信小店（视频号）资料",
        tab_label="微信小店（视频号）资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=True,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter="wxsph_listing:WxsphListing",
    ),
    PlatformSpec(
        cli_name="xhs",
        platform_id="xhs",
        display_name="小红书资料",
        tab_label="小红书资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=True,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter="xhs_listing:XhsListing",
    ),
    PlatformSpec(
        cli_name="youzan",
        platform_id="yz",
        display_name="有赞资料",
        tab_label="有赞资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=False,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter=None,
    ),
    PlatformSpec(
        cli_name="jd",
        platform_id="jd",
        display_name="京东资料",
        tab_label="京东资料",
        lifecycle="implemented",
        enabled_in_all=True,
        supports_inspect=False,
        save_policy="allowed",
        publish_allowed=True,
        discovery_adapter=None,
    ),
)


def platform_cli_choices() -> Tuple[str, ...]:
    return ("all",) + tuple(spec.cli_name for spec in PLATFORM_SPECS)


def get_platform_spec(value: str) -> PlatformSpec:
    for spec in PLATFORM_SPECS:
        if value == spec.cli_name or value == spec.platform_id:
            return spec
    raise ValueError("unknown platform: {0}".format(value))


def canonical_platform_name(value: object) -> str:
    """Return the runner name for either a runner name or persisted platform id."""
    text = str(value or "").strip()
    try:
        return get_platform_spec(text).cli_name
    except ValueError:
        return text


def platforms_equivalent(left: object, right: object) -> bool:
    return canonical_platform_name(left) == canonical_platform_name(right)


def expand_platform_selection(value: str) -> Tuple[PlatformSpec, ...]:
    text = value.strip()
    if text == "all":
        return tuple(spec for spec in PLATFORM_SPECS if spec.enabled_in_all)
    names = tuple(part for part in re.split(r"[,，、\s]+", text) if part)
    if not names:
        raise ValueError("unknown platform: empty selection")
    # Resolve aliases before deduplication; preserve the user's chosen order.
    return tuple(dict.fromkeys(get_platform_spec(name) for name in names))


def normalize_platform_selection(value: str) -> str:
    selected = expand_platform_selection(value)
    if value.strip() == "all":
        return "all"
    return ",".join(spec.cli_name for spec in selected)


def is_all_platform_selection(value: str) -> bool:
    return normalize_platform_selection(value) == "all"


__all__ = [
    "PLATFORM_SPECS",
    "PlatformSpec",
    "canonical_platform_name",
    "expand_platform_selection",
    "get_platform_spec",
    "is_all_platform_selection",
    "normalize_platform_selection",
    "platform_cli_choices",
    "platforms_equivalent",
]
