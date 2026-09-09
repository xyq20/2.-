"""快麦通天猫资料页的安全填写适配器。

这个模块只负责编辑器内的表单填写和回读。首次创建天猫资料时，产品
信息区的蓝色“发布”用于根据已准备的产品图生成后续表单；它只会在
首屏产品图完成天猫顺序替换后点击，绝不点击底部保存或保存并铺货。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from taobao_listing import (
    MaterialComponent,
    TaobaoListing,
    TaobaoListingError,
    excel_aliases,
    normalize_label,
    normalize_option,
    parse_taobao_materials,
    selection_value_groups,
)
from tmall_api_index import TmallApiJsonIndex
from tmall_rules import (
    TmallRuleError,
    new_product_declaration_value,
    normalize_tmall_spec_values,
    shanghai_today,
)


class TmallFormListingError(RuntimeError):
    """可直接向用户展示的天猫资料填写异常。"""


class TmallProductWriteRequired(TmallFormListingError):
    """首次天猫表单需要先准备产品图并点击产品信息“发布”。"""


# 从第一轮就动态检测图位，不再固定空等十几秒。五张图都已
# 回显、解码且来源连续稳定后才允许上传或发布。
TMALL_INITIAL_IMAGE_SETTLE_SECONDS = 0.0
TMALL_INITIAL_IMAGE_STABLE_SECONDS = 1.5


TMALL_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("服装版型"): ("服饰版型",),
    normalize_label("服饰版型"): ("服装版型",),
    normalize_label("弹力等级"): ("弹力",),
    normalize_label("系列"): ("商品系列",),
    normalize_label("商品价格"): ("吊牌价", "价格", "基本售价", "售价"),
    normalize_label("库存"): ("数量", "现货库存"),
    normalize_label("商家外部编码"): ("货号", "款式编码"),
    # Excel 里历史模板有时把页面的“腰型”写成“腰形”，两者只作
    # 明确别名，不做模糊推断。
    normalize_label("腰型"): ("腰形",),
    normalize_label("腰形"): ("腰型",),
}

TMALL_SALES_AND_LOGISTICS_FIELDS: Mapping[str, Tuple[str, ...]] = {
    normalize_label("商品价格"): ("吊牌价", "价格", "基本售价", "售价"),
    normalize_label("商家外部编码"): ("货号", "款式编码"),
    normalize_label("商品物流体积"): (
        "商品物流体积(立方米)",
        "物流体积",
    ),
    normalize_label("商品物流体积(立方米)"): ("商品物流体积", "物流体积"),
    normalize_label("商品物流重量"): (
        "商品物流重量(千克)",
        "物流重量",
    ),
    normalize_label("商品物流重量(千克)"): ("商品物流重量", "物流重量"),
    normalize_label("省份"): (),
    normalize_label("城市"): (),
    normalize_label("提取方式"): (),
    normalize_label("运费承担方式"): (
        "运费承担",
        "运费承担方",
        "邮费承担",
    ),
}

# 天猫页面的物流联动字段不一定会出现在 Excel。页面只有一个可靠的
# 邮寄候选时，先选中它才能让“运费承担方式”控件渲染出来；默认承担方
# 与当前 ERP 页面文案保持一致。Excel 明确提供值时仍优先使用 Excel 值。
TMALL_DEFAULT_SALES_AND_LOGISTICS_VALUES: Mapping[str, str] = {
    normalize_label("提取方式"): "邮寄",
    normalize_label("运费承担方式"): "卖家承担运费",
}


def _fields_mapping(fields: Any) -> Mapping[str, str]:
    value = getattr(fields, "fields", fields)
    if not isinstance(value, Mapping):
        raise TmallFormListingError("天猫 Excel 字段结构无效")
    return value


def _numeric_text_equal(actual: str, expected: str) -> bool:
    try:
        return Decimal(actual.replace(",", "")) == Decimal(
            expected.replace(",", "")
        )
    except InvalidOperation:
        return False


def _tmall_values_equal(page_label: str, actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    if normalize_label(page_label) not in {
        normalize_label("商品价格"),
        normalize_label("吊牌价"),
        normalize_label("价格"),
        normalize_label("基本售价"),
        normalize_label("售价"),
    }:
        return False
    return _numeric_text_equal(actual, expected)


def _field_source(
    fields: Mapping[str, str],
    page_label: str,
    aliases: Sequence[str] = (),
) -> Optional[Tuple[str, str]]:
    wanted = {normalize_label(page_label)}
    wanted.update(normalize_label(value) for value in aliases)
    wanted.update(
        normalize_label(value)
        for value in TMALL_FIELD_ALIASES.get(normalize_label(page_label), ())
    )
    matches = [
        (str(key), str(value).strip())
        for key, value in fields.items()
        if wanted.intersection(excel_aliases(key)) and str(value).strip()
    ]
    if len(matches) > 1:
        distinct = {value for _key, value in matches}
        if len(distinct) == 1:
            return matches[0]
        raise TmallFormListingError(
            f"天猫字段“{page_label}”匹配到多个 Excel 字段："
            + "、".join(key for key, _value in matches)
        )
    return matches[0] if matches else None


def _required_source(
    fields: Mapping[str, str],
    page_label: str,
    aliases: Sequence[str] = (),
) -> str:
    source = _field_source(fields, page_label, aliases)
    if source is None:
        rendered = "/".join((page_label, *aliases))
        raise TmallFormListingError(f"Excel 中缺少天猫字段：{rendered}")
    return source[1]


class TmallFormListing(TaobaoListing):
    """填写天猫编辑器资料，不点击底部保存或保存并铺货。"""

    def __init__(
        self,
        page: Any,
        drawer: Any,
        logger: Any,
        api_index: Optional[TmallApiJsonIndex] = None,
        *,
        attribute_runtime: Optional[Any] = None,
    ) -> None:
        super().__init__(
            page,
            drawer,
            logger,
            attribute_runtime=attribute_runtime,
        )
        self.api_index = api_index

    async def _api_resolved_groups(
        self,
        page_label: str,
        groups: Sequence[Sequence[str]],
    ) -> Tuple[Tuple[str, ...], ...]:
        """用只读接口候选缩小范围；缺少 JSON 时原样回退 DOM。"""
        if self.api_index is None or not groups:
            return tuple(tuple(group) for group in groups)
        await self.api_index.settle(timeout_seconds=0.25)
        resolved = []
        for group in groups:
            option = self.api_index.resolve_option(page_label, group)
            if option is None:
                return tuple(tuple(value) for value in groups)
            resolved.append((option,))
        return tuple(resolved)

    async def _wait_for_loading_masks(self, timeout_seconds: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        raise TmallFormListingError("天猫资料加载遮罩在 30 秒内未消失")

    async def open(self) -> "TmallFormListing":
        tab = self.drawer.get_by_role("tab", name="天猫资料", exact=True)
        if await tab.count() != 1:
            raise TmallFormListingError("找不到唯一的“天猫资料”页签")
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role(
                "tabpanel", name="天猫资料", exact=True
            )
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise TmallFormListingError("天猫资料表单未渲染") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        await self._refresh_shop_authorization_once()
        if self.logger is not None:
            self.logger.info("天猫资料页签已打开")
        return self

    async def _refresh_shop_authorization_once(self) -> None:
        """刷新一次专用浏览器中的店铺授权缓存，不发起绑定流程。"""
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        if "绑定天猫店铺" not in (await self.panel.inner_text()):
            return

        refresh = self.drawer.get_by_text("刷新店铺授权状态", exact=True)
        if await refresh.count() != 1:
            raise TmallFormListingError(
                "当前运行窗口未加载天猫授权店铺，且找不到唯一的"
                "“刷新店铺授权状态”入口"
            )
        if self.logger is not None:
            self.logger.info("专用 Chrome 未显示天猫店铺，正在刷新一次授权状态")
        await refresh.click()

        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            await self._wait_for_loading_masks(timeout_seconds=15)
            panel = self.drawer.get_by_role(
                "tabpanel", name="天猫资料", exact=True
            )
            if await panel.count() == 1 and await panel.is_visible():
                self.panel = panel
                if "绑定天猫店铺" not in (await panel.inner_text()):
                    if self.logger is not None:
                        self.logger.info("天猫店铺授权状态刷新完成")
                    return
            await asyncio.sleep(0.1)
        raise TmallFormListingError(
            "刷新后当前运行窗口仍未加载天猫授权店铺；"
            "请先在自动化专用 Chrome 中完成店铺绑定"
        )

    async def _category_text(self) -> str:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        display = self.panel.locator(".platform-category-input").first
        if not await display.count():
            display = self.panel.locator(".category").first
        if not await display.count():
            panel_text = (await self.panel.inner_text()).strip()
            if "绑定天猫店铺" in panel_text:
                raise TmallFormListingError(
                    "当前运行窗口未加载天猫授权店铺；请先在该窗口刷新店铺授权状态"
                )
            return ""
        explicit = display.locator(".current").first
        if await explicit.count():
            value = (await explicit.inner_text()).strip()
            if value:
                return value
        try:
            direct = await display.evaluate(
                """element => Array.from(element.childNodes)
                  .filter(node => node.nodeType === Node.TEXT_NODE)
                  .map(node => node.textContent || '').join(' ').trim()"""
            )
        except Exception:
            direct = ""
        if str(direct).strip():
            return str(direct).strip()
        text = (await display.inner_text()).strip()
        if "点击使用" in text:
            return ""
        return text.split("修改类目", 1)[0].strip()

    @staticmethod
    def _normalize_category_path(value: object) -> str:
        return re.sub(r"[\s>＞]", "", str(value)).casefold()

    async def apply_recommended_category(self) -> str:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        current = ""
        candidates: List[Tuple[Any, str]] = []
        # ERP 常在保存基础资料后的约 30 秒才重新挂载类目配置与产品字段；
        # 使用实际字段/推荐项作为完成条件，给动态重渲染保留完整 60 秒窗口。
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            current = await self._category_text()
            buttons = self.panel.get_by_role("button", name="点击使用", exact=True)
            candidates = []
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible():
                    continue
                row = button.locator("xpath=..")
                marker = row.get_by_text("推荐", exact=True)
                if not await marker.count() or not await marker.first.is_visible():
                    continue
                row_text = re.sub(r"\s+", " ", (await row.inner_text()).strip())
                expected = re.sub(r"^\s*推荐\s*", "", row_text)
                expected = re.sub(r"\s*点击使用\s*$", "", expected).strip()
                if expected:
                    candidates.append((button, expected))
            if candidates:
                break

            # 保存基础资料后，ERP 可能先回显旧类目文字、再异步生成推荐项。
            # 文字相同并不代表“点击使用”已完成；但已存在类目且“天猫产品
            # 信息”处于折叠态时，ERP 不会再显示推荐项。此时交由
            # fill_product_identity() 的受限“展开”逻辑打开该区块；绝不点击
            # 同页可能存在的蓝色“发布”。
            if current and normalize_label(current) not in {"请选择", "商品分类"}:
                expand_buttons = self.panel.get_by_text("展开", exact=True)
                visible_expand = []
                for index in range(await expand_buttons.count()):
                    button = expand_buttons.nth(index)
                    if await button.is_visible():
                        visible_expand.append(button)
                if len(visible_expand) == 1:
                    return current
                if len(visible_expand) > 1:
                    raise TmallFormListingError(
                        f"天猫产品信息“展开”入口不唯一：{len(visible_expand)}"
                    )
                try:
                    scope = await self._product_identity_scope()
                    await self._tmall_form_item("货号", scope=scope)
                    return current
                except TmallFormListingError as exc:
                    if not str(exc).endswith("：0"):
                        raise
            await asyncio.sleep(0.1)

        if not candidates:
            raise TmallFormListingError("天猫页面没有唯一可用的推荐类目")
        if len(candidates) != 1:
            raise TmallFormListingError(
                f"天猫页面带“推荐”标记的类目不是唯一项：{len(candidates)}"
            )
        button, expected = candidates[0]
        # 即便上方文本已与推荐路径相同，只要“点击使用”仍存在，说明
        # 类目配置尚未激活，必须点击该受限推荐入口一次。
        await button.scroll_into_view_if_needed()
        await button.click()
        self.category_clicked = True
        await self._wait_for_loading_masks()
        deadline = asyncio.get_running_loop().time() + 30
        actual = ""
        while asyncio.get_running_loop().time() < deadline:
            actual = await self._category_text()
            if self._normalize_category_path(actual) == self._normalize_category_path(
                expected
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise TmallFormListingError(
                f"天猫类目应用后校验失败：推荐 {expected!r}，页面为 {actual!r}"
            )
        if self.logger is not None:
            self.logger.info("已应用页面唯一推荐的天猫类目：%s", actual)
        return actual

    async def _item_is_required(self, item: Any, page_label: str) -> bool:
        form_item = item.locator(":scope > .el-form-item")
        classes = (await form_item.get_attribute("class") or "") if await form_item.count() else ""
        if "is-required" in classes.split():
            return True
        label_node = form_item.locator(":scope > .el-form-item__label").first
        label_text = (
            (await label_node.inner_text()).strip() if await label_node.count() else page_label
        )
        return bool(re.match(r"^\s*\*", label_text))

    async def _item_has_value(self, item: Any) -> bool:
        checked = item.locator('input[type="radio"]:checked, input[type="checkbox"]:checked')
        if await checked.count():
            return True
        tags = item.locator(".el-select__tags .el-tag")
        if await tags.count():
            return True
        uploaded = item.locator(
            ".file-img img[src], .el-upload-list__item.is-success img[src]"
        )
        for index in range(await uploaded.count()):
            image = uploaded.nth(index)
            if await image.is_visible() and str(await image.get_attribute("src") or "").strip():
                return True
        rich_editors = item.locator(
            '[contenteditable="true"], .ql-editor, .ProseMirror'
        )
        for index in range(await rich_editors.count()):
            editor = rich_editors.nth(index)
            if not await editor.is_visible():
                continue
            if (await editor.inner_text()).strip() or await editor.locator("img[src]").count():
                return True
        inputs = item.locator('input:not([type="radio"]):not([type="checkbox"]), textarea')
        for index in range(await inputs.count()):
            control = inputs.nth(index)
            if not await control.is_visible():
                continue
            try:
                value = await control.input_value()
            except Exception:
                continue
            if str(value).strip():
                return True
        rich_content = item.locator(
            '[contenteditable="true"], .ql-editor, .rich-text-content'
        )
        for index in range(await rich_content.count()):
            node = rich_content.nth(index)
            if not await node.is_visible():
                continue
            if (await node.inner_text()).strip() or await node.locator("img[src]").count():
                return True
        uploaded = item.locator(
            ".el-upload-list__item.is-success, "
            ".el-upload-list__item-thumbnail, .el-image img[src]"
        )
        for index in range(await uploaded.count()):
            if await uploaded.nth(index).is_visible():
                return True
        return False

    async def _visible_form_items(
        self, scope: Optional[Any] = None
    ) -> Tuple[Tuple[str, Any, bool], ...]:
        root = scope or self.panel
        if root is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        labels = root.locator(".el-form-item > .el-form-item__label")
        records: List[Tuple[str, Any, bool]] = []
        # 真实天猫页会同时渲染几十个表单项。以前每个 label 都通过
        # Playwright 单独读可见性、文本和 class，销售物流阶段会产生数百次
        # CDP 往返。现在一次 DOM 快照读完元数据，仍返回 Playwright locator
        # 执行后续填写；测试替身不支持 evaluate_all 时保留原路径。
        try:
            snapshots = await labels.evaluate_all(
                """nodes => nodes.map((label, index) => {
                  const item = label.parentElement;
                  return {
                    index,
                    visible: Boolean(
                      label.offsetWidth || label.offsetHeight ||
                      label.getClientRects().length
                    ),
                    text: (label.innerText || label.textContent || '').trim(),
                    itemClass: item && typeof item.className === 'string'
                      ? item.className : ''
                  };
                })"""
            )
        except Exception:
            snapshots = None
        if snapshots is not None:
            for snapshot in snapshots:
                if not snapshot.get("visible"):
                    continue
                raw_label = str(snapshot.get("text") or "").strip()
                page_label = re.sub(r"^\s*\*\s*", "", raw_label).strip()
                if not normalize_label(page_label):
                    continue
                item = labels.nth(int(snapshot["index"])).locator("xpath=..")
                classes = str(snapshot.get("itemClass") or "").split()
                required = "is-required" in classes or bool(
                    re.match(r"^\s*\*", raw_label)
                )
                records.append((page_label, item, required))
            return tuple(records)

        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            raw_label = (await label.inner_text()).strip()
            page_label = re.sub(r"^\s*\*\s*", "", raw_label).strip()
            normalized = normalize_label(page_label)
            if not normalized:
                continue
            item = label.locator("xpath=..")
            classes = (await item.get_attribute("class") or "").split()
            required = "is-required" in classes or bool(
                re.match(r"^\s*\*", raw_label)
            )
            records.append((page_label, item, required))
        return tuple(records)

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        """返回天猫商品属性项，并兼容淘宝旧版的 complex-item 结构。"""
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        roots = self.panel.locator(".useCategory-wrap .props-items")
        visible_roots = []
        for index in range(await roots.count()):
            root = roots.nth(index)
            if await root.is_visible():
                visible_roots.append(root)
        if len(visible_roots) != 1:
            if len(visible_roots) > 1:
                raise TmallFormListingError(
                    f"天猫商品属性区域不唯一：{len(visible_roots)}"
                )

        material_labels = self.panel.locator(
            ".others-items .el-form-item > .el-form-item__label"
        )
        visible_material_labels = []
        for index in range(await material_labels.count()):
            label = material_labels.nth(index)
            if (
                await label.is_visible()
                and normalize_label(await label.inner_text())
                == normalize_label("材质成分")
            ):
                visible_material_labels.append(label)
        if not visible_roots and not visible_material_labels:
            return await super()._attribute_items()

        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}

        async def add_label(label: Any, *, direct_item: bool = False) -> None:
            raw = (await label.inner_text()).strip()
            page_label = re.sub(r"^\s*\*\s*", "", raw).replace("重要", "").strip()
            normalized = normalize_label(page_label)
            if not normalized:
                return
            form_item = label.locator("xpath=..")
            item = form_item if direct_item else form_item.locator("xpath=..")
            counts[normalized] = counts.get(normalized, 0) + 1
            key = (
                normalized
                if counts[normalized] == 1
                else f"{normalized}#{counts[normalized]}"
            )
            result[key] = (page_label, item)

        if visible_roots:
            labels = visible_roots[0].locator(
                ".el-form-item > .el-form-item__label"
            )
            for index in range(await labels.count()):
                label = labels.nth(index)
                if await label.is_visible():
                    await add_label(label)

        # 材质成分位于“售后及其他”，但复用同一套结构化材质协议。
        for label in visible_material_labels:
            await add_label(label, direct_item=True)

        if not result:
            raise TmallFormListingError("天猫类目属性区域为空")
        return result

    async def _fill_exact_form_item(
        self, page_label: str, item: Any, expected: object
    ) -> str:
        expected_text = str(expected).strip()
        if not expected_text:
            raise TmallFormListingError(f"天猫字段“{page_label}”期望值为空")

        choice_groups = await self._api_resolved_groups(
            page_label,
            selection_value_groups(page_label, expected_text),
        )
        radio_controls = item.locator('input[type="radio"]')
        visible_radio_count = 0
        for index in range(await radio_controls.count()):
            if await radio_controls.nth(index).is_visible():
                visible_radio_count += 1
        if visible_radio_count:
            if len(choice_groups) != 1:
                rendered = "，".join("/".join(group) for group in choice_groups)
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”是单选，Excel 却提供了"
                    f"多个逗号分组：{rendered}"
                )
            radio = None
            chosen_text = ""
            ambiguous = []
            for candidate in choice_groups[0]:
                radios = item.get_by_role("radio", name=candidate, exact=True)
                visible_radios = []
                for index in range(await radios.count()):
                    candidate_radio = radios.nth(index)
                    if await candidate_radio.is_visible():
                        visible_radios.append(candidate_radio)
                if len(visible_radios) > 1:
                    ambiguous.append(candidate)
                    continue
                if visible_radios:
                    radio = visible_radios[0]
                    chosen_text = candidate
                    break
            if radio is None:
                detail = (
                    f"；重名候选：{'/'.join(ambiguous)}" if ambiguous else ""
                )
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”没有唯一精确候选"
                    f"“{'/'.join(choice_groups[0])}”{detail}"
                )
            if not await radio.is_checked():
                await radio.click()
            if not await radio.is_checked():
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”选择“{chosen_text}”后回读失败"
                )
            return chosen_text

        selects = item.locator(".el-select")
        if await selects.count():
            if await selects.count() != 1:
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”下拉框不唯一"
                )
            # 天猫“提取方式”等字段使用带 tags 的 Element 多选下拉，
            # 即使 Excel 只有一个值，回读也必须从 `.el-select__tags`
            # 读取，而不是从隐藏的单值 input 读取。
            select = selects.first
            select_class = await select.get_attribute("class") or ""
            multi = (
                "is-multiple" in select_class.split()
                or await select.locator(".el-select__tags").count() > 0
            )
            if not multi and len(choice_groups) != 1:
                rendered = "，".join("/".join(group) for group in choice_groups)
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”是单选，Excel 却提供了"
                    f"多个逗号分组：{rendered}"
                )
            try:
                actual = await self._select_values(
                    select,
                    choice_groups,
                    label=page_label,
                    multi=multi,
                )
            except TaobaoListingError as exc:
                raise TmallFormListingError(
                    str(exc).replace("淘宝", "天猫")
                ) from exc
            if actual is None or len(actual) != 1:
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”没有唯一精确候选“{expected_text}”"
                )
            return actual[0]

        inputs = item.locator(
            'input:not([type="radio"]):not([type="checkbox"]):not([disabled]), textarea:not([disabled])'
        )
        visible_inputs = []
        for index in range(await inputs.count()):
            control = inputs.nth(index)
            if await control.is_visible() and not await control.evaluate(
                "element => Boolean(element.closest('.el-select'))"
            ):
                visible_inputs.append(control)
        if len(visible_inputs) != 1:
            raise TmallFormListingError(
                f"天猫字段“{page_label}”找不到唯一可填控件"
            )
        control = visible_inputs[0]
        actual = (await control.input_value()).strip()
        if not _tmall_values_equal(page_label, actual, expected_text):
            if await control.get_attribute("readonly") is not None:
                raise TmallFormListingError(
                    f"天猫字段“{page_label}”为只读且与 Excel 不一致"
                )
            await control.fill(expected_text)
            await control.press("Tab")
            actual = (await control.input_value()).strip()
        if not _tmall_values_equal(page_label, actual, expected_text):
            raise TmallFormListingError(
                f"天猫字段“{page_label}”回读失败：{actual!r}"
            )
        return actual

    async def fill_product_identity(self, fields: Any) -> Mapping[str, Any]:
        source_fields = _fields_mapping(fields)
        specifications = (
            ("货号", ("商家外部编码", "款式编码")),
            ("品牌", ()),
            ("上市年份季节", ("上市季节",)),
        )
        expected = {
            label: _required_source(source_fields, label, aliases)
            for label, aliases in specifications
        }
        await self._expand_product_identity_if_needed()
        # 真实天猫页在类目应用后只先显示“货号”和“品牌”。品牌选中后，
        # “上市年份季节”以及后续完整表单才会动态渲染。因此 Excel 来源仍
        # 一次性预检，但 DOM 必须按页面真实顺序填写和等待。
        first_stage = ("货号", "品牌")
        items = await self._wait_for_product_identity_items(first_stage)
        actual: Dict[str, Any] = {}
        for label in first_stage:
            actual[label] = await self._fill_exact_form_item(
                label, items[label], expected[label]
            )

        await self._wait_for_loading_masks()
        deadline = asyncio.get_running_loop().time() + 30
        season_item = None
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                season_item = await self._tmall_form_item(
                    "上市年份季节", scope=self.panel
                )
                break
            except TmallFormListingError as exc:
                last_error = exc
                if not str(exc).endswith("：0"):
                    raise
            await asyncio.sleep(0.1)
        if season_item is None:
            raise TmallFormListingError(
                "选择天猫品牌后，上市年份季节字段在 30 秒内未渲染"
            ) from last_error
        actual["上市年份季节"] = await self._fill_exact_form_item(
            "上市年份季节",
            season_item,
            expected["上市年份季节"],
        )
        return {"values": actual}

    async def _wait_for_product_identity_items(
        self,
        labels: Sequence[str] = ("货号", "品牌"),
    ) -> Mapping[str, Any]:
        """等待类目激活后异步渲染的首批产品信息字段。"""
        deadline = asyncio.get_running_loop().time() + 60
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            scope = await self._product_identity_scope()
            try:
                return {
                    label: await self._tmall_form_item(label, scope=scope)
                    for label in labels
                }
            except TmallFormListingError as exc:
                last_error = exc
                if not str(exc).endswith("：0"):
                    raise
            await asyncio.sleep(0.1)
        rendered = "、".join(labels)
        raise TmallFormListingError(
            "天猫类目激活后，产品信息字段在 60 秒内未显示：{0}".format(
                rendered
            )
        ) from last_error

    async def _expand_product_identity_if_needed(self) -> None:
        """展开被 ERP 收起的天猫产品信息区，不触发产品“发布”。"""
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        scope = await self._product_identity_scope()
        try:
            await self._tmall_form_item("货号", scope=scope)
            return
        except TmallFormListingError as exc:
            if not str(exc).endswith("：0"):
                raise

        candidates = self.panel.get_by_text("展开", exact=True)
        visible = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        if not visible:
            return
        if len(visible) != 1:
            raise TmallFormListingError(
                f"天猫产品信息“展开”入口不唯一：{len(visible)}"
            )
        await visible[0].click()
        if self.logger is not None:
            self.logger.info("已展开天猫产品信息区域")

        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            await self._wait_for_loading_masks(timeout_seconds=10)
            scope = await self._product_identity_scope()
            try:
                await self._tmall_form_item("货号", scope=scope)
                return
            except TmallFormListingError as exc:
                if not str(exc).endswith("：0"):
                    raise
            await asyncio.sleep(0.1)
        raise TmallFormListingError("展开天猫产品信息后，货号字段在 10 秒内未显示")

    async def _product_identity_scope(self) -> Any:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        candidates = self.panel.locator(".tm-product-info")
        visible = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        if len(visible) > 1:
            raise TmallFormListingError("天猫产品信息区域不唯一")
        if visible:
            return visible[0]

        product_forms = self.panel.locator("form.product-form")
        visible_forms = []
        for index in range(await product_forms.count()):
            candidate = product_forms.nth(index)
            if await candidate.is_visible():
                visible_forms.append(candidate)
        if len(visible_forms) > 1:
            raise TmallFormListingError("天猫产品信息表单不唯一")
        if visible_forms:
            return visible_forms[0]

        labels = self.panel.locator(".wrap-item > .wrap-item_label")
        matches = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if await label.is_visible() and normalize_label(
                await label.inner_text()
            ) == normalize_label("天猫产品信息"):
                matches.append(label.locator("xpath=.."))
        if len(matches) > 1:
            raise TmallFormListingError("天猫产品信息区域不唯一")
        return matches[0] if matches else self.panel

    async def _tmall_form_item(self, label: str, *, scope: Any) -> Any:
        if self.api_index is not None:
            await self.api_index.settle(timeout_seconds=0.25)
            api_matches = []
            for source_id in self.api_index.source_ids(label):
                token = str(source_id).strip()
                if token.startswith("prop_"):
                    token = token[5:]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                    continue
                controls = scope.locator(
                    f'input[name="prop_{token}"], input[name="{token}"]'
                )
                for index in range(await controls.count()):
                    control = controls.nth(index)
                    if not await control.is_visible():
                        continue
                    if await control.evaluate(
                        "element => Boolean(element.closest('.step3-form, .useCategory-wrap'))"
                    ):
                        continue
                    item = control.locator(
                        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]"
                    )
                    if await item.count() == 1:
                        api_matches.append(item)
            if len(api_matches) == 1:
                if self.logger is not None:
                    self.logger.info("天猫字段“%s”已由接口 field id 定位", label)
                return api_matches[0]

        labels = scope.locator(".el-form-item > .el-form-item__label")
        matches = []
        for index in range(await labels.count()):
            candidate = labels.nth(index)
            if await candidate.is_visible() and normalize_label(
                await candidate.inner_text()
            ) == normalize_label(label):
                matches.append(candidate.locator("xpath=.."))
        if len(matches) > 1:
            identity_matches = []
            for match in matches:
                if not await match.evaluate(
                    """element => Boolean(element.closest(
                      '.conf, .step3-form, .useCategory-wrap'))"""
                ):
                    identity_matches.append(match)
            if len(identity_matches) == 1:
                matches = identity_matches
        if len(matches) != 1:
            raise TmallFormListingError(
                f"天猫产品信息字段“{label}”不是唯一项：{len(matches)}"
            )
        return matches[0]

    async def fill_sales_and_logistics(self, fields: Any) -> Mapping[str, Any]:
        source_fields = _fields_mapping(fields)
        targets = []
        missing = []
        preserved_defaults = []
        defaults_applied = []
        seen = set()
        records = await self._visible_form_items()
        for page_label, item, required in records:
            normalized = normalize_label(page_label)
            aliases = TMALL_SALES_AND_LOGISTICS_FIELDS.get(normalized)
            if aliases is None:
                continue
            if normalized in seen:
                raise TmallFormListingError(
                    f"天猫销售物流字段“{page_label}”不唯一"
                )
            seen.add(normalized)
            source = _field_source(source_fields, page_label, aliases)
            default = TMALL_DEFAULT_SALES_AND_LOGISTICS_VALUES.get(normalized)
            expected = source[1] if source is not None else default
            if required and expected is None:
                if normalized in {
                    normalize_label("省份"),
                    normalize_label("城市"),
                } and await self._item_has_value(item):
                    preserved_defaults.append(page_label)
                else:
                    missing.append(page_label)
            if expected is not None:
                targets.append((page_label, item, expected, source is None))
        if missing:
            raise TmallFormListingError(
                "天猫销售物流必填字段缺少 Excel 值："
                + "、".join(dict.fromkeys(missing))
            )

        extraction_label = normalize_label("提取方式")
        freight_label = normalize_label("运费承担方式")
        actual = {}

        async def fill_target(
            page_label: str,
            item: Any,
            expected: str,
            is_default: bool,
        ) -> None:
            actual[page_label] = await self._fill_exact_form_item(
                page_label, item, expected
            )
            if is_default:
                defaults_applied.append(page_label)

        # “运费承担方式”只有在“提取方式”选中后才会被天猫渲染，
        # 所以无论页面原始顺序如何，都先完成提取方式。
        extraction_target = next(
            (
                target
                for target in targets
                if normalize_label(target[0]) == extraction_label
            ),
            None,
        )
        if extraction_target is not None:
            await fill_target(*extraction_target)
            await self._wait_for_loading_masks(timeout_seconds=10)

        async def visible_freight_target() -> Optional[Tuple[str, Any, str, bool]]:
            refreshed = await self._visible_form_items()
            matches = [
                (page_label, item, required)
                for page_label, item, required in refreshed
                if normalize_label(page_label) == freight_label
            ]
            if len(matches) > 1:
                raise TmallFormListingError("天猫销售物流字段“运费承担方式”不唯一")
            if not matches:
                return None
            page_label, item, _required = matches[0]
            # 某些版本先渲染禁用的承担方式控件，再在提取方式的
            # change/nextTick 完成后解除禁用；禁用期间不能尝试点击。
            selects = item.locator(".el-select")
            if await selects.count():
                select = selects.first
                select_class = await select.get_attribute("class") or ""
                input_box = select.locator("input.el-input__inner").first
                if "is-disabled" in select_class.split() or (
                    await input_box.count()
                    and await input_box.get_attribute("disabled") is not None
                ):
                    return None
            else:
                controls = item.locator("input, textarea")
                enabled_controls = []
                for index in range(await controls.count()):
                    control = controls.nth(index)
                    if await control.is_visible() and await control.is_enabled():
                        enabled_controls.append(control)
                if await controls.count() and not enabled_controls:
                    return None
            aliases = TMALL_SALES_AND_LOGISTICS_FIELDS.get(freight_label, ())
            source = _field_source(source_fields, page_label, aliases)
            default = TMALL_DEFAULT_SALES_AND_LOGISTICS_VALUES.get(freight_label)
            expected = source[1] if source is not None else default
            if expected is None:
                raise TmallFormListingError("天猫字段“运费承担方式”缺少 Excel 值")
            return page_label, item, expected, source is None

        # 动态字段通常在一次 nextTick 后出现；短轮询只等待该字段，
        # 不重新扫描或填写其他无关控件。
        freight_target = await visible_freight_target()
        if extraction_target is not None and freight_target is None:
            deadline = asyncio.get_running_loop().time() + 8
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
                freight_target = await visible_freight_target()
                if freight_target is not None:
                    break
        if freight_target is not None:
            await fill_target(*freight_target)

        # 其余初始字段按页面扫描顺序填写。提取方式/运费承担方式已经
        # 单独完成，避免使用动态重渲染前的旧 locator。
        for page_label, item, expected, is_default in targets:
            normalized = normalize_label(page_label)
            if normalized in {extraction_label, freight_label}:
                continue
            await fill_target(page_label, item, expected, is_default)
        return {
            "values": actual,
            "preserved_defaults": tuple(preserved_defaults),
            "defaults_applied": tuple(dict.fromkeys(defaults_applied)),
        }

    async def _after_sales_scope(self) -> Any:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        actual_sections = self.panel.locator(".step3-form > .others-items")
        visible_actual = []
        for index in range(await actual_sections.count()):
            section = actual_sections.nth(index)
            if await section.is_visible():
                visible_actual.append(section)
        if len(visible_actual) > 1:
            raise TmallFormListingError("天猫“售后及其他”区域不唯一")
        if visible_actual:
            return visible_actual[0]
        labels = self.panel.locator(".wrap-item > .wrap-item_label")
        matches = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if await label.is_visible() and normalize_label(
                await label.inner_text()
            ) == normalize_label("售后及其他"):
                matches.append(label.locator("xpath=.."))
        if len(matches) > 1:
            raise TmallFormListingError("天猫“售后及其他”区域不唯一")
        return matches[0] if matches else self.panel

    async def fill_materials(
        self,
        materials: Sequence[MaterialComponent],
    ) -> Tuple[Tuple[str, int], ...]:
        """通过天猫页面的普通 DOM 事件填写材质成分并回读。

        天猫新版材质组件不再暴露淘宝旧页使用的 ``__vue__`` 内部对象，
        因此这里不触碰框架私有状态，只点击页面控件、填写输入框，并以
        页面最终显示值作为校验依据。
        """
        items = await self._attribute_items()
        records = tuple(
            record
            for key, record in items.items()
            if key.split("#", 1)[0] == normalize_label("材质成分")
        )
        if len(records) != 1:
            raise TmallFormListingError(
                f"当前天猫类目中属性“材质成分”匹配数为 {len(records)}"
            )
        _label, item = records[0]
        expected = tuple(
            (material.name, material.percentage) for material in materials
        )
        if not expected:
            raise TmallFormListingError("Excel 中没有可用于天猫材质成分的内容")
        if sum(percentage for _name, percentage in expected) != 100:
            raise TmallFormListingError("天猫材质成分含量合计必须为 100")

        try:
            if await self._read_materials(item) == expected:
                self.material_validation = {
                    "source": "tmall_dom_events",
                    "confirmed": True,
                    "rows": expected,
                }
                return expected

            await item.scroll_into_view_if_needed()
            while await self._material_rows(item):
                rows = await self._material_rows(item)
                remove_buttons = rows[-1].get_by_text(
                    re.compile(r"^\s*移\s*除\s*$")
                )
                if await remove_buttons.count() != 1:
                    raise TmallFormListingError(
                        "天猫材质成分已有内容，但找不到唯一“移除”按钮"
                    )
                before = len(rows)
                await remove_buttons.click()
                deadline = asyncio.get_running_loop().time() + 3
                while asyncio.get_running_loop().time() < deadline:
                    if len(await self._material_rows(item)) < before:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise TmallFormListingError("移除天猫材质成分旧行失败")

            add_button = item.get_by_role("button", name="添加", exact=True)
            if await add_button.count() != 1:
                raise TmallFormListingError(
                    "天猫材质成分找不到唯一“添加”按钮"
                )

            for index, (name, percentage) in enumerate(expected):
                before = len(await self._material_rows(item))
                await add_button.click()
                deadline = asyncio.get_running_loop().time() + 3
                rows = await self._material_rows(item)
                while (
                    len(rows) != before + 1
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.05)
                    rows = await self._material_rows(item)
                if len(rows) != before + 1:
                    raise TmallFormListingError(
                        f"点击“添加”后未生成材质成分第 {index + 1} 行"
                    )

                row = rows[-1]
                selects = row.locator(".el-select")
                if await selects.count() != 1:
                    raise TmallFormListingError(
                        f"天猫材质成分第 {index + 1} 行找不到唯一材质下拉框"
                    )
                actual_name = await self._select_values(
                    selects.first,
                    ((name,),),
                    label=f"材质成分第 {index + 1} 行材质",
                    multi=False,
                )
                if actual_name is None:
                    raise TmallFormListingError(
                        f"天猫材质成分第 {index + 1} 行没有 Excel 材质“{name}”"
                        "的精确候选"
                    )

                percentage_inputs = []
                inputs = row.locator("input")
                for input_index in range(await inputs.count()):
                    candidate = inputs.nth(input_index)
                    if not await candidate.is_visible():
                        continue
                    if await candidate.evaluate(
                        "element => Boolean(element.closest('.el-select'))"
                    ):
                        continue
                    percentage_inputs.append(candidate)
                if len(percentage_inputs) != 1:
                    raise TmallFormListingError(
                        f"天猫材质成分第 {index + 1} 行找不到唯一含量输入框"
                    )
                await percentage_inputs[0].fill(str(percentage))
                await percentage_inputs[0].press("Tab")

                # 复杂材质组件的外层校验监听原生 change；候选点击和输入
                # 回读成功后再显式提交标准 DOM 事件，使“必填”状态同步。
                select_input = selects.first.locator(
                    "input.el-input__inner"
                ).first
                await select_input.dispatch_event("change")
                await percentage_inputs[0].dispatch_event("change")
                await percentage_inputs[0].dispatch_event("blur")

            await asyncio.sleep(0.1)
            for row in await self._material_rows(item):
                inputs = row.locator("input")
                for input_index in range(await inputs.count()):
                    control = inputs.nth(input_index)
                    if not await control.is_visible() or await control.evaluate(
                        "element => Boolean(element.closest('.el-select'))"
                    ):
                        continue
                    current = (await control.input_value()).strip()
                    if re.fullmatch(r"[+-]?\d+\.0+", current):
                        await control.evaluate(
                            """(element, value) => {
                              const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value'
                              ).set;
                              setter.call(element, value);
                            }""",
                            current.split(".", 1)[0],
                        )

            deadline = asyncio.get_running_loop().time() + 5
            actual: Tuple[Tuple[str, int], ...] = ()
            while asyncio.get_running_loop().time() < deadline:
                actual = await self._read_materials(item)
                if actual == expected:
                    self.material_validation = {
                        "source": "tmall_dom_events",
                        "confirmed": True,
                        "rows": actual,
                    }
                    return actual
                await asyncio.sleep(0.05)
            raise TmallFormListingError(
                f"天猫材质成分填写后校验失败：期望 {expected!r}，"
                f"页面为 {actual!r}"
            )
        except TaobaoListingError as exc:
            raise TmallFormListingError(
                str(exc).replace("淘宝", "天猫")
            ) from exc

    async def _read_materials(self, item: Any) -> Tuple[Tuple[str, int], ...]:
        actual = []
        for index, row in enumerate(await self._material_rows(item)):
            select_input = row.locator(
                ".el-select input.el-input__inner"
            ).first
            percentage_box = None
            inputs = row.locator("input")
            for input_index in range(await inputs.count()):
                candidate = inputs.nth(input_index)
                if not await candidate.is_visible() or await candidate.evaluate(
                    "element => Boolean(element.closest('.el-select'))"
                ):
                    continue
                percentage_box = candidate
                break
            if percentage_box is None:
                continue
            name = (await select_input.input_value()).strip()
            percentage_text = (await percentage_box.input_value()).strip()
            if not name and not percentage_text:
                continue
            try:
                percentage_number = Decimal(percentage_text)
            except InvalidOperation as exc:
                raise TmallFormListingError(
                    f"天猫材质成分第 {index + 1} 行含量不是整数："
                    f"{percentage_text!r}"
                ) from exc
            if (
                not percentage_number.is_finite()
                or percentage_number != percentage_number.to_integral_value()
            ):
                raise TmallFormListingError(
                    f"天猫材质成分第 {index + 1} 行含量不是整数："
                    f"{percentage_text!r}"
                )
            actual.append((name, int(percentage_number)))
        return tuple(actual)

    async def fill_after_sales(self, fields: Any) -> Mapping[str, Any]:
        source_fields = _fields_mapping(fields)
        scope = await self._after_sales_scope()
        records = await self._visible_form_items(scope)
        labels = tuple(page_label for page_label, _item, _required in records)
        conditional_value = new_product_declaration_value(labels)
        conditional_label = normalize_label("是否申报新品")
        material_label = normalize_label("材质成分")

        targets = []
        missing = []
        preserved: List[str] = []
        seen = set()
        material_record: Optional[Tuple[str, Any, bool]] = None
        for page_label, item, required in records:
            normalized = normalize_label(page_label)
            if normalized == conditional_label:
                continue
            if await item.evaluate(
                "element => Boolean(element.closest('.multi-complex-items'))"
            ):
                # 已有材质行时，“材质/含量”子控件也会被扫描为
                # el-form-item；它们由结构化材质逻辑统一处理。
                continue
            if normalized in seen:
                raise TmallFormListingError(
                    f"天猫售后字段“{page_label}”不唯一"
                )
            seen.add(normalized)
            if normalized == material_label:
                material_record = (page_label, item, required)
                continue
            source = _field_source(source_fields, page_label)
            if required and source is None:
                if await self._item_has_value(item):
                    preserved.append(page_label)
                else:
                    missing.append(page_label)
            if source is not None:
                targets.append((page_label, item, source[1]))

        materials = ()
        material_compatible = False
        if material_record is not None:
            page_label, material_item, required = material_record
            try:
                materials = parse_taobao_materials(source_fields)
            except TaobaoListingError as exc:
                raise TmallFormListingError(
                    str(exc).replace("淘宝", "天猫")
                ) from exc

            if materials:
                material_compatible = bool(
                    await material_item.evaluate(
                        """element => {
                          const host = element.closest(
                            '.complex-item_multi, .complex-item');
                          const legacy = host && host.closest('.conf');
                          const current = element.matches('.el-form-item')
                            && element.closest('.others-items');
                          return Boolean((legacy || current)
                            && element.querySelector('.multi-complex-items'));
                        }"""
                    )
                )
                if material_compatible:
                    try:
                        page_items = await self._attribute_items()
                    except TaobaoListingError as exc:
                        raise TmallFormListingError(
                            str(exc).replace("淘宝", "天猫")
                        ) from exc
                    material_items = tuple(
                        record
                        for key, record in page_items.items()
                        if key.split("#", 1)[0] == material_label
                    )
                    material_compatible = len(material_items) == 1
                if not material_compatible:
                    raise TmallFormListingError(
                        "天猫材质成分组件与淘宝结构化填写协议不兼容，"
                        "已在点击“添加”前停止"
                    )
            elif required:
                if await self._item_has_value(material_item):
                    preserved.append(page_label)
                else:
                    missing.append(page_label)
        if missing:
            raise TmallFormListingError(
                "天猫售后必填字段缺少 Excel 值："
                + "、".join(dict.fromkeys(missing))
            )
        actual_materials: Tuple[Tuple[str, int], ...] = ()
        if materials and material_compatible:
            try:
                actual_materials = await self.fill_materials(materials)
            except TaobaoListingError as exc:
                raise TmallFormListingError(
                    str(exc).replace("淘宝", "天猫")
                ) from exc

        # 添加/移除材质行会让 others-items 整体重渲染，并可能重置同区块
        # 的普通字段。因此材质先处理，随后重新定位并填写发布类型等字段。
        refreshed_scope = await self._after_sales_scope()
        refreshed_items: Dict[str, List[Any]] = {}
        for page_label, item, _required in await self._visible_form_items(
            refreshed_scope
        ):
            refreshed_items.setdefault(normalize_label(page_label), []).append(item)
        actual = {}
        for page_label, _old_item, expected in targets:
            matches = refreshed_items.get(normalize_label(page_label), ())
            if len(matches) != 1:
                raise TmallFormListingError(
                    f"天猫售后字段“{page_label}”重渲染后匹配数为 {len(matches)}"
                )
            actual[page_label] = await self._fill_exact_form_item(
                page_label, matches[0], expected
            )
        declaration = None
        if conditional_value is not None:
            declaration = await self.fill_new_product_declaration()
            if declaration != conditional_value:
                raise TmallFormListingError(
                    "天猫“是否申报新品”未精确选中“是”"
                )
        return {
            "values": actual,
            "preserved": tuple(dict.fromkeys(preserved)),
            "materials": actual_materials,
            "new_product_declaration": declaration,
        }

    async def validate_remaining_required_fields(self) -> Mapping[str, Any]:
        """只读校验天猫页面仍为空的可见必填项。

        不会点击“导入PC描述”、“从素材空间上传”或任何其他按钮。
        """
        missing = []
        for page_label, item, required in await self._visible_form_items():
            if required and not await self._item_has_value(item):
                missing.append(page_label)
        missing_labels = tuple(dict.fromkeys(missing))
        if missing_labels:
            raise TmallFormListingError(
                "天猫页面仍有未填写的可见必填字段："
                + "、".join(missing_labels)
            )
        return {"valid": True, "missing": ()}

    async def fill_attribute(
        self,
        label: str,
        expected: object,
        *,
        exact_values: Optional[Sequence[str]] = None,
        item: Optional[Any] = None,
    ) -> Optional[Tuple[str, ...]]:
        """用接口 JSON 选定精确候选，再由 DOM 完成点击和回读。"""
        if normalize_label(label) == normalize_label("吊牌价"):
            if item is None:
                items = await self._attribute_items()
                records = [
                    record
                    for key, record in items.items()
                    if key.split("#", 1)[0] == normalize_label(label)
                ]
                if len(records) != 1:
                    raise TmallFormListingError(
                        f"当前天猫类目中属性“{label}”匹配数为 {len(records)}"
                    )
                _page_label, item = records[0]
            number_text = str(expected).strip()
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:元)?", number_text)
            if match is None:
                raise TmallFormListingError(
                    f"天猫吊牌价不是有效金额：{expected!r}"
                )
            amount = Decimal(match.group(1))
            if amount != amount.to_integral_value():
                raise TmallFormListingError("天猫吊牌价必须是整数")
            expected_with_unit = f"{int(amount)}元"
            inputs = item.locator(
                ":scope > .el-form-item > .el-form-item__content "
                "input:not([readonly])"
            )
            visible_inputs = []
            for index in range(await inputs.count()):
                candidate = inputs.nth(index)
                if await candidate.is_visible() and not await candidate.evaluate(
                    "element => Boolean(element.closest('.el-select'))"
                ):
                    visible_inputs.append(candidate)
            if len(visible_inputs) != 1:
                raise TmallFormListingError("天猫吊牌价找不到唯一数值输入框")
            input_box = visible_inputs[0]
            if (await input_box.input_value()).strip() != expected_with_unit:
                await input_box.fill(expected_with_unit)
                await input_box.press("Tab")
            actual = (await input_box.input_value()).strip()
            if actual != expected_with_unit:
                raise TmallFormListingError(
                    "天猫吊牌价填写后校验失败："
                    f"期望 {expected_with_unit!r}，页面为 {actual!r}"
                )
            return (actual,)

        groups = (
            tuple((str(value),) for value in exact_values)
            if exact_values is not None
            else selection_value_groups(label, expected)
        )
        resolved_groups = await self._api_resolved_groups(label, groups)
        resolved_values = (
            tuple(group[0] for group in resolved_groups)
            if all(len(group) == 1 for group in resolved_groups)
            else None
        )
        try:
            return await super().fill_attribute(
                label,
                expected,
                exact_values=resolved_values,
                item=item,
            )
        except TaobaoListingError as exc:
            raise TmallFormListingError(
                str(exc).replace("淘宝", "天猫")
            ) from exc

    async def fill_attributes(self, fields: Any) -> Mapping[str, Any]:
        source_fields = _fields_mapping(fields)
        try:
            page_items = await self._attribute_items()
        except TaobaoListingError as exc:
            raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc

        targets: List[
            Tuple[
                str,
                str,
                Any,
                Optional[Tuple[str, str]],
                bool,
                Optional[Tuple[str, ...]],
            ]
        ] = []
        missing: List[str] = []
        deferred: List[str] = []
        material_label = normalize_label("材质成分")
        for key, (page_label, item) in page_items.items():
            if key.split("#", 1)[0] == material_label:
                if page_label not in deferred:
                    deferred.append(page_label)
                continue
            required = await self._item_is_required(item, page_label)
            hang_price = normalize_label(page_label) == normalize_label("吊牌价")
            waist_shape = normalize_label(page_label) in {
                normalize_label("腰型"),
                normalize_label("腰形"),
            }
            if not required and not hang_price and not waist_shape:
                continue
            source = _field_source(source_fields, page_label)
            if required and source is None:
                missing.append(page_label)
            exact_values = None
            if (
                source is not None
                and normalize_label(page_label) == normalize_label("面料")
            ):
                try:
                    materials = parse_taobao_materials(source_fields)
                except TaobaoListingError as exc:
                    raise TmallFormListingError(
                        str(exc).replace("淘宝", "天猫")
                    ) from exc
                if materials:
                    exact_values = tuple(
                        dict.fromkeys(material.name for material in materials)
                    )
            targets.append(
                (key, page_label, item, source, required, exact_values)
            )
        if missing:
            raise TmallFormListingError(
                "天猫重要必填属性缺少 Excel 值：" + "、".join(dict.fromkeys(missing))
            )

        applied: Dict[str, Tuple[str, ...]] = {}
        for _key, page_label, item, source, required, exact_values in targets:
            if source is None:
                continue
            try:
                actual = await self.fill_attribute(
                    page_label,
                    source[1],
                    exact_values=exact_values,
                    item=item,
                )
            except TaobaoListingError as exc:
                raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc
            if actual is None:
                if required:
                    raise TmallFormListingError(
                        f"天猫重要属性“{page_label}”没有唯一精确候选"
                    )
                continue
            applied[page_label] = tuple(actual)
        return {
            "attributes": applied,
            "deferred": tuple(deferred),
            "required": tuple(
                page_label
                for _key, page_label, _item, _source, required, _exact in targets
                if required
            ),
        }

    async def _initial_product_image_slots(self) -> Tuple[Tuple[str, Any], ...]:
        """定位首次天猫资料页产品信息区的五个独立产品图位。"""
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        # 实际产品图片有独立容器，不能限定在货号/品牌的 identity form 内。
        containers = self.panel.locator(".product_images:visible")
        count = await containers.count()
        if count > 1:
            raise TmallFormListingError("天猫产品图片区域不唯一")
        scope = containers.first if count == 1 else await self._product_identity_scope()
        expected_labels = (
            "产品主图",
            "产品图片2",
            "产品图片3",
            "产品图片4",
            "产品图片5",
        )
        expected_by_normalized = {
            normalize_label(label): label for label in expected_labels
        }
        found: Dict[str, Any] = {}
        items = scope.locator(".el-form-item.pic")
        for index in range(await items.count()):
            item = items.nth(index)
            if not await item.is_visible():
                continue
            labels = item.locator(":scope > .el-form-item__label")
            if await labels.count() != 1:
                continue
            label = expected_by_normalized.get(
                normalize_label(await labels.first.inner_text())
            )
            if label is None:
                continue
            if label in found:
                raise TmallFormListingError(f"天猫首次产品图片位“{label}”不唯一")
            found[label] = item
        return tuple((label, found[label]) for label in expected_labels if label in found)

    async def wait_for_initial_product_images(
        self,
        *,
        expected_count: int,
        timeout_seconds: float = 60,
        initial_delay_seconds: float = TMALL_INITIAL_IMAGE_SETTLE_SECONDS,
        stable_seconds: float = TMALL_INITIAL_IMAGE_STABLE_SECONDS,
        allow_existing_product_shortcut: bool = True,
    ) -> Mapping[str, Any]:
        """等待天猫页顶部按基础资料实际图片数回显并保持稳定。"""
        expected_count = int(expected_count)
        if expected_count < 1 or expected_count > 5:
            raise TmallFormListingError(
                f"天猫顶部产品图片数量必须为 1-5 张，当前为 {expected_count} 张"
            )
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + timeout_seconds
        initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        stable_seconds = max(0.0, float(stable_seconds))
        if self.logger is not None:
            self.logger.info(
                "等待天猫产品图片回显：基础资料有 %s 张；先静置 %.1f 秒，"
                "全部图片连续稳定 %.1f 秒后才允许发布",
                expected_count,
                initial_delay_seconds,
                stable_seconds,
            )

        # 选择上市季节后，快麦会异步把基础资料图片复到天猫首屏。
        # 默认使用 0.5 秒的低频轮询追踪真实加载状态；只在调用方
        # 明确传入延迟时才先静置。
        remaining = deadline - loop.time()
        if initial_delay_seconds:
            await asyncio.sleep(min(initial_delay_seconds, max(0.0, remaining)))

        last_loaded = 0
        stable_since: Optional[float] = None
        stable_sources: Tuple[str, ...] = ()
        last_progress_log = 0.0
        while loop.time() < deadline:
            slots = await self._initial_product_image_slots()
            loaded = 0
            sources: List[str] = []
            if len(slots) >= expected_count:
                for _label, item in slots[:expected_count]:
                    images = item.locator(".sc-upload .file-img img.originImg[src]")
                    if await images.count() == 1 and await images.first.is_visible():
                        source = str(await images.first.get_attribute("src") or "").strip()
                        decoded = bool(
                            await images.first.evaluate(
                                """image => Boolean(
                                    image.complete &&
                                    image.naturalWidth > 0 &&
                                    image.naturalHeight > 0
                                )"""
                            )
                        )
                        if source and decoded:
                            loaded += 1
                            sources.append(source)
            last_loaded = loaded
            # 快麦 renderUI 会异步 matchProductSchema，命中既有产品后
            # 自动隐藏发布入口、回填旧产品图并生成完整表单。不是同步完成，
            # 也不能再等待首次继承；仅有明确接口证据和完整 DOM 才切分支。
            if allow_existing_product_shortcut and loaded != expected_count and getattr(
                self.api_index, "matched_existing_product", False
            ) is True:
                try:
                    await self.require_full_form()
                except TmallFormListingError:
                    pass
                else:
                    if self.logger is not None:
                        self.logger.info("天猫等待期间匹配既有产品：停止首次继承等待，改为校正本地产品图，不重复发布")
                    return {
                        "count": loaded, "expected_count": expected_count,
                        "matched_existing_product": True,
                        "decoded": False,
                        "waited_seconds": round(loop.time() - started_at, 1),
                    }
            if loaded == expected_count:
                source_fingerprint = tuple(sources)
                if source_fingerprint != stable_sources:
                    stable_sources = source_fingerprint
                    stable_since = loop.time()
                stable_for = loop.time() - (stable_since or loop.time())
                if stable_for >= stable_seconds:
                    if self.logger is not None:
                        self.logger.info(
                            "天猫顶部产品图片同步完成：%s 张，已连续稳定 %.1f 秒",
                            loaded,
                            stable_for,
                        )
                    return {
                        "count": loaded,
                        "expected_count": expected_count,
                        "labels": tuple(
                            label for label, _ in slots[:expected_count]
                        ),
                        "decoded": True,
                        "stable_seconds": round(stable_for, 1),
                        "waited_seconds": round(loop.time() - started_at, 1),
                    }
            else:
                stable_since = None
                stable_sources = ()

            if self.logger is not None and loop.time() - last_progress_log >= 5:
                self.logger.info(
                    "天猫产品图片仍在同步：已加载 %s/%s 张；未完成前不会点击发布",
                    last_loaded,
                    expected_count,
                )
                last_progress_log = loop.time()
            await asyncio.sleep(0.5)
        raise TmallFormListingError(
            "天猫顶部产品图片在 {0:g} 秒内未回显并稳定完成（已加载 {1}/{2} 张）；"
            "为避免提前发布，程序已停止".format(
                timeout_seconds,
                last_loaded,
                expected_count,
            )
        )

    async def inspect_initial_product_images(
        self, *, expected_count: int
    ) -> Mapping[str, Any]:
        """立即回读顶部产品图片，供首次和重复打开共用完整性判断。"""
        expected_count = int(expected_count)
        if expected_count < 1 or expected_count > 5:
            raise TmallFormListingError(
                f"天猫产品图片数量必须为 1-5 张，当前为 {expected_count} 张"
            )
        slots = await self._initial_product_image_slots()
        loaded = 0
        labels: List[str] = []
        sources: List[str] = []
        for label, item in slots[:expected_count]:
            images = item.locator(".sc-upload .file-img img.originImg[src]")
            if await images.count() != 1 or not await images.first.is_visible():
                continue
            source = str(await images.first.get_attribute("src") or "").strip()
            decoded = bool(
                await images.first.evaluate(
                    """image => Boolean(
                        image.complete &&
                        image.naturalWidth > 0 &&
                        image.naturalHeight > 0
                    )"""
                )
            )
            if source and decoded:
                loaded += 1
                labels.append(label)
                sources.append(source)
        return {
            "count": loaded,
            "expected_count": expected_count,
            "slot_count": len(slots),
            "labels": tuple(labels),
            "sources": tuple(sources),
            "decoded": loaded == expected_count,
        }

    async def sync_initial_product_images(
        self,
        paths: Sequence[Any],
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, Any]:
        """首次创建时按本次实际图片数替换产品信息区图片。"""
        original = tuple(paths)
        if not original or len(original) > 5:
            raise TmallFormListingError(
                f"天猫首次产品图片需要 1-5 张本地图片，当前为 {len(original)} 张"
            )
        slots = await self._initial_product_image_slots()
        if len(slots) < len(original):
            raise TmallFormListingError(
                "天猫首次产品图片位不完整：页面为 {0}/{1} 个".format(
                    len(slots),
                    len(original),
                )
            )
        ordered = self._tmall_main_image_order(original)
        actions: Dict[str, str] = {}
        for index, (page_label, item) in enumerate(slots[: len(original)]):
            label = "天猫" + page_label
            actions[page_label] = await uploader(
                self.page,
                item,
                (ordered[index],),
                label,
                timeout_seconds,
                force_replace=True,
            )
        return {"count": len(actions), "actions": actions}

    async def publish_product_information(
        self, *, timeout_seconds: float = 30, expected_image_count: int = 5
    ) -> Mapping[str, Any]:
        """点击首次资料区唯一“发布”，并等待其生成完整天猫表单。"""
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        candidates = self.panel.get_by_role("button", name="发布", exact=True)
        visible = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        if not visible:
            await self.require_full_form()
            return {"clicked": False, "already_ready": True}
        if len(visible) != 1:
            raise TmallFormListingError(
                f"天猫首次产品信息“发布”入口不唯一：{len(visible)}"
            )
        # 上传会再次异步回显；即使上游完成首次同步检查，点击前仍要复核。
        readiness = await self.wait_for_initial_product_images(
            expected_count=expected_image_count,
            timeout_seconds=timeout_seconds,
            initial_delay_seconds=0,
        )
        if isinstance(readiness, Mapping) and readiness.get("matched_existing_product") is True:
            raise TmallFormListingError("发布前页面已匹配既有产品，禁止继续点击首次发布")
        await visible[0].scroll_into_view_if_needed()
        await visible[0].click()
        if self.logger is not None:
            self.logger.info("已点击天猫首次产品信息“发布”，等待完整表单生成")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        last_error: Optional[BaseException] = None
        last_progress_log = 0.0
        while loop.time() < deadline:
            loading_masks = await self.drawer.locator(
                ".el-loading-mask:visible"
            ).count()
            if loading_masks:
                if self.logger is not None and loop.time() - last_progress_log >= 3:
                    self.logger.info(
                        "天猫下方表单仍在生成：页面加载中，将继续等待"
                    )
                    last_progress_log = loop.time()
                await asyncio.sleep(0.2)
                continue
            try:
                await self.require_full_form()
                return {"clicked": True, "already_ready": False}
            except (TmallProductWriteRequired, TmallFormListingError) as exc:
                last_error = exc
            if self.logger is not None and loop.time() - last_progress_log >= 3:
                self.logger.info(
                    "天猫下方表单仍在生成：必要区域尚未全部渲染"
                )
                last_progress_log = loop.time()
            await asyncio.sleep(0.2)
        raise TmallFormListingError(
            "点击天猫首次产品信息“发布”后，完整表单在 {0:g} 秒内未生成".format(
                timeout_seconds
            )
        ) from last_error

    async def require_full_form(self) -> None:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        # 首次页面可能已渲染下方字段。可见的产品“发布”优先于完整表单判断。
        publish_buttons = self.panel.get_by_role("button", name="发布", exact=True)
        for index in range(await publish_buttons.count()):
            if await publish_buttons.nth(index).is_visible():
                raise TmallProductWriteRequired(
                    "天猫产品信息待发布：必须先等待产品图片同步完成"
                )
        required_sections = {
            normalize_label(value)
            for value in ("商品明细", "物流信息", "商品描述", "售后及其他")
        }
        size_sections = {normalize_label("码表"), normalize_label("尺码表")}
        labels = self.panel.locator(
            ".wrap-item > .wrap-item_label, .step3-form > .title"
        )
        visible_sections = set()
        for index in range(await labels.count()):
            label = labels.nth(index)
            if await label.is_visible():
                visible_sections.add(normalize_label(await label.inner_text()))
        sku_rows = self.panel.locator(".sku-batch-row")
        has_sku_row = any(
            [await sku_rows.nth(index).is_visible() for index in range(await sku_rows.count())]
        )
        has_size_table = False
        try:
            size_container = await self._size_chart_container()
            size_table = await self._size_chart_table(size_container)
            has_size_table = await size_table.is_visible()
        except TmallFormListingError:
            pass
        if not has_size_table:
            legacy_size_tables = self.panel.locator(".tmall-size-table")
            has_size_table = any(
                [
                    await legacy_size_tables.nth(index).is_visible()
                    for index in range(await legacy_size_tables.count())
                ]
            )
        if (
            required_sections.issubset(visible_sections)
            and bool(size_sections.intersection(visible_sections))
            and has_sku_row
            and has_size_table
        ):
            return
        raise TmallFormListingError("天猫类目已选择，但后续完整表单尚未渲染")

    async def normalize_synced_specifications(
        self, category_path: str
    ) -> Mapping[str, Any]:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        category_parts = tuple(
            part.strip()
            for part in re.split(r"[>\uff1e/\uff0f\u2192\u00bb]+", str(category_path))
            if part.strip()
        )
        category_leaf = category_parts[-1] if category_parts else ""
        is_footwear = category_leaf.endswith(("鞋", "靴", "鞋子", "靴子"))
        titles = self.panel.locator(".block-specification .title-bg")
        plans: List[Tuple[str, Tuple[Any, ...], Tuple[str, ...], Tuple[str, ...]]] = []
        for index in range(await titles.count()):
            title = titles.nth(index)
            title_input = title.locator("input").first
            dimension = (
                (await title_input.input_value()).strip()
                if await title_input.count()
                else (await title.inner_text()).strip()
            )
            value_groups = title.locator(
                "xpath=following-sibling::*[contains(concat(' ', "
                "normalize-space(@class), ' '), ' specification-value ')][1]"
            )
            if await value_groups.count() != 1:
                continue
            inputs = value_groups.first.locator("input.spec-value")
            controls = tuple(inputs.nth(item) for item in range(await inputs.count()))
            before = tuple([await control.input_value() for control in controls])
            dimension_key = normalize_label(dimension)
            is_size_dimension = (
                "尺码" in dimension_key or dimension_key in {"鞋码", "码数"}
            )
            try:
                after = normalize_tmall_spec_values(
                    before,
                    is_footwear=is_footwear,
                    is_size_dimension=is_size_dimension,
                )
            except TmallRuleError as exc:
                raise TmallFormListingError(str(exc)) from exc
            plans.append((dimension, controls, before, after))

        for dimension, controls, before, after in plans:
            for control, old_value, new_value in zip(controls, before, after):
                if old_value == new_value:
                    continue
                if not await control.is_visible() or not await control.is_editable():
                    raise TmallFormListingError(
                        f"天猫规格“{dimension}”存在不可写的鞋码控件，已在修改前停止"
                    )

        changed = 0
        dimensions: Dict[str, Tuple[str, ...]] = {}
        for dimension, controls, before, after in plans:
            for control, old_value, new_value in zip(controls, before, after):
                if old_value == new_value:
                    continue
                await control.fill(new_value)
                await control.press("Tab")
                actual = await control.input_value()
                if actual != new_value:
                    raise TmallFormListingError(
                        f"天猫规格“{dimension}”鞋码清理后回读失败：{actual!r}"
                    )
                changed += 1
            dimensions[dimension] = after
        return {"changed": changed, "dimensions": dimensions}

    async def _fill_batch_date(self, row: Any, expected: str) -> str:
        try:
            item = await self._sku_batch_item(row, "上市时间")
        except TaobaoListingError as exc:
            raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc
        inputs = item.locator("input.el-input__inner, input")
        if await inputs.count() != 1:
            raise TmallFormListingError("天猫 SKU 上市时间日期输入框不唯一")
        input_box = inputs.first
        readonly = await input_box.get_attribute("readonly")
        if readonly is None:
            await input_box.fill(expected)
            await input_box.press("Tab")
        else:
            await input_box.click()
            cells = self.page.locator(
                ".el-picker-panel:visible td.today.available:not(.disabled), "
                ".el-date-picker:visible td.today.available:not(.disabled)"
            )
            visible_cells = []
            for index in range(await cells.count()):
                if await cells.nth(index).is_visible():
                    visible_cells.append(cells.nth(index))
            if len(visible_cells) != 1:
                raise TmallFormListingError(
                    f"天猫上市时间日期表中的当天单元格不唯一：{len(visible_cells)}"
                )
            await visible_cells[0].click()
        actual = (await input_box.input_value()).strip()
        if re.sub(r"\D", "", actual) != re.sub(r"\D", "", expected):
            raise TmallFormListingError(
                f"天猫 SKU 上市时间回读失败：期望 {expected!r}，页面为 {actual!r}"
            )
        return actual

    def _validate_tmall_sku_snapshot(
        self,
        snapshot: Mapping[str, Any],
        expected: Mapping[str, str],
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = snapshot.get("rows", ())
        if not raw_rows:
            raise TmallFormListingError("天猫 SKU 表格没有可校验的明细行")
        try:
            indices = {
                label: self._sku_column_index(headers, label) for label in expected
            }
        except TaobaoListingError as exc:
            raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc
        rows: List[Mapping[str, str]] = []
        errors: List[str] = []
        for row_number, raw_row in enumerate(raw_rows, 1):
            raw_values = tuple(str(value) for value in raw_row)
            values = {label: raw_values[index] for label, index in indices.items()}
            for label, wanted in expected.items():
                actual = values[label]
                if label in {"价格", "库存"}:
                    matches = self._numeric_equal(actual, wanted)
                elif label == "上市时间":
                    matches = re.sub(r"\D", "", actual) == re.sub(r"\D", "", wanted)
                else:
                    matches = normalize_option(actual) == normalize_option(wanted)
                if not matches:
                    errors.append(f"第{row_number}行{label}={actual!r}")
            rows.append(values)
        if errors:
            raise TmallFormListingError(
                "天猫 SKU 批量设置后校验失败：" + "；".join(errors[:12])
            )
        return tuple(rows)

    async def fill_sku_batch(
        self,
        fields: Mapping[str, str],
        *,
        today: Optional[date] = None,
    ) -> Mapping[str, Any]:
        source_fields = _fields_mapping(fields)
        price = _required_source(
            source_fields,
            "价格",
            ("商品价格", "吊牌价", "基本售价", "售价"),
        )
        quantity_source = _field_source(source_fields, "数量")
        stock = (
            quantity_source[1]
            if quantity_source is not None
            else _required_source(source_fields, "现货库存", ("库存",))
        )
        code = _required_source(source_fields, "货号", ("商家外部编码", "款式编码"))
        if not re.fullmatch(r"\d+(?:\.\d+)?", price):
            raise TmallFormListingError(f"Excel 天猫 SKU 价格不是有效数字：{price!r}")
        if not re.fullmatch(r"\d+", stock):
            raise TmallFormListingError(f"Excel 天猫 SKU 库存不是整数：{stock!r}")
        today_value = (today or shanghai_today()).isoformat()
        try:
            details = await self._wrap_item("商品明细")
            row = await self._sku_batch_row(details)
            before = await self._sku_table_snapshot(details)
            platform_index = self._sku_column_index(
                before.get("headers", ()), "平台规格编码"
            )
            platform_codes_before = tuple(
                str(values[platform_index]) for values in before.get("rows", ())
            )
            await self._fill_batch_text(row, "价格", price)
            await self._fill_batch_text(row, "库存", stock)
            await self._fill_batch_date(row, today_value)
            await self._fill_batch_text(row, "货号", code)
        except TaobaoListingError as exc:
            raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc

        dynamic: Dict[str, str] = {}
        fixed = {"价格", "库存", "上市时间", "货号", "平台规格编码", "条形码"}
        items = row.locator(":scope > .sku-batch-item")
        for index in range(await items.count()):
            item = items.nth(index)
            label_node = item.locator(":scope > .sku-batch-item_label")
            if not await label_node.count():
                continue
            label = (await label_node.inner_text()).strip().rstrip("：:").strip()
            if not label or label in fixed:
                continue
            source = _field_source(source_fields, label)
            if source is None:
                continue
            if await item.locator(".el-select").count() == 1:
                groups = selection_value_groups(label, source[1])
                if len(groups) != 1:
                    raise TmallFormListingError(
                        f"天猫 SKU 字段“{label}”是单选，但 Excel 不是单值"
                    )
                try:
                    dynamic[label] = await self._fill_batch_select(
                        row, label, groups[0]
                    )
                except TaobaoListingError as exc:
                    raise TmallFormListingError(
                        str(exc).replace("淘宝", "天猫")
                    ) from exc
            else:
                try:
                    dynamic[label] = await self._fill_batch_text(
                        row, label, source[1]
                    )
                except TaobaoListingError as exc:
                    raise TmallFormListingError(
                        str(exc).replace("淘宝", "天猫")
                    ) from exc

        button = row.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise TmallFormListingError("天猫 SKU 批量设置按钮不唯一")
        await button.click()
        expected = {
            "价格": price,
            "库存": stock,
            "上市时间": today_value,
            "货号": code,
            **dynamic,
        }
        deadline = asyncio.get_running_loop().time() + 8
        last_snapshot: Mapping[str, Any] = {}
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            last_snapshot = await self._sku_table_snapshot(details)
            try:
                rows = self._validate_tmall_sku_snapshot(last_snapshot, expected)
                break
            except TmallFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        else:
            raise TmallFormListingError(str(last_error or "天猫 SKU 批量设置超时"))
        try:
            after_platform_index = self._sku_column_index(
                last_snapshot.get("headers", ()), "平台规格编码"
            )
        except TaobaoListingError as exc:
            raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc
        platform_codes_after = tuple(
            str(values[after_platform_index])
            for values in last_snapshot.get("rows", ())
        )
        if platform_codes_after != platform_codes_before:
            raise TmallFormListingError("天猫批量设置意外改动了平台规格编码")
        return {
            "batch_clicked": True,
            "row_count": len(rows),
            "values": expected,
            "rows": rows,
            "platform_codes_preserved": True,
            "platform_codes": platform_codes_after,
        }

    async def _size_chart_container(self) -> Any:
        for label in ("码表", "尺码表"):
            try:
                return await self._wrap_item(label)
            except TaobaoListingError:
                continue
        if self.panel is not None:
            tables = self.panel.locator(".tmall-size-table")
            if await tables.count() == 1:
                return tables.first.locator("xpath=..")
        raise TmallFormListingError("天猫页面找不到唯一尺码表区域")

    @staticmethod
    def _row_sources(row: Mapping[str, Any]) -> Mapping[str, Tuple[str, Any]]:
        result: Dict[str, Tuple[str, Any]] = {}
        for key, value in row.items():
            normalized = normalize_label(key)
            if normalized:
                result[normalized] = (str(key), value)
        return result

    @staticmethod
    def _size_header_name(value: object) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip().lstrip("* ")
        text = re.sub(r"(?:↔\s*)?区间\s*$", "", text).strip()
        # 天猫部分类目的真实表头会把单位列显示为“身高（cm） 值”。
        # “值”只是控件展示后缀，不属于 Excel 字段名；仅在单位括号后
        # 剥离，避免误伤本身以“值”结尾的业务字段。
        return re.sub(r"([)）])\s*值\s*$", r"\1", text).strip()

    @staticmethod
    def _size_value_parts(value: Any, *, size: str, label: str) -> Tuple[str, ...]:
        def display_text(item: Any) -> str:
            text = str(item).strip()
            try:
                number = Decimal(text)
            except InvalidOperation:
                return text
            if not number.is_finite():
                return text
            rendered = format(number, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            return rendered

        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                raise TmallFormListingError(
                    f"天猫尺码 {size} 的“{label}”区间必须恰好两个值"
                )
            parts = tuple(display_text(item) for item in value)
        else:
            text = str(value).strip().replace("～", "~")
            if "~" in text:
                if text.count("~") != 1:
                    raise TmallFormListingError(
                        f"天猫尺码 {size} 的“{label}”区间格式无效：{text!r}"
                    )
                parts = tuple(display_text(part) for part in text.split("~"))
            else:
                parts = (display_text(text),)
        if any(not part for part in parts):
            raise TmallFormListingError(
                f"天猫尺码 {size} 的“{label}”存在空值"
            )
        return parts

    async def _size_chart_table(self, container: Any) -> Any:
        # Element UI 的真实页面把表头和表体拆成两张 table；后续逻辑需要
        # 以共同的 .el-table 容器为根，才能同时读取 thead 与 tbody。
        grids = container.locator(".el-table")
        grid_matches = []
        for index in range(await grids.count()):
            candidate = grids.nth(index)
            if await candidate.locator("tbody tr").count() and await candidate.locator(
                "thead th"
            ).count():
                grid_matches.append(candidate)
        if len(grid_matches) == 1:
            return grid_matches[0]
        if len(grid_matches) > 1:
            raise TmallFormListingError(
                f"天猫尺码表可填表格不唯一：{len(grid_matches)}"
            )

        tables = container.locator("table")
        matches = []
        for index in range(await tables.count()):
            candidate = tables.nth(index)
            if await candidate.locator("tbody tr").count() and await candidate.locator(
                "thead th"
            ).count():
                matches.append(candidate)
        if len(matches) != 1:
            raise TmallFormListingError(
                f"天猫尺码表可填表格不唯一：{len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _is_size_header(value: object) -> bool:
        normalized = normalize_label(value)
        size_label = normalize_label("尺码")
        return normalized == size_label or normalized.endswith(size_label)

    async def _size_table_headers(
        self, table: Any
    ) -> Tuple[Tuple[str, bool, Any], ...]:
        # Element UI 会把左/右固定列的表头复制到独立 table。真实主表有时
        # 不再显示“尺码”表头，但主数据行仍保留对应 td。优先使用双方共有
        # 的 column class 对齐，完全摆脱副本数量和视觉下标。
        all_nodes = table.locator("thead th")
        header_by_column: Dict[
            str, List[Tuple[str, bool, Any, bool, bool]]
        ] = {}
        for index in range(await all_nodes.count()):
            node = all_nodes.nth(index)
            raw = re.sub(r"\s+", " ", (await node.inner_text()).strip())
            classes = (await node.get_attribute("class") or "").split()
            column_key = next(
                (
                    value
                    for value in classes
                    if re.fullmatch(r"el-table_\d+_column_\d+", value)
                ),
                "",
            )
            if not column_key:
                continue
            in_fixed = bool(
                await node.evaluate(
                    "element => Boolean(element.closest('.el-table__fixed, .el-table__fixed-right'))"
                )
            )
            is_hidden = "is-hidden" in classes
            header_by_column.setdefault(column_key, []).append(
                (
                    self._size_header_name(raw),
                    raw.lstrip().startswith("*"),
                    node,
                    in_fixed,
                    is_hidden,
                )
            )

        body_rows = await self._size_table_rows(table)
        if await body_rows.count():
            cells = body_rows.first.locator(":scope > td")
            aligned = []
            for index in range(await cells.count()):
                classes = (await cells.nth(index).get_attribute("class") or "").split()
                column_key = next(
                    (
                        value
                        for value in classes
                        if re.fullmatch(r"el-table_\d+_column_\d+", value)
                    ),
                    "",
                )
                candidates = header_by_column.get(column_key, ())
                if not column_key or not candidates:
                    aligned = []
                    break
                # Element UI 会给固定列副本补齐一整套 is-hidden 空表头。
                # 同一个 column id 因而可能同时对应一个真实标题和多个空副本；
                # 只把非隐藏且有文字的表头视为该列的权威标题。
                active = tuple(
                    candidate
                    for candidate in candidates
                    if not candidate[4] and normalize_label(candidate[0])
                )
                if not active:
                    active = tuple(
                        candidate
                        for candidate in candidates
                        if normalize_label(candidate[0])
                    )
                names = {normalize_label(candidate[0]) for candidate in active}
                if len(names) != 1:
                    raise TmallFormListingError(
                        f"天猫尺码表 column id {column_key} 对应多个表头"
                    )
                chosen = next(
                    (candidate for candidate in active if not candidate[3]),
                    active[0],
                )
                aligned.append(chosen[:3])
            if aligned:
                return tuple(aligned)

        # 单元测试和旧页面没有 Element UI column class，退回主 wrapper。
        nodes = table.locator(":scope > .el-table__header-wrapper thead th")
        if not await nodes.count():
            nodes = table.locator("thead th")
        headers = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            raw = re.sub(r"\s+", " ", (await node.inner_text()).strip())
            headers.append(
                (self._size_header_name(raw), raw.lstrip().startswith("*"), node)
            )
        return tuple(headers)

    async def _size_table_rows(self, table: Any) -> Any:
        rows = table.locator(
            ":scope > .el-table__body-wrapper tbody tr"
        )
        if await rows.count():
            return rows
        return table.locator("tbody tr")

    async def _size_cell_text(
        self, table: Any, row_index: int, main_cell: Any
    ) -> str:
        value = (await main_cell.inner_text()).strip()
        if value:
            return value
        classes = (await main_cell.get_attribute("class") or "").split()
        column_key = next(
            (
                item
                for item in classes
                if re.fullmatch(r"el-table_\d+_column_\d+", item)
            ),
            "",
        )
        if not column_key:
            return ""
        # 固定左列在主表体中可能只有空占位 td，实际尺码文字位于
        # el-table__fixed-body-wrapper 的同列、同行副本中。
        values = set()
        wrappers = table.locator(
            ":scope > .el-table__fixed .el-table__fixed-body-wrapper, "
            ":scope > .el-table__fixed-right .el-table__fixed-body-wrapper"
        )
        for wrapper_index in range(await wrappers.count()):
            rows = wrappers.nth(wrapper_index).locator("tbody tr")
            if row_index >= await rows.count():
                continue
            cells = rows.nth(row_index).locator(f":scope > td.{column_key}")
            for cell_index in range(await cells.count()):
                candidate = (await cells.nth(cell_index).inner_text()).strip()
                if candidate:
                    values.add(candidate)
        if len(values) > 1:
            raise TmallFormListingError(
                f"天猫尺码表第 {row_index + 1} 行固定列内容不唯一"
            )
        return next(iter(values), "")

    async def _strip_integer_decimal_displays(self, table: Any) -> None:
        # 前面的 fill/Tab 已经通过正常 input/change/blur 链路更新页面模型。
        # Element/Vue 会在随后的微任务中重新生成数字控件；每轮重新取得
        # locator，避免只改到已被替换的旧 input。
        for retry in range(2):
            if retry:
                await asyncio.sleep(0.15)
            inputs = (await self._size_table_rows(table)).locator(
                'input:not([type="checkbox"]):not([type="radio"])'
            )
            for index in range(await inputs.count()):
                control = inputs.nth(index)
                current = (await control.input_value()).strip()
                if not re.fullmatch(r"[+-]?\d+\.0+", current):
                    continue
                clean = current.split(".", 1)[0]
                # 不再派发 input；否则 Vue 会异步把控件重新格式化为 155.00。
                await control.evaluate(
                    """(element, value) => {
                      const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                      ).set;
                      setter.call(element, value);
                    }""",
                    clean,
                )
                actual = (await control.input_value()).strip()
                if actual != clean:
                    raise TmallFormListingError(
                        f"天猫尺码表整数显示格式清理失败：{actual!r}"
                    )

    async def clean_size_chart_integer_displays(self) -> None:
        """在所有天猫字段填写完成后，最终清除尺码整数的无意义小数位。"""
        container = await self._size_chart_container()
        table = await self._size_chart_table(container)
        await self._strip_integer_decimal_displays(table)

    async def fill_size_chart(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        if not rows:
            raise TmallFormListingError("天猫尺码表没有来源数据")
        sources_by_size: Dict[str, Mapping[str, Tuple[str, Any]]] = {}
        for row in rows:
            normalized_sources = self._row_sources(row)
            size_record = normalized_sources.get(normalize_label("尺码"))
            size = str(size_record[1]).strip() if size_record else ""
            normalized_size = normalize_option(size)
            if not normalized_size:
                raise TmallFormListingError("天猫尺码表来源行缺少尺码")
            if normalized_size in sources_by_size:
                raise TmallFormListingError(f"天猫尺码表来源尺码重复：{size}")
            sources_by_size[normalized_size] = normalized_sources

        container = await self._size_chart_container()
        table = await self._size_chart_table(container)
        headers = await self._size_table_headers(table)
        scanned_fields = tuple(label for label, _required, _node in headers)
        if self.logger is not None:
            self.logger.info(
                "天猫尺码表字段扫描完成：%s",
                " / ".join(scanned_fields),
            )
        required_labels = tuple(
            label for label, required, _node in headers if required
        )

        common_source_keys = set.intersection(
            *(set(values) for values in sources_by_size.values())
        )
        common_source_keys.discard(normalize_label("尺码"))
        # 顶部“选择参数”复选框保持页面原状，不自动勾选。只对当前已经
        # 渲染出的表格列按 Excel 同名字段填写；必填列先完整预检。
        missing_required: List[str] = []
        for label in required_labels:
            normalized = normalize_label(label)
            for values in sources_by_size.values():
                if normalized not in values:
                    missing_required.append(label)
                    break
                size = str(values[normalize_label("尺码")][1]).strip()
                try:
                    self._size_value_parts(
                        values[normalized][1], size=size, label=label
                    )
                except TmallFormListingError:
                    missing_required.append(label)
                    break
        if missing_required:
            raise TmallFormListingError(
                "天猫尺码表必填参数缺少来源："
                + "、".join(dict.fromkeys(missing_required))
            )

        # 区间是列级模式。所有尺码的来源必须一致为单值或二元区间，
        # 否则不猜测页面应使用哪一种模式。
        # 表头在未切换“区间”时不会变化。复用首次扫描结果，
        # 避免每一列都重扫 Element UI 的主表和固定列副本。
        current_headers = headers
        for header_label, _required, _node in headers:
            normalized_header = normalize_label(header_label)
            if normalized_header not in common_source_keys:
                continue
            part_counts = set()
            for values in sources_by_size.values():
                size = str(values[normalize_label("尺码")][1]).strip()
                part_counts.add(
                    len(
                        self._size_value_parts(
                            values[normalized_header][1],
                            size=size,
                            label=header_label,
                        )
                    )
                )
            if len(part_counts) != 1:
                raise TmallFormListingError(
                    f"天猫尺码参数“{header_label}”混用单值和区间"
                )
            expected_count = next(iter(part_counts))
            matches = [
                (index, node)
                for index, (name, _is_required, node) in enumerate(current_headers)
                if normalize_label(name) == normalized_header
            ]
            if len(matches) != 1:
                raise TmallFormListingError(
                    f"天猫尺码表列“{header_label}”不唯一"
                )
            column_index, header_node = matches[0]
            first_row = (await self._size_table_rows(table)).first
            first_inputs = first_row.locator(":scope > td").nth(column_index).locator(
                'input:not([type="checkbox"]):not([type="radio"])'
            )
            current_count = await first_inputs.count()
            if expected_count == 2 and current_count == 1:
                switches = header_node.get_by_text("区间", exact=True)
                visible_switches = []
                for switch_index in range(await switches.count()):
                    switch = switches.nth(switch_index)
                    if await switch.is_visible():
                        visible_switches.append(switch)
                if len(visible_switches) != 1:
                    raise TmallFormListingError(
                        f"天猫尺码参数“{header_label}”区间开关不唯一"
                    )
                await visible_switches[0].click()
                await self._wait_for_loading_masks()
                table = await self._size_chart_table(container)
                current_headers = await self._size_table_headers(table)
                rematches = [
                    index
                    for index, (name, _required, _node) in enumerate(current_headers)
                    if normalize_label(name) == normalized_header
                ]
                if len(rematches) != 1:
                    raise TmallFormListingError(
                        f"天猫尺码参数“{header_label}”切换区间后列丢失"
                    )
                first_inputs = (await self._size_table_rows(table)).first.locator(
                    ":scope > td"
                ).nth(rematches[0]).locator(
                    'input:not([type="checkbox"]):not([type="radio"])'
                )
                current_count = await first_inputs.count()
            if current_count != expected_count:
                mode = "区间" if expected_count == 2 else "单值"
                raise TmallFormListingError(
                    f"天猫尺码参数“{header_label}”页面不是期望的{mode}模式"
                )

        header_names = tuple(
            name for name, _required, _node in current_headers
        )
        size_indexes = [
            index
            for index, name in enumerate(header_names)
            if self._is_size_header(name)
        ]
        if len(size_indexes) != 1:
            raise TmallFormListingError(
                f"天猫尺码表“尺码”列不唯一：{size_indexes}"
            )
        size_index = size_indexes[0]

        page_rows = await self._size_table_rows(table)
        seen_sizes = set()
        filled_rows: Dict[str, Dict[str, str]] = {}
        for row_index in range(await page_rows.count()):
            page_row = page_rows.nth(row_index)
            cells = page_row.locator(":scope > td")
            if not await cells.count():
                continue
            page_size = await self._size_cell_text(
                table, row_index, cells.nth(size_index)
            )
            if not page_size:
                # Element UI 的测量/占位行没有业务尺码，也没有对应来源，
                # 不参与填写和完整性校验。
                continue
            normalized_size = normalize_option(page_size)
            if normalized_size not in sources_by_size:
                raise TmallFormListingError(
                    f"天猫尺码表页面出现 Excel 未提供的尺码：{page_size}"
                )
            seen_sizes.add(normalized_size)
            sources = sources_by_size[normalized_size]
            filled: Dict[str, Any] = {}
            for column_index, header in enumerate(header_names):
                if column_index == size_index:
                    continue
                normalized_header = normalize_label(header)
                source = sources.get(normalized_header)
                if source is None:
                    continue
                cell = cells.nth(column_index)
                inputs = cell.locator(
                    'input:not([type="checkbox"]):not([type="radio"])'
                )
                expected_parts = self._size_value_parts(
                    source[1], size=page_size, label=header
                )
                if await inputs.count() != len(expected_parts):
                    raise TmallFormListingError(
                        f"天猫尺码 {page_size} 的“{header}”输入框数量与来源不一致"
                    )
                actual_parts = []
                for input_index, expected in enumerate(expected_parts):
                    control = inputs.nth(input_index)
                    current = (await control.input_value()).strip()
                    # 已保存的 Element 数字区间会显示为 155.00。
                    # 对数值相同的控件不重新派发 input，避免组件
                    # 把新值与旧模型拼接成 155155。
                    if current == expected or _numeric_text_equal(current, expected):
                        actual_parts.append(current)
                        continue
                    if current:
                        await control.fill("")
                        await control.press("Tab")
                        await asyncio.sleep(0.05)
                        remaining = (await control.input_value()).strip()
                        if remaining:
                            raise TmallFormListingError(
                                f"天猫尺码 {page_size} 的“{header}”旧值清空失败："
                                f"{remaining!r}"
                            )
                    await control.fill(expected)
                    await control.press("Tab")
                    actual = (await control.input_value()).strip()
                    if actual != expected and not _numeric_text_equal(
                        actual, expected
                    ):
                        raise TmallFormListingError(
                            f"天猫尺码 {page_size} 的“{header}”回读失败：{actual!r}"
                        )
                    actual_parts.append(actual)
                filled[header] = (
                    tuple(actual_parts)
                    if len(actual_parts) == 2
                    else actual_parts[0]
                )
            filled_rows[page_size] = filled
        if seen_sizes != set(sources_by_size):
            missing_sizes = [
                str(values[normalize_label("尺码")][1])
                for key, values in sources_by_size.items()
                if key not in seen_sizes
            ]
            raise TmallFormListingError(
                "天猫尺码表页面缺少来源尺码：" + "、".join(missing_sizes)
            )
        await self._strip_integer_decimal_displays(table)
        return {
            "checked_parameters": (),
            "scanned_fields": header_names,
            "row_count": len(filled_rows),
            "rows": filled_rows,
        }

    @staticmethod
    def _tmall_main_image_order(paths: Sequence[Any]) -> Tuple[Any, ...]:
        """Return the Tmall-only order: local image 3, 2, 1, then the rest."""
        ordered = tuple(paths)
        if len(ordered) < 3:
            return ordered
        return (ordered[2], ordered[1], ordered[0], *ordered[3:])

    async def _inherited_image_locator_diagnostics(self) -> Mapping[str, Any]:
        """Return minimal DOM-shape evidence when the inherited image locator fails."""
        if self.panel is None:
            return {}
        selectors = (
            ".muti-upload",
            ".sc-upload.draggable",
            ".sc-upload",
            'input[type="file"]',
        )
        result: Dict[str, Any] = {}
        for selector in selectors:
            nodes = self.panel.locator(selector)
            count = await nodes.count()
            samples = []
            for index in range(min(count, 8)):
                node = nodes.nth(index)
                samples.append(
                    await node.evaluate(
                        """element => {
                          const ancestors = [];
                          let current = element;
                          for (let depth = 0; current && depth < 4; depth += 1) {
                            ancestors.push(current.className || current.tagName);
                            current = current.parentElement;
                          }
                          return {
                            visible: Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length),
                            ancestors,
                          };
                        }"""
                    )
                )
            result[selector] = {"count": count, "samples": samples}
        return result

    async def _inherited_main_image_item(self, label: str) -> Any:
        """Locate one Tmall inherited main-image group without assuming tabs' DOM.

        Older fixtures use ``.wrap-item > .wrap-item_label``.  The live Tmall
        panel instead renders each inherited row as ``.complex-wrap`` around a
        ``.muti-upload`` collection, so the generic Taobao section locator
        correctly returns zero there.  Prefer the old exact locator, then use
        the live upload collection and its nearest labelled complex wrapper.
        """
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        try:
            return await self._wrap_item(label)
        except TaobaoListingError:
            pass

        expected = normalize_label(label)
        groups = self.panel.locator(".muti-upload")
        matches: List[Any] = []
        for index in range(await groups.count()):
            group = groups.nth(index)
            if not await group.is_visible():
                continue

            # In the live page the label is a sibling of ``complex-wrap``:
            # ``el-form-item > label + el-form-item__content > complex-wrap
            # > muti-upload``.  Match at this boundary before considering
            # any wrapper-local labels or document order.
            form_item = None
            ancestor = group
            for _depth in range(5):
                ancestor = ancestor.locator("xpath=..")
                if await ancestor.count() != 1:
                    break
                classes = (await ancestor.get_attribute("class") or "").split()
                if "el-form-item" in classes:
                    form_item = ancestor
                    break
            if form_item is not None:
                form_labels = form_item.locator(":scope > .el-form-item__label")
                matched_form_label = False
                for label_index in range(await form_labels.count()):
                    candidate = form_labels.nth(label_index)
                    if await candidate.is_visible() and normalize_label(
                        await candidate.inner_text()
                    ) == expected:
                        matches.append(form_item)
                        matched_form_label = True
                        break
                if matched_form_label:
                    continue

            item = None
            ancestor = group
            for _depth in range(4):
                ancestor = ancestor.locator("xpath=..")
                if await ancestor.count() != 1:
                    break
                classes = (await ancestor.get_attribute("class") or "").split()
                if "complex-wrap" in classes:
                    item = ancestor
                    break
            if item is None or not await item.is_visible():
                continue

            # The first branch handles the regular Element label classes. The
            # prefix fallback covers this panel's lightweight image label
            # element (for example, "* 商品图片 拖动可调整顺序").  It is
            # prefix-only, so “商品图片” cannot accidentally select “3:4商品图片”.
            labels = item.locator(
                ".wrap-item_label, .el-form-item__label, [class*='label'], "
                "label, [class*='title']"
            )
            matched = False
            for label_index in range(await labels.count()):
                candidate = labels.nth(label_index)
                if await candidate.is_visible() and normalize_label(
                    await candidate.inner_text()
                ) == expected:
                    matched = True
                    break
            if not matched:
                text = normalize_label(await item.inner_text())
                matched = text.startswith(expected)
            if matched:
                matches.append(item)

        # Some category templates render these image labels outside the
        # upload wrapper.  In the live Tmall form there are exactly two
        # visible multi-image collections in document order: 1:1 first and
        # 3:4 second.  Only use that order-based fallback when it is uniquely
        # established; never guess from a larger collection of uploads.
        if not matches:
            multi_groups: List[Any] = []
            for index in range(await groups.count()):
                group = groups.nth(index)
                if not await group.is_visible():
                    continue
                if await group.locator(".sc-upload.draggable").count() >= 2:
                    multi_groups.append(group)
            if len(multi_groups) == 2:
                positions = {
                    normalize_label("商品图片"): 0,
                    normalize_label("3:4商品图片"): 1,
                }
                position = positions.get(expected)
                if position is not None:
                    return multi_groups[position]

        if len(matches) != 1:
            if self.logger is not None:
                self.logger.warning(
                    "天猫继承图片定位诊断：%s",
                    await self._inherited_image_locator_diagnostics(),
                )
            raise TmallFormListingError(
                f"天猫继承图片区域“{label}”不是唯一项：{len(matches)}"
            )
        return matches[0]

    async def sync_inherited_main_images(
        self,
        square_paths: Sequence[Any],
        portrait_paths: Sequence[Any],
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, str]:
        """Apply Tmall's fixed 1st/3rd main-image swap to both inherited rows.

        The base-data panel keeps its original order for every platform. Tmall
        displays two independent inherited groups (``商品图片`` and
        ``3:4商品图片``), so both are deliberately replaced in their requested
        order only when at least three local images exist.
        """
        mappings = (
            ("商品图片", square_paths),
            ("3:4商品图片", portrait_paths),
        )
        actions: Dict[str, str] = {}
        for label, paths in mappings:
            original = tuple(paths)
            if not original:
                raise TmallFormListingError(f"天猫{label}没有可同步的图片")
            ordered = self._tmall_main_image_order(original)
            item = await self._inherited_main_image_item(label)
            actions[label] = await uploader(
                self.page,
                item,
                ordered,
                label,
                timeout_seconds,
                force_replace=(ordered != original),
            )
        return actions

    async def sync_required_images(
        self,
        assets: Any,
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, str]:
        mappings = (
            ("商品竖图", assets.vertical_image),
            ("透明素材图", assets.transparent_image),
            ("产品参数图片", assets.parameter_image),
        )
        actions: Dict[str, str] = {}
        for label, path in mappings:
            try:
                item = await self._form_item(label)
            except TaobaoListingError as exc:
                raise TmallFormListingError(str(exc).replace("淘宝", "天猫")) from exc
            actions[label] = await uploader(
                self.page,
                item,
                (path,),
                label,
                timeout_seconds,
            )
        return actions

    async def sync_attribute_images(
        self,
        paths: Sequence[Any],
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, str]:
        """将第一销售规格（通常为颜色）的属性图片同步为 SKU 图。

        天猫把“属性图片”嵌在销售规格值内部，而不是普通商品图片的
        ``el-form-item``。SKU 图的既有约定是一张图对应第一规格的一个值，
        因此这里按页面显示顺序一一对应；已有图片由上传器按幂等规则保留。
        """
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        if not paths:
            raise TmallFormListingError("天猫属性图片没有可同步的 SKU 图")

        blocks = self.panel.locator(".block-specification")
        visible_blocks: List[Any] = []
        for index in range(await blocks.count()):
            block = blocks.nth(index)
            if await block.is_visible():
                visible_blocks.append(block)
        if not visible_blocks:
            raise TmallFormListingError("天猫商品规格区域未渲染")

        # 优先按规格名定位“颜色”，避免页面在不同类目下调整规格顺序。
        color_blocks: List[Any] = []
        for block in visible_blocks:
            title = block.locator(".title-bg").first
            title_input = title.locator("input").first
            dimension = (
                (await title_input.input_value()).strip()
                if await title_input.count()
                else (await title.inner_text()).strip()
            )
            if normalize_label(dimension) in {
                normalize_label("颜色"),
                normalize_label("色"),
            }:
                color_blocks.append(block)
        if len(color_blocks) > 1:
            raise TmallFormListingError("天猫颜色规格区域不唯一")
        block = color_blocks[0] if color_blocks else visible_blocks[0]

        values = block.locator(
            ".specification-value-flex > .specification-value-flex_item"
        )
        slots: List[Tuple[str, Any]] = []
        for index in range(await values.count()):
            value_item = values.nth(index)
            slot = value_item.locator(".specification-value-flex_img").first
            if await slot.count() != 1:
                continue
            value_input = value_item.locator(
                ".specification-value-flex_input input"
            ).first
            value = (
                (await value_input.input_value()).strip()
                if await value_input.count()
                else str(index + 1)
            )
            slots.append((value or str(index + 1), slot))
        if len(slots) != len(paths):
            raise TmallFormListingError(
                "天猫属性图片位数量与 SKU 图数量不一致："
                f"页面 {len(slots)}，本地 {len(paths)}"
            )

        actions: Dict[str, str] = {}
        for index, ((value, slot), path) in enumerate(zip(slots, paths), 1):
            label = f"属性图片[{value}]"
            actions[label] = await uploader(
                self.page,
                slot,
                (path,),
                label,
                timeout_seconds,
            )
        return actions

    async def fill_new_product_declaration(self) -> Optional[str]:
        if self.panel is None:
            raise TmallFormListingError("请先调用 open() 打开天猫资料")
        labels = self.panel.locator(".el-form-item > .el-form-item__label")
        visible = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if await label.is_visible():
                visible.append(((await label.inner_text()).strip(), label))
        expected = new_product_declaration_value(
            tuple(text for text, _label in visible)
        )
        if expected is None:
            return None
        matches = [
            label.locator("xpath=..")
            for text, label in visible
            if normalize_label(text) == normalize_label("是否申报新品")
        ]
        if len(matches) != 1:
            raise TmallFormListingError(
                f"天猫“是否申报新品”字段不是唯一项：{len(matches)}"
            )
        item = matches[0]
        radios = item.get_by_role("radio", name=expected, exact=True)
        if await radios.count() != 1:
            raise TmallFormListingError(
                f"天猫“是否申报新品”没有唯一的“{expected}”选项"
            )
        radio = radios.first
        if not await radio.is_checked():
            await radio.click()
        if not await radio.is_checked():
            raise TmallFormListingError(
                f"天猫“是否申报新品”选择“{expected}”后回读失败"
            )
        return expected


__all__ = [
    "TmallFormListing",
    "TmallFormListingError",
    "TmallProductWriteRequired",
]
