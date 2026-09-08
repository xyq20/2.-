"""DOM writer and post-save verifier for FastMai's JD product tab."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from jd_data import JdFields
from taobao_listing import (
    TaobaoListingError,
    excel_aliases,
    normalize_label,
    normalize_option,
    selection_value_groups,
)
from youzan_form_listing import YouzanFormListing, YouzanFormListingError


class JdFormListingError(RuntimeError):
    """A JD form problem that can be shown directly to the operator."""


JD_CATEGORY_PATH = ("服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤")
JD_BRAND = "NEIGBORL"
JD_DELIVERY_TEMPLATE = "48小时发货"
JD_SKU_THICKNESS = "常规"
JD_BATCH_ORDER = ("京东价", "库存")
JD_EXACT_OPTION_MAP: Mapping[str, Mapping[str, str]] = {
    normalize_label("面料"): {
        normalize_option("棉"): "棉布",
    },
}

JD_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("面料"): tuple(
        normalize_label(value)
        for value in ("面料材质", "面料俗称", "水洗标", "吊牌图")
    ),
    normalize_label("材质"): tuple(normalize_label(value) for value in ("材质成分",)),
    normalize_label("产地"): (),
    normalize_label("款式"): tuple(normalize_label(value) for value in ("裤型",)),
    normalize_label("裤型"): tuple(normalize_label(value) for value in ("款式",)),
    normalize_label("工艺"): tuple(normalize_label(value) for value in ("服饰工艺", "工艺处理")),
    normalize_label("流行元素"): tuple(normalize_label(value) for value in ("款式细节",)),
    normalize_label("版型"): tuple(normalize_label(value) for value in ("服饰版型",)),
    normalize_label("裤脚口款式"): tuple(normalize_label(value) for value in ("裤脚款式",)),
    normalize_label("适用人群"): tuple(normalize_label(value) for value in ("适用对象",)),
    normalize_label("上市时间"): tuple(
        normalize_label(value) for value in ("上市年份季节", "上市时节")
    ),
    normalize_label("弹力"): tuple(normalize_label(value) for value in ("弹力等级",)),
    normalize_label("风格"): tuple(normalize_label(value) for value in ("细分风格", "基础风格")),
    normalize_label("品牌类型"): (),
    normalize_label("功能"): tuple(normalize_label(value) for value in ("面料功能",)),
}

JD_IGNORED_ATTRIBUTE_LABELS = frozenset(
    normalize_label(value)
    for value in (
        "颜色",
        "是否可机洗",
    )
)


def _numeric_equal(actual: str, expected: str) -> bool:
    try:
        return Decimal(actual.replace(",", "")) == Decimal(expected.replace(",", ""))
    except (InvalidOperation, ValueError):
        return False


def _required_excel_value(
    fields: Mapping[str, str], aliases: Sequence[str], label: str
) -> str:
    wanted = {normalize_label(alias) for alias in aliases}
    matches = [
        (str(key), str(value).strip())
        for key, value in fields.items()
        if wanted.intersection(excel_aliases(key)) and str(value).strip()
    ]
    if not matches:
        raise JdFormListingError(
            "Excel 中缺少京东{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    values = {value for _key, value in matches}
    if len(values) != 1:
        raise JdFormListingError(
            "京东{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def _first_excel_value(
    fields: Mapping[str, str], aliases: Sequence[str]
) -> Optional[str]:
    wanted = {normalize_label(alias) for alias in aliases}
    for key, value in fields.items():
        if wanted.intersection(excel_aliases(key)):
            text = str(value).strip()
            if text:
                return text
    return None


def _material_name(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"\s*[（(]\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]\s*$", "", text)
    return text.strip()


def _material_percentage(value: str) -> Optional[str]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[%％]", str(value))
    if match is None:
        return None
    number = Decimal(match.group(1))
    if not number.is_finite() or number < 0 or number > 100:
        return None
    return format(number.normalize(), "f")


def _kilograms(value: str) -> str:
    text = str(value).strip().casefold().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(kg|公斤|g|克)?", text)
    if match is None:
        raise JdFormListingError("Excel 克重无法换算为公斤：{0!r}".format(value))
    number = Decimal(match.group(1))
    if match.group(2) in {"g", "克"}:
        number /= Decimal("1000")
    if not number.is_finite() or number <= 0:
        raise JdFormListingError("Excel 克重必须大于 0")
    return format(number.normalize(), "f")


def _category_parts(value: object) -> Tuple[str, ...]:
    return tuple(
        normalize_label(part)
        for part in re.split(r"[>＞/／]", str(value or ""))
        if normalize_label(part)
    )


class JdFormListing(YouzanFormListing):
    """Fill and verify JD category, attributes, SKU data and delivery time."""

    allow_created_exact_dom_option = True
    attribute_wait_timeout_seconds = 30.0
    attribute_stable_seconds = 0.75

    async def _raise_as_jd(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except (YouzanFormListingError, TaobaoListingError) as exc:
            raise JdFormListingError(
                str(exc).replace("有赞", "京东").replace("淘宝", "京东")
            ) from exc

    async def open(self) -> "JdFormListing":
        tab = self.drawer.get_by_role("tab", name="京东资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role("tabpanel", name="京东资料", exact=True)
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise JdFormListingError("找不到可切换的“京东资料”页签") from exc
        self.panel = panel
        await self._raise_as_jd(super()._wait_for_loading_masks())
        if self.logger is not None:
            self.logger.info("京东资料页签已打开")
        return self

    async def _category_text(self) -> str:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        category = self.panel.locator(".platform-category-input").first
        if await category.count() and await category.is_visible():
            text = re.sub(r"\s+", " ", (await category.inner_text()).strip())
            text = re.sub(r"^商品分类\s*[：:]?\s*", "", text)
            text = re.split(r"\s*(?:修改类目|同步平台类目)\s*", text, maxsplit=1)[0].strip()
            if text:
                return text
        values = await self.panel.evaluate(
            """root => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const result = [];
              for (const element of root.querySelectorAll('*')) {
                const style = getComputedStyle(element);
                if (!element.getClientRects().length || style.display === 'none'
                    || style.visibility === 'hidden') continue;
                const text = clean(element.innerText);
                const match = text.match(/^商品分类\s*[：:]\s*(.*?)(?:\s*修改类目|\s*同步平台类目|$)/);
                if (match && match[1]) result.push(clean(match[1]));
              }
              return [...new Set(result)];
            }"""
        )
        if len(values) != 1:
            raise JdFormListingError("京东页面已选类目不是唯一项：{0}".format(len(values)))
        return str(values[0])

    @staticmethod
    def _json_category_nodes(payload: Any, target: str) -> Tuple[Mapping[str, Any], ...]:
        """Return the API objects that identify the exact target category."""
        wanted = normalize_label(target)
        matches: List[Mapping[str, Any]] = []

        def walk(value: Any, path: Tuple[str, ...]) -> None:
            if isinstance(value, Mapping):
                exact = any(
                    isinstance(child, str) and normalize_label(child) == wanted
                    for child in value.values()
                )
                if exact:
                    record: Dict[str, Any] = {"json_path": "/".join(path)}
                    for key, child in value.items():
                        if child is None or isinstance(child, (str, int, float, bool)):
                            record[str(key)] = child
                    matches.append(record)
                for key, child in value.items():
                    walk(child, path + (str(key),))
                return
            if isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, path + (str(index),))
                return
            if isinstance(value, str):
                text = value.strip()
                if text.startswith(("{", "[")):
                    try:
                        walk(json.loads(text), path + ("json-string",))
                    except (TypeError, ValueError):
                        return

        walk(payload, ())
        deduplicated: Dict[str, Mapping[str, Any]] = {}
        for match in matches:
            key = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str)
            deduplicated[key] = match
        return tuple(deduplicated.values())

    @staticmethod
    def _json_contains_category(payload: Any, target: str) -> bool:
        return bool(JdFormListing._json_category_nodes(payload, target))

    async def apply_category(self) -> Mapping[str, Any]:
        target = JD_CATEGORY_PATH[-1]
        try:
            current = await self._category_text()
        except JdFormListingError:
            current = ""
        if _category_parts(current)[-1:] == (normalize_label(target),):
            return {"path": JD_CATEGORY_PATH, "selected": current, "changed": False}
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        modify = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await modify.count() != 1:
            raise JdFormListingError("京东“修改类目”按钮不是唯一项")
        await modify.click()
        dialog = self.page.get_by_role("dialog", name="修改类目", exact=True)
        try:
            await dialog.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise JdFormListingError("京东修改类目弹窗未出现") from exc
        search = dialog.get_by_placeholder("请输入类目关键词，支持模糊查询", exact=True)
        if await search.count() != 1:
            inputs = dialog.locator('input:not([type="hidden"]):not([readonly]):visible')
            if await inputs.count() != 1:
                raise JdFormListingError("京东修改类目弹窗缺少唯一搜索框")
            search = inputs.first

        api_matches: List[str] = []
        api_candidates: List[Mapping[str, Any]] = []
        tasks: List[asyncio.Task[Any]] = []

        async def capture(response: Any) -> None:
            try:
                headers = await response.all_headers()
                if "json" not in str(headers.get("content-type") or "").casefold():
                    return
                payload = await response.json()
                nodes = self._json_category_nodes(payload, target)
                if nodes:
                    path = urlsplit(response.url).path
                    if path not in api_matches:
                        api_matches.append(path)
                    for node in nodes:
                        record = {"response_path": path, **dict(node)}
                        if record not in api_candidates:
                            api_candidates.append(record)
            except Exception:
                return

        def handler(response: Any) -> None:
            tasks.append(asyncio.create_task(capture(response)))

        self.page.on("response", handler)
        await search.fill(target)
        deadline = asyncio.get_running_loop().time() + 30
        candidates: List[Any] = []
        stable_since: Optional[float] = None
        try:
            while asyncio.get_running_loop().time() < deadline:
                nodes = dialog.get_by_text(target, exact=True)
                candidates = [
                    nodes.nth(index)
                    for index in range(await nodes.count())
                    if await nodes.nth(index).is_visible()
                ]
                if api_matches and len(candidates) == 1:
                    if stable_since is None:
                        stable_since = asyncio.get_running_loop().time()
                    elif asyncio.get_running_loop().time() - stable_since >= 1.0:
                        break
                else:
                    stable_since = None
                await asyncio.sleep(0.1)
        finally:
            self.page.remove_listener("response", handler)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if not api_matches or len(candidates) != 1:
            raise JdFormListingError(
                "京东类目接口与 DOM 交叉校验失败：接口 {0}，DOM {1}".format(
                    len(api_matches), len(candidates)
                )
            )
        if self.logger is not None:
            self.logger.info("京东类目 JSON 节点：%s", api_candidates)
        # 快麦页面右下角助手浮层会间歇性遮住类目候选。
        # JSON 接口已确认该候选存在，因此直接触发这个唯一 DOM
        # 节点，避免 Playwright 因非业务浮层持续等待。
        loading = self.page.locator(".el-loading-mask:visible")
        mask_deadline = asyncio.get_running_loop().time() + 15
        while await loading.count() and asyncio.get_running_loop().time() < mask_deadline:
            await asyncio.sleep(0.1)
        if await loading.count():
            raise JdFormListingError("京东类目搜索加载遮罩在 15 秒内未消失")
        if self.logger is not None:
            structure = await candidates[0].evaluate(
                """node => {
                  const result = [];
                  let current = node;
                  for (let index = 0; current && index < 5; index += 1) {
                    result.push({
                      tag: current.tagName,
                      className: String(current.className || ''),
                      role: current.getAttribute && current.getAttribute('role'),
                      html: String(current.outerHTML || '').slice(0, 600)
                    });
                    current = current.parentElement;
                  }
                  return result;
                }"""
            )
            self.logger.info("京东目标类目 DOM 结构：%s", structure)
        async def selection_state() -> Mapping[str, bool]:
            return await dialog.evaluate(
                """(root, expected) => {
                  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                  const row = Array.from(root.querySelectorAll('.el-cascader-node'))
                    .find(node => clean(node.innerText) === expected);
                  const rowSelected = Boolean(row) && (
                    row.classList.contains('is-active') ||
                    Boolean(row.querySelector('.el-icon-check, input:checked, [aria-checked="true"]'))
                  );
                  const footerSelected = Array.from(root.querySelectorAll('*')).some(node => {
                    const text = clean(node.innerText);
                    return /^\s*已选\s*[：:]/.test(text) && text.includes(expected);
                  });
                  return {rowSelected, footerSelected};
                }""",
                target,
            )

        selected = False
        for attempt in range(5):
            # 等待 DOM 稳定
            await asyncio.sleep(0.3)
            live_nodes = dialog.get_by_text(target, exact=True)
            live_candidates = [
                live_nodes.nth(index)
                for index in range(await live_nodes.count())
                if await live_nodes.nth(index).is_visible()
            ]
            if len(live_candidates) == 0:
                if self.logger is not None:
                    self.logger.warning(
                        "京东目标类目第 %s 次尝试时 DOM 节点不可见，等待重新渲染",
                        attempt + 1
                    )
                await asyncio.sleep(0.5)
                continue
            if len(live_candidates) != 1:
                if self.logger is not None:
                    self.logger.warning(
                        "京东目标类目候选数量异常：%s 个，尝试 %s/5",
                        len(live_candidates), attempt + 1
                    )
                if attempt < 4:
                    await asyncio.sleep(0.5)
                    continue
                raise JdFormListingError(
                    "京东目标类目重新渲染后不是唯一项：{0}".format(
                        len(live_candidates)
                    )
                )
            row = live_candidates[0].locator(
                "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' el-cascader-node ')][1]"
            )
            if attempt == 0 or attempt == 1:
                await row.click(force=True, timeout=5_000)
            elif attempt == 2:
                await row.evaluate(
                    """node => {
                      node.focus();
                      for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        node.dispatchEvent(new MouseEvent(type, {
                          bubbles: true, cancelable: true, view: window
                        }));
                      }
                    }"""
                )
            else:
                invoked = await row.evaluate(
                    """node => {
                      const vm = node.__vue__;
                      if (!vm || typeof vm.handleClick !== 'function') return false;
                      vm.handleClick();
                      return true;
                    }"""
                )
                if not invoked:
                    await row.focus()
                    await row.press("Enter")
            selected_deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < selected_deadline:
                state = await selection_state()
                if state.get("rowSelected") and state.get("footerSelected"):
                    selected = True
                    break
                await asyncio.sleep(0.1)
            if selected:
                break
            if self.logger is not None:
                self.logger.warning("京东类目第 %s 次点击未选中，继续重试", attempt + 1)
            await asyncio.sleep(0.5)
        if not selected:
            raise JdFormListingError("京东类目 DOM 点击后未同时出现勾选与已选路径")
        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise JdFormListingError("京东修改类目弹窗“确定”按钮不是唯一项")
        await confirm.click()
        await dialog.wait_for(state="hidden", timeout=20_000)
        await self._raise_as_jd(super()._wait_for_loading_masks())
        actual = await self._category_text()
        if _category_parts(actual)[-1:] != (normalize_label(target),):
            raise JdFormListingError(
                "京东类目确认后回读失败：页面为 {0!r}".format(actual)
            )
        self.category_clicked = True
        if self.logger is not None:
            self.logger.info(
                "京东类目已通过 JSON 接口与 DOM 交叉校验并选中：%s",
                actual,
            )
        return {
            "path": JD_CATEGORY_PATH,
            "selected": actual,
            "changed": True,
            "api_paths": tuple(api_matches),
            "api_candidates": tuple(api_candidates),
        }

    async def _form_item_exact(self, label: str, *, occurrence: Optional[int] = None) -> Any:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        labels = self.panel.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await labels.count()):
            node = labels.nth(index)
            if not await node.is_visible():
                continue
            item = node.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]")
            if await item.count() == 1 and item not in matches:
                matches.append(item)
        if occurrence is not None:
            if occurrence < 1 or occurrence > len(matches):
                raise JdFormListingError(
                    "京东字段“{0}”第 {1} 个表单项不存在（共 {2} 个）".format(
                        label, occurrence, len(matches)
                    )
                )
            return matches[occurrence - 1]
        if len(matches) != 1:
            raise JdFormListingError("京东字段“{0}”表单项不是唯一项：{1}".format(label, len(matches)))
        return matches[0]

    @staticmethod
    async def _editable_inputs(item: Any) -> List[Any]:
        nodes = item.locator('input:not([type="hidden"]):not([readonly]):visible')
        result = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select ')]"
            ).count():
                result.append(node)
        return result

    @staticmethod
    async def _enter_as_user(input_box: Any, expected: str) -> None:
        await input_box.click()
        await input_box.fill("")
        await input_box.press_sequentially(expected)
        await input_box.press("Tab")

    async def _fill_input_item(
        self, label: str, expected: str, *, numeric: bool = False
    ) -> str:
        item = await self._form_item_exact(label)
        inputs = await self._editable_inputs(item)
        if len(inputs) != 1:
            raise JdFormListingError("京东字段“{0}”输入框不是唯一项：{1}".format(label, len(inputs)))
        input_box = inputs[0]
        before = (await input_box.input_value()).strip()
        matches = _numeric_equal(before, expected) if numeric else before == expected
        if not matches:
            await self._enter_as_user(input_box, expected)
        actual = (await input_box.input_value()).strip()
        matches = _numeric_equal(actual, expected) if numeric else actual == expected
        if not matches:
            raise JdFormListingError("京东字段“{0}”回读失败：{1!r}".format(label, actual))
        return actual

    async def fill_identity_and_parameters(
        self, fields: Mapping[str, str], *, style_code: str
    ) -> Mapping[str, str]:
        brand_item = await self._form_item_exact("品牌")
        async def read_brand() -> Tuple[str, Tuple[str, ...]]:
            # 尝试从多种可能的输入框读取品牌
            inputs = brand_item.locator('input:not([type="hidden"]):visible')
            collected: List[str] = []
            for index in range(await inputs.count()):
                value = (await inputs.nth(index).input_value()).strip()
                if value:
                    collected.append(value)

            # 同时检查 el-select 的显示值
            select_inputs = brand_item.locator('.el-select input:visible')
            for index in range(await select_inputs.count()):
                value = (await select_inputs.nth(index).input_value()).strip()
                if value and value not in collected:
                    collected.append(value)

            values = tuple(collected)
            value = next(
                (
                    item for item in values
                    if normalize_option(item) == normalize_option(JD_BRAND)
                ),
                "",
            )
            return value, values

        brand, brand_values = await read_brand()
        if not brand:
            # 首先尝试使用下拉框选择品牌
            select = brand_item.locator(".el-select").first
            if await select.count():
                if self.logger is not None:
                    self.logger.info("京东品牌使用下拉框选择：%s", JD_BRAND)
                try:
                    actual = await self._raise_as_jd(
                        self._select_values(
                            select,
                            ((JD_BRAND,),),
                            label="品牌",
                            multi=False,
                        )
                    )
                    if actual:
                        brand = actual[0]
                        brand_values = actual
                except Exception as e:
                    if self.logger is not None:
                        self.logger.warning("京东品牌下拉选择失败：%s", e)

            # 如果下拉选择失败，尝试手动填写
            if not brand:
                inputs = await self._editable_inputs(brand_item)
                if len(inputs) >= 1:
                    if self.logger is not None:
                        self.logger.info("京东品牌下拉失败，尝试手动填写：%s", JD_BRAND)
                    await self._enter_as_user(inputs[0], JD_BRAND)
                    await asyncio.sleep(0.5)
                    brand, brand_values = await read_brand()

            # 如果手动填写也失败，尝试一键应用
            if not brand:
                apply_brand = self.panel.get_by_role(
                    "button", name="一键应用品牌配置", exact=True
                )
                if await apply_brand.count() == 1:
                    if self.logger is not None:
                        self.logger.info("京东品牌为空，点击一键应用品牌配置")
                    await apply_brand.click(force=True, timeout=5_000)
                    deadline = asyncio.get_running_loop().time() + 15
                    while asyncio.get_running_loop().time() < deadline:
                        await self._raise_as_jd(super()._wait_for_loading_masks())
                        brand, brand_values = await read_brand()
                        if brand:
                            break
                        await asyncio.sleep(0.1)
                elif self.logger is not None:
                    self.logger.warning(
                        "京东品牌为空，但一键应用品牌配置按钮数量异常：%s",
                        await apply_brand.count()
                    )

        if normalize_option(brand) != normalize_option(JD_BRAND):
            # 如果还是不匹配，记录详细信息
            if self.logger is not None:
                self.logger.error(
                    "京东品牌配置失败：期望=%s, 实际=%s, 所有值=%s",
                    JD_BRAND, brand, brand_values
                )
            raise JdFormListingError(
                "京东品牌配置失败：期望 {0!r}，实际 {1!r}".format(JD_BRAND, brand_values)
            )

        origin = _required_excel_value(fields, ("产地",), "产地")
        weight_source = _required_excel_value(fields, ("克重", "商品毛重"), "商品毛重")
        gross_weight = _kilograms(weight_source)
        result = {
            "品牌": brand,
            "货号": await self._fill_input_item("货号", style_code),
            "商品毛重(公斤)": await self._fill_input_item(
                "商品毛重(公斤)", gross_weight, numeric=True
            ),
        }
        # 京东页中“商品参数”和“商品属性”各有一个产地；
        # 这里明确取上方商品参数项，属性项由后续映射统一填写。
        origin_item = await self._form_item_exact("产地", occurrence=1)
        actual = await self._raise_as_jd(
            self._fill_attribute("产地", origin_item, origin, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东产地没有可用的 Excel 精确候选")
        result["产地"] = actual[0]
        return result

    async def _attribute_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:商品属性|销售属性|商品规格|规格明细|商品图片|物流信息|售后服务)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            box = await heading.bounding_box()
            if box is None:
                continue
            label = normalize_label(await heading.inner_text())
            if label == normalize_label("商品属性"):
                start = box["y"]
            elif start is not None and box["y"] > start:
                end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _collect_attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        top, end = await self._attribute_bounds()
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        items = self.panel.locator(".el-form-item:visible")
        for index in range(await items.count()):
            item = items.nth(index)
            box = await item.bounding_box()
            if box is not None:
                if top is not None and box["y"] <= top:
                    continue
                if end is not None and box["y"] >= end:
                    continue
            label_node = item.locator(":scope > .el-form-item__label").first
            if not await label_node.count() or not await label_node.is_visible():
                continue
            label = re.sub(r"^\s*\*\s*", "", (await label_node.inner_text()).strip())
            normalized = normalize_label(label)
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(normalized, counts[normalized])
            result[key] = (label, item)
        return result

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.attribute_wait_timeout_seconds
        previous: Optional[Tuple[str, ...]] = None
        stable_since: Optional[float] = None
        while loop.time() < deadline:
            items = await self._collect_attribute_items()
            signature = tuple(items)
            now = loop.time()
            if signature:
                if signature != previous:
                    previous = signature
                    stable_since = now
                elif stable_since is not None and now - stable_since >= self.attribute_stable_seconds:
                    if self.logger is not None:
                        self.logger.info("京东商品属性已稳定渲染：%s 项", len(items))
                    return items
            await asyncio.sleep(0.25)
        raise JdFormListingError("京东商品属性在 30 秒内未稳定渲染")

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        sources: Dict[str, List[Tuple[str, str]]] = {}
        for excel_key, raw_value in fields.items():
            excel_names = set(excel_aliases(excel_key))
            for page_key, (page_label, _item) in page_items.items():
                base = page_key.split("#", 1)[0]
                accepted = {base}
                accepted.update(JD_FIELD_ALIASES.get(base, ()))
                if excel_names.intersection(accepted):
                    sources.setdefault(page_key, []).append((str(excel_key), str(raw_value).strip()))
        assignments: Dict[str, Tuple[str, str]] = {}
        for page_key, (page_label, _item) in page_items.items():
            matches = sources.get(page_key, ())
            if not matches:
                continue
            values = {value for _key, value in matches}
            if len(values) != 1:
                raise JdFormListingError(
                    "京东属性“{0}”匹配到多个 Excel 值：{1}".format(
                        page_label, "、".join(key for key, _value in matches)
                    )
                )
            assignments[page_key] = (page_label, matches[0][1])
        return assignments

    @staticmethod
    async def _is_required(item: Any) -> bool:
        classes = set((await item.get_attribute("class") or "").split())
        label = item.locator(":scope > .el-form-item__label").first
        return "is-required" in classes or bool(
            await label.count() and re.match(r"^\s*\*", (await label.inner_text()).strip())
        )

    async def _fill_attribute_item(
        self, page_label: str, item: Any, expected: str, *, required: bool
    ) -> Optional[Tuple[str, ...]]:
        normalized = normalize_label(page_label)
        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        inputs = await self._editable_inputs(item)
        material = normalized in {normalize_label("面料"), normalize_label("材质")}
        if material and not await selects.count():
            add = item.get_by_role("button", name="添加", exact=True)
            if await add.count() == 1:
                await add.click(force=True, timeout=5_000)
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    selects = item.locator(
                        ":scope > .el-form-item__content .el-select:visible"
                    )
                    inputs = await self._editable_inputs(item)
                    if await selects.count() and inputs:
                        break
                    await asyncio.sleep(0.1)
        desired = _material_name(expected) if material else expected
        desired = JD_EXACT_OPTION_MAP.get(normalized, {}).get(
            normalize_option(desired), desired
        )
        actual: Optional[Tuple[str, ...]] = None
        if await selects.count():
            # Material rows contain a value select plus an optional percentage input.
            value_select = selects.first
            multi = await value_select.locator(".el-select__tags").count() > 0
            try:
                actual = await self._raise_as_jd(
                    self._select_values(
                        value_select,
                        ((desired,),) if material else selection_value_groups(page_label, desired),
                        label=page_label,
                        multi=multi,
                    )
                )
            except JdFormListingError:
                raise
            if actual is None:
                if required:
                    raise JdFormListingError("京东属性“{0}”没有 Excel 精确候选".format(page_label))
                return None
        elif len(inputs) == 1:
            # 检查是否是只读的 cascader 输入框
            is_readonly = await inputs[0].get_attribute(“readonly”)
            if is_readonly:
                # 这是级联选择器，尝试使用下拉选择逻辑
                cascader = item.locator(“.el-cascader”).first
                if await cascader.count():
                    if self.logger is not None:
                        self.logger.info(“京东属性”%s”是级联选择器，尝试选择值”, page_label)
                    try:
                        actual = await self._raise_as_jd(
                            self._select_values(
                                cascader,
                                selection_value_groups(page_label, desired),
                                label=page_label,
                                multi=False,
                            )
                        )
                        if actual is None:
                            if required:
                                raise JdFormListingError(“京东属性”{0}”没有 Excel 精确候选”.format(page_label))
                            return None
                    except JdFormListingError:
                        if required:
                            raise
                        return None
                else:
                    if required:
                        raise JdFormListingError(“京东属性”{0}”是只读输入框但不是级联选择器”.format(page_label))
                    return None
            else:
                # 普通可编辑输入框
                expected_text = str(desired).strip()
                if (await inputs[0].input_value()).strip() != expected_text:
                    await self._enter_as_user(inputs[0], expected_text)
                actual = ((await inputs[0].input_value()).strip(),)
                if actual[0] != expected_text:
                    raise JdFormListingError(“京东属性”{0}”回读失败”.format(page_label))
        else:
            if required:
                structure = await item.evaluate(
                    “””node => ({
                      html: String(node.outerHTML || '').slice(0, 5000),
                      selects: node.querySelectorAll('.el-select').length,
                      cascaders: node.querySelectorAll('.el-cascader').length,
                      inputs: Array.from(node.querySelectorAll('input')).map(input => ({
                        type: input.type,
                        value: input.value,
                        readOnly: input.readOnly,
                        disabled: input.disabled,
                        className: input.className
                      }))
                    })”””
                )
                if self.logger is not None:
                    self.logger.info(“京东属性”%s”复合控件结构：%s”, page_label, structure)
                raise JdFormListingError(
                    “京东属性”{0}”无唯一可填控件：”
                    “select={1}, cascader={2}, input={3}”.format(
                        page_label,
                        structure.get(“selects”),
                        structure.get(“cascaders”),
                        len(structure.get(“inputs”, ()))
                    )
                )
            return None

        if material and inputs:
            percentage = _material_percentage(expected)
            if percentage is not None:
                percent_input = inputs[0]
                if not _numeric_equal((await percent_input.input_value()).strip(), percentage):
                    await self._enter_as_user(percent_input, percentage)
                if not _numeric_equal((await percent_input.input_value()).strip(), percentage):
                    raise JdFormListingError("京东属性“{0}”百分比回读失败".format(page_label))
        return actual

    async def fill_attributes(self, fields: JdFields) -> Mapping[str, Any]:
        items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        missing_required = []
        for key, (page_label, item) in items.items():
            # 选择一个属性后，京东表单会重新渲染后续复合字段。
            # Playwright 的 nth locator 会因 DOM 顺序变化指向错项，
            # 所以每次写入前按字段名重新解析当前页面结构。
            current_items = await self._collect_attribute_items()
            current = current_items.get(key)
            if current is None:
                raise JdFormListingError(
                    "京东属性“{0}”在页面重新渲染后消失".format(page_label)
                )
            page_label, item = current
            required = await self._is_required(item)
            assignment = assignments.get(key)
            if assignment is None:
                if required and key.split("#", 1)[0] not in JD_IGNORED_ATTRIBUTE_LABELS:
                    missing_required.append(page_label)
                continue
            _source, expected = assignment
            actual = await self._fill_attribute_item(page_label, item, expected, required=required)
            if actual is None:
                skipped[page_label] = expected
            else:
                applied[page_label] = actual
        if missing_required:
            raise JdFormListingError("Excel 中缺少京东必填属性：" + "、".join(missing_required))
        return {
            "attributes": applied,
            "skipped_no_exact_candidate": skipped,
            "unmatched_page_fields": tuple(
                page_label for key, (page_label, _item) in items.items() if key not in assignments
            ),
        }

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        nodes = self.panel.get_by_text(re.compile(r"^\s*{0}\s*[：:]?\s*$".format(re.escape(label))))
        matches = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            root = node
            for _depth in range(5):
                inputs = root.locator('input:not([type="hidden"]):not([readonly]):visible')
                if await inputs.count() == 1 and not await root.locator(".el-table, table").count():
                    matches.append(inputs.first)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise JdFormListingError("京东批量字段“{0}”输入框不是唯一项：{1}".format(label, len(matches)))
        return matches[0]

    @staticmethod
    def _expected_sku(fields: Mapping[str, str]) -> Mapping[str, str]:
        result = {
            "京东价": _required_excel_value(fields, ("京东价", "价格", "基本售价", "商品价格"), "京东价"),
            "库存": _required_excel_value(fields, ("库存", "数量"), "库存"),
        }
        for label, value in result.items():
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise JdFormListingError("Excel 京东{0}不是数字".format(label)) from exc
            if not number.is_finite() or number <= 0 if label == "京东价" else number < 0 or number % 1:
                raise JdFormListingError("Excel 京东{0}值无效：{1!r}".format(label, value))
        return result

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            snapshot = await tables.nth(index).evaluate(
                """root => {
                  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                  const headers = Array.from(root.querySelectorAll(
                    '.el-table__header-wrapper th, thead th'
                  )).map(node => clean(node.innerText));
                  const rows = Array.from(root.querySelectorAll(
                    '.el-table__body-wrapper tbody tr, tbody tr'
                  )).map(row => Array.from(row.querySelectorAll(':scope > td')).map(cell => {
                    const inputs = Array.from(cell.querySelectorAll('input'))
                      .filter(input => input.type !== 'checkbox' && input.type !== 'radio')
                      .map(input => clean(input.value));
                    return inputs.length ? inputs.join('|') : clean(cell.innerText);
                  }));
                  return {headers, rows};
                }"""
            )
            normalized = tuple(normalize_label(value) for value in snapshot["headers"])
            if all(any(header == normalize_label(label) or header.startswith(normalize_label(label)) for header in normalized) for label in JD_BATCH_ORDER):
                matches.append(snapshot)
        if len(matches) != 1:
            raise JdFormListingError("京东 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    @staticmethod
    def _column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        indexes = [index for index, header in enumerate(headers) if normalize_label(header) == wanted or normalize_label(header).startswith(wanted)]
        if len(indexes) != 1:
            raise JdFormListingError("京东 SKU 列“{0}”不是唯一项".format(label))
        return indexes[0]

    def _validate_sku(self, snapshot: Mapping[str, Any], expected: Mapping[str, str]) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = tuple(snapshot.get("rows", ()))
        if not raw_rows:
            raise JdFormListingError("京东 SKU 表格没有明细行")
        indexes = {label: self._column_index(headers, label) for label in expected}
        rows = []
        errors = []
        for number, raw_row in enumerate(raw_rows, 1):
            row = {label: str(raw_row[index]) if index < len(raw_row) else "" for label, index in indexes.items()}
            for label, value in expected.items():
                if not _numeric_equal(row[label], value):
                    errors.append("第{0}行{1}={2!r}".format(number, label, row[label]))
            rows.append(row)
        if errors:
            raise JdFormListingError("京东批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(rows)

    async def fill_sku_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = self._expected_sku(fields)
        for label, value in expected.items():
            input_box = await self._batch_input(label)
            if not _numeric_equal((await input_box.input_value()).strip(), value):
                await self._enter_as_user(input_box, value)
        buttons = self.panel.get_by_role("button", name="批量设置", exact=True)
        candidates = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible() and not await button.locator("xpath=ancestor::th").count():
                candidates.append(button)
        if len(candidates) != 1:
            raise JdFormListingError("京东价格库存“批量设置”按钮不是唯一项：{0}".format(len(candidates)))
        await candidates[0].click()
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku(await self._sku_table_snapshot(), expected)
                return {"batch_clicked": True, "row_count": len(rows), "values": expected, "rows": rows}
            except JdFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.15)
        raise JdFormListingError(str(last_error or "京东批量设置超时"))

    async def apply_sku_thickness(self) -> Mapping[str, str]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        headers = self.panel.get_by_text(re.compile(r"^\s*SKU\s*/?\s*属性\s*$", re.I))
        triggers = []
        for index in range(await headers.count()):
            header = headers.nth(index)
            if not await header.is_visible():
                continue
            root = header.locator("xpath=ancestor::th[1]")
            if await root.count():
                links = root.get_by_text("批量设置", exact=True)
                for link_index in range(await links.count()):
                    if await links.nth(link_index).is_visible():
                        triggers.append(links.nth(link_index))
        if len(triggers) != 1:
            raise JdFormListingError("京东 SKU 属性“批量设置”不是唯一项：{0}".format(len(triggers)))
        await triggers[0].click()
        dialogs = self.page.locator(".el-dialog:visible, [role=dialog]:visible")
        matches = []
        for index in range(await dialogs.count()):
            dialog = dialogs.nth(index)
            if normalize_label("厚度") in normalize_label(await dialog.inner_text()):
                matches.append(dialog)
        if len(matches) != 1:
            raise JdFormListingError("京东 SKU 属性批量弹窗不是唯一项：{0}".format(len(matches)))
        dialog = matches[0]
        labels = dialog.get_by_text(re.compile(r"^\s*厚度\s*[：:]?\s*$"))
        if await labels.count() != 1:
            raise JdFormListingError("京东 SKU 属性弹窗缺少“厚度”")
        item = labels.first.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]")
        actual = await self._raise_as_jd(
            self._fill_attribute("厚度", item, JD_SKU_THICKNESS, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东 SKU 厚度没有“常规”精确候选")
        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise JdFormListingError("京东 SKU 属性弹窗“确定”不是唯一项")
        await confirm.click()
        await dialog.wait_for(state="hidden", timeout=10_000)
        return {"厚度": actual[0]}

    async def fill_summary_prices(self, fields: Mapping[str, str]) -> Mapping[str, str]:
        jd_price = _required_excel_value(fields, ("京东价", "价格", "基本售价", "商品价格"), "京东价")
        market = _required_excel_value(fields, ("市场价", "价格", "吊牌价", "商品价格"), "市场价")
        return {
            "京东价": await self._fill_input_item("京东价（元）", jd_price),
            "市场价": await self._fill_input_item("市场价（元）", market),
        }

    async def fill_delivery_template(self) -> str:
        item = await self._form_item_exact("发货时效")
        actual = await self._raise_as_jd(
            self._fill_attribute("发货时效", item, JD_DELIVERY_TEMPLATE, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东发货时效没有“48小时发货”精确候选")
        return actual[0]

    async def _visible_validation_errors(self) -> Tuple[str, ...]:
        if self.panel is None:
            return ()
        return tuple(
            await self.panel.evaluate(
                """root => Array.from(root.querySelectorAll('.el-form-item__error'))
                  .filter(error => {
                    const style = getComputedStyle(error);
                    return error.getClientRects().length > 0
                      && style.display !== 'none' && style.visibility !== 'hidden';
                  })
                  .map(error => String(error.innerText || '').trim())
                  .filter((value, index, values) => value && values.indexOf(value) === index)"""
            )
        )

    async def apply_excel_fields(
        self, fields: JdFields, *, style_code: str
    ) -> Mapping[str, Any]:
        category = await self.apply_category()
        identity = await self.fill_identity_and_parameters(fields.fields, style_code=style_code)
        attributes = await self.fill_attributes(fields)
        sku = await self.fill_sku_batch(fields.fields)
        sku_attributes = await self.apply_sku_thickness()
        prices = await self.fill_summary_prices(fields.fields)
        delivery = await self.fill_delivery_template()
        errors = await self._visible_validation_errors()
        if errors:
            raise JdFormListingError("京东页面校验错误：" + "；".join(errors))
        return {
            "category": category,
            "identity_and_parameters": identity,
            "attributes": attributes,
            "sku_batch": sku,
            "sku_attributes": sku_attributes,
            "summary_prices": prices,
            "delivery_template": delivery,
        }

    async def verify_persisted_values(
        self, fields: JdFields, *, style_code: str
    ) -> Mapping[str, Any]:
        category = await self._category_text()
        if _category_parts(category)[-1:] != (normalize_label(JD_CATEGORY_PATH[-1]),):
            raise JdFormListingError("京东保存后类目回读失败：{0!r}".format(category))
        brand_item = await self._form_item_exact("品牌")
        brand_inputs = await self._editable_inputs(brand_item)
        brand = (await brand_inputs[0].input_value()).strip() if len(brand_inputs) == 1 else ""
        if brand != JD_BRAND:
            raise JdFormListingError("京东保存后品牌回读失败：{0!r}".format(brand))
        expected = self._expected_sku(fields.fields)
        rows = self._validate_sku(await self._sku_table_snapshot(), expected)
        prices = await self.fill_summary_prices(fields.fields)
        delivery = await self.fill_delivery_template()
        # The verifier only writes when a value is unexpectedly missing; all values
        # are still read back and compared before reporting success.
        return {
            "category": category,
            "brand": brand,
            "style_code": style_code,
            "sku_values": expected,
            "row_count": len(rows),
            "summary_prices": prices,
            "delivery_template": delivery,
        }


__all__ = [
    "JD_BATCH_ORDER",
    "JD_BRAND",
    "JD_CATEGORY_PATH",
    "JD_DELIVERY_TEMPLATE",
    "JD_SKU_THICKNESS",
    "JdFormListing",
    "JdFormListingError",
]
