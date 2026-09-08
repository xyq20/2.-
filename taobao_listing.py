"""快麦通淘宝资料页的推荐类目与 Excel 属性适配器。"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

from douyin_data import DouyinDataError, MaterialComponent, parse_materials
from sync_validation import split_or_values


class TaobaoListingError(RuntimeError):
    """可直接向用户展示的淘宝资料填写异常。"""


REMOTE_OPTION_SEARCH_SECONDS = 1.5
TAOBAO_CATEGORY_MODES = ("casual-pants", "recommended")
TAOBAO_CASUAL_PANTS_PATH = ("男装", "休闲裤")
SIZE_NAME_PATTERN = re.compile(
    r"^(?:XS|S|M|L|X{1,6}L|\d{1,2}XL)$",
    re.IGNORECASE,
)


def normalize_option(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s，,;；、・·\-_（）()]", "", text).casefold()


def normalize_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = text.replace("重要", "")
    return re.sub(r"[\s*:：]+", "", text).casefold()


def normalize_size_name(value: object) -> Optional[str]:
    normalized = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", "" if value is None else str(value)),
    ).upper()
    return normalized if SIZE_NAME_PATTERN.fullmatch(normalized) else None


def form_number(value: object) -> str:
    if isinstance(value, bool):
        raise TaobaoListingError("淘宝尺码表数值不能是布尔值")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def excel_aliases(value: object) -> Tuple[str, ...]:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    aliases: List[str] = []
    for part in text.split("/"):
        normalized = normalize_label(part)
        if not normalized:
            continue
        aliases.append(normalized)
        historical_alias = {
            "裤门禁": "裤门襟",
            "服装版型": "服饰版型",
            "服饰版型": "服装版型",
        }.get(normalized)
        if historical_alias:
            aliases.append(normalize_label(historical_alias))
    return tuple(dict.fromkeys(aliases))


# 页面字段名与产品表历史字段名之间的确定性等价关系。
TAOBAO_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("面料工艺"): tuple(
        normalize_label(value) for value in ("服饰工艺", "工艺", "工艺处理")
    ),
}


# 仅保留已经由真实淘宝候选项确认的值映射，不做模糊推断。
TAOBAO_VALUE_ALIASES: Mapping[Tuple[str, str], Tuple[str, ...]] = {
    (normalize_label("风格"), normalize_option("休闲风")): ("休闲",),
    (normalize_label("产地"), normalize_option("中国大陆")): ("中国",),
    (normalize_label("弹力"), normalize_option("无弹")): ("无弹力",),
    (normalize_label("弹力"), normalize_option("无弹力")): ("无弹",),
}


# 用户明确要求这些淘宝可选属性保持空白，即使 Excel 中存在同名字段也不填写。
TAOBAO_IGNORED_FIELDS = frozenset(
    normalize_label(value) for value in ("防水等级", "适用年龄段")
)


def value_candidates(label: object, value: object) -> Tuple[str, ...]:
    """生成平台候选；与抖音一致，斜杠严格表示从左到右的 OR。"""
    text = str(value).strip()
    alternatives = split_or_values(text)
    source_candidates = alternatives or ((text,) if text else ())
    candidates: List[str] = []
    for source_candidate in source_candidates:
        if source_candidate not in candidates:
            candidates.append(source_candidate)
        mapped = TAOBAO_VALUE_ALIASES.get(
            (normalize_label(label), normalize_option(source_candidate)),
            (),
        )
        for candidate in mapped:
            if candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def selection_value_groups(label: object, value: object) -> Tuple[Tuple[str, ...], ...]:
    """下拉选项规则：逗号分组全选，每组内斜杠是 OR。"""
    return tuple(
        value_candidates(label, group.strip())
        for group in re.split(r"[,，、;；]", str(value))
        if group.strip()
    )


def single_selection_candidates(label: object, value: object) -> Tuple[str, ...]:
    """单选字段也继承 Excel 分隔规则，但不允许逗号要求多选。"""
    groups = selection_value_groups(label, value)
    if not groups:
        raise TaobaoListingError(f"Excel 字段“{label}”没有可用值")
    if len(groups) > 1:
        rendered = "，".join("/".join(group) for group in groups)
        raise TaobaoListingError(
            f"淘宝字段“{label}”是单选，Excel 却提供了"
            f"多个逗号分组：{rendered}"
        )
    return groups[0]


def _single_source(
    fields: Mapping[str, str],
    aliases: Sequence[str],
    label: str,
) -> Optional[Tuple[str, str]]:
    wanted = {normalize_label(alias) for alias in aliases}
    matches = [
        (key, value)
        for key, value in fields.items()
        if wanted.intersection(excel_aliases(key))
    ]
    if len(matches) > 1:
        distinct_values = {str(value).strip() for _key, value in matches}
        if len(distinct_values) == 1:
            return matches[0]
        keys = "、".join(key for key, _value in matches)
        raise TaobaoListingError(f"淘宝{label}匹配到多个 Excel 字段：{keys}")
    return matches[0] if matches else None


def _required_excel_value(
    fields: Mapping[str, str],
    aliases: Sequence[str],
    label: str,
) -> str:
    source = _single_source(fields, aliases, label)
    if source is None:
        raise TaobaoListingError(
            f"Excel 中缺少淘宝{label}字段（可识别：{'/'.join(aliases)}）"
        )
    _key, value = source
    text = str(value).strip()
    if not text:
        raise TaobaoListingError(f"Excel 中的淘宝{label}为空")
    return text


def parse_taobao_materials(fields: Mapping[str, str]) -> Tuple[MaterialComponent, ...]:
    source = _single_source(fields, ("材质成分", "材质"), "材质成分")
    if source is None:
        return ()
    key, value = source
    try:
        return parse_materials(value)
    except DouyinDataError as exc:
        raise TaobaoListingError(f"Excel 字段“{key}”无法填写淘宝材质成分：{exc}") from exc


def parse_taobao_fabrics(fields: Mapping[str, str]) -> Tuple[MaterialComponent, ...]:
    source = _single_source(
        fields,
        ("面料材质", "面料俗称", "水洗标", "吊牌图", "面料"),
        "面料",
    )
    if source is None:
        return ()
    key, value = source
    try:
        return parse_materials(value)
    except DouyinDataError as exc:
        raise TaobaoListingError(f"Excel 字段“{key}”无法填写淘宝面料：{exc}") from exc


class TaobaoListing:
    """处理淘宝推荐类目、类目属性和基础销售资料。"""

    def __init__(self, page: Any, drawer: Any, logger: Any) -> None:
        self.page = page
        self.drawer = drawer
        self.logger = logger
        self.panel: Optional[Any] = None
        self.category_clicked = False
        self.material_validation: Mapping[str, Any] = {}
        self.specification_state: Mapping[str, Any] = {}

    async def _wait_for_loading_masks(self, timeout_seconds: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        raise TaobaoListingError("淘宝资料加载遮罩在 30 秒内未消失")

    async def open(self) -> "TaobaoListing":
        tab = self.drawer.get_by_role("tab", name="淘宝资料", exact=True)
        try:
            if self.logger is not None:
                self.logger.info("正在切换到淘宝资料页签")
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
        except Exception as exc:
            raise TaobaoListingError("找不到可切换的“淘宝资料”页签") from exc

        panel = self.drawer.get_by_role("tabpanel", name="淘宝资料", exact=True)
        try:
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise TaobaoListingError("淘宝资料表单未渲染") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info("淘宝资料页签已打开")
        return self

    async def _category_text(self) -> str:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        display = self.panel.locator(".platform-category-input").first
        if not await display.count():
            display = self.panel.locator(".category").first
        if not await display.count():
            raise TaobaoListingError("淘宝商品分类区域中找不到已选类目")
        try:
            direct_text = await display.evaluate(
                """element => Array.from(element.childNodes)
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => node.textContent || '')
                    .join(' ').trim()"""
            )
        except Exception:
            direct_text = ""
        if direct_text:
            return str(direct_text).strip()
        text = (await display.inner_text()).strip()
        return text.split("修改类目", 1)[0].strip()

    @staticmethod
    def _normalize_category_path(value: object) -> str:
        return re.sub(r"[\s>]", "", str(value)).casefold()

    async def apply_recommended_category(self) -> str:
        """点击当前页面唯一带“推荐”标记的类目，不依赖固定路径。"""
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")

        current = ""
        try:
            current = await self._category_text()
        except TaobaoListingError:
            pass

        buttons = self.panel.get_by_role("button", name="点击使用", exact=True)
        recommended: List[Tuple[Any, Any, str]] = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if not await button.is_visible():
                continue
            row = button.locator("xpath=..")
            row_text = re.sub(r"\s+", " ", (await row.inner_text()).strip())
            marker = row.get_by_text("推荐", exact=True)
            if await marker.count() and await marker.first.is_visible():
                recommended.append((button, row, row_text))

        if not recommended:
            if current and normalize_label(current) not in {
                "请选择",
                "请选择类目",
                "商品分类",
            }:
                if self.logger is not None:
                    self.logger.info("淘宝商品分类已有值，未重复点击推荐：%s", current)
                return current
            raise TaobaoListingError("淘宝页面没有带“推荐”标记的可用预测类目")
        if len(recommended) != 1:
            raise TaobaoListingError(
                f"淘宝页面带“推荐”标记的预测类目不是唯一项：{len(recommended)}"
            )

        button, _row, row_text = recommended[0]
        expected = re.sub(r"^\s*推荐\s*", "", row_text)
        expected = re.sub(r"\s*点击使用\s*$", "", expected)
        expected = re.sub(r"\s+", " ", expected).strip()
        if not expected:
            raise TaobaoListingError("无法读取淘宝推荐类目路径")

        if current and self._normalize_category_path(current) == self._normalize_category_path(expected):
            if self.logger is not None:
                self.logger.info("淘宝商品分类已匹配推荐项，跳过重复应用：%s", current)
            return current

        await button.scroll_into_view_if_needed()
        await button.click()
        self.category_clicked = True
        await self._wait_for_loading_masks()
        try:
            await self.panel.locator(
                ".conf .complex-wrap > .complex-item, "
                ".conf .complex-wrap > .complex-item_multi"
            ).first.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise TaobaoListingError("淘宝推荐类目应用后，类目属性未渲染") from exc

        actual = await self._category_text()
        if self._normalize_category_path(actual) != self._normalize_category_path(expected):
            raise TaobaoListingError(
                f"淘宝类目应用后校验失败：推荐 {expected!r}，页面为 {actual!r}"
            )
        if self.logger is not None:
            self.logger.info("已应用页面动态推荐的淘宝类目：%s", actual)
        return actual

    async def apply_casual_pants_category(self) -> str:
        """通过修改类目弹窗精确选择“男装 > 休闲裤”。"""
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        expected = " > ".join(TAOBAO_CASUAL_PANTS_PATH)
        try:
            current = await self._category_text()
        except TaobaoListingError:
            current = ""
        if self._normalize_category_path(current) == self._normalize_category_path(
            expected
        ):
            if self.logger is not None:
                self.logger.info("淘宝商品分类已是目标类目，跳过修改：%s", current)
            return current

        modify = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await modify.count() != 1:
            raise TaobaoListingError("淘宝页面找不到唯一的“修改类目”按钮")
        if self.logger is not None:
            self.logger.info("正在点击“修改类目”")
        await modify.click(timeout=30_000)
        # Element Dialog 使用 portal 挂载到页面根节点，不属于商品抽屉 DOM。
        dialog = self.page.get_by_role("dialog", name="修改类目", exact=True)
        try:
            await dialog.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise TaobaoListingError("淘宝修改类目弹窗未出现") from exc

        search = dialog.get_by_placeholder(
            "请输入类目关键词，支持模糊查询", exact=True
        )
        if await search.count() != 1:
            raise TaobaoListingError("淘宝修改类目弹窗缺少搜索框")
        if self.logger is not None:
            self.logger.info("正在类目弹窗顶部搜索框输入：休闲裤")

        category_response_tasks: List[asyncio.Task[Any]] = []
        category_api_hits: List[Tuple[str, Any]] = []

        async def capture_category_response(response: Any) -> None:
            try:
                headers = await response.all_headers()
                content_type = str(headers.get("content-type") or "").casefold()
                if "json" not in content_type:
                    return
                payload = await response.json()
                rendered = json.dumps(payload, ensure_ascii=False)
                if "休闲裤" in rendered:
                    category_api_hits.append((response.url, payload))
            except Exception:
                return

        def on_category_response(response: Any) -> None:
            category_response_tasks.append(
                asyncio.create_task(capture_category_response(response))
            )

        self.page.on("response", on_category_response)
        await search.fill("休闲裤", timeout=10_000)

        # 真实页面搜索后展示扁平的完整路径列表
        # （例如“男装 > 休闲裤”），只接受完整路径精确匹配。
        # Element UI 会把搜索建议通过 portal 挂到 body，而不是放在弹窗
        # 节点内部；同时包含 dialog 是为了兼容本地/旧版内嵌实现。
        search_result_nodes = self.page.locator(
            ".el-autocomplete-suggestion:visible *:visible, "
            ".el-popper:visible *:visible"
        )
        exact_search_results: List[Any] = []
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            exact_search_results = []
            matching_indexes = await search_result_nodes.evaluate_all(
                """(elements, target) => elements.map((element, index) => ({
                    index,
                    text: (element.innerText || '').normalize('NFKC')
                      .replace(/[\\s>]/g, '').toLowerCase()
                  })).filter(item => item.text === target).map(item => item.index)""",
                self._normalize_category_path(expected),
            )
            exact_search_results = [
                search_result_nodes.nth(index) for index in matching_indexes
            ]
            if exact_search_results:
                break
            await asyncio.sleep(0.05)

        self.page.remove_listener("response", on_category_response)
        if category_response_tasks:
            await asyncio.gather(*category_response_tasks, return_exceptions=True)

        api_path_confirmed = False
        for _url, payload in category_api_hits:
            stack = [payload]
            while stack:
                current_value = stack.pop()
                if isinstance(current_value, Mapping):
                    stack.extend(current_value.values())
                elif isinstance(current_value, Sequence) and not isinstance(
                    current_value, (str, bytes, bytearray)
                ):
                    stack.extend(current_value)
                elif isinstance(current_value, str) and (
                    self._normalize_category_path(current_value)
                    == self._normalize_category_path(expected)
                ):
                    api_path_confirmed = True
                    break
            if api_path_confirmed:
                break
        if self.logger is not None and category_api_hits:
            self.logger.info(
                "淘宝类目搜索 JSON 已捕获：接口=%s；目标路径=%s",
                "、".join(dict.fromkeys(url for url, _payload in category_api_hits)),
                "已确认" if api_path_confirmed else "由 DOM 精确复核",
            )

        if exact_search_results:
            # DOM 顺序中父节点先于子节点，因此第一项是包含完整路径的
            # 最外层结果节点；点击它可触发 Vue 行选择事件。
            target = exact_search_results[0]
            await target.click(timeout=10_000)
            if self.logger is not None:
                self.logger.info("已点击淘宝类目搜索结果：%s", expected)
        else:
            if self.logger is not None:
                visible_popper_text = await self.page.locator(
                    ".el-autocomplete-suggestion:visible, .el-popper:visible"
                ).evaluate_all(
                    "elements => elements.map(element => element.innerText.trim()).filter(Boolean)"
                )
                self.logger.info(
                    "淘宝类目搜索可见浮层文本：%s",
                    visible_popper_text,
                )
            raise TaobaoListingError(
                "淘宝类目搜索后未出现“男装 > 休闲裤”精确结果"
            )

        # 搜索结果点击后，以弹窗底部“已选：...”作为提交前的
        # 最终路径确认依据。
        selected_path = ""
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            lines = [line.strip() for line in (await dialog.inner_text()).splitlines()]
            selected_lines = [line for line in lines if line.startswith("已选")]
            if selected_lines:
                selected_path = selected_lines[-1].split("：", 1)[-1].strip()
                if self._normalize_category_path(selected_path) == self._normalize_category_path(
                    expected
                ):
                    break
            await asyncio.sleep(0.05)
        if self._normalize_category_path(selected_path) != self._normalize_category_path(
            expected
        ):
            raise TaobaoListingError(
                f"淘宝类目确认前路径不一致：期望 {expected}，页面为 {selected_path!r}"
            )
        confirm = dialog.get_by_role("button", name=re.compile(r"确\s*定"))
        if await confirm.count() != 1:
            raise TaobaoListingError("淘宝修改类目弹窗找不到唯一“确定”按钮")
        if self.logger is not None:
            self.logger.info("类目搜索已选中：%s，正在确认", expected)
        await confirm.click(timeout=10_000)
        await dialog.wait_for(state="hidden", timeout=30_000)
        self.category_clicked = True
        await self._wait_for_loading_masks()
        try:
            await self.panel.locator(
                ".conf .complex-wrap > .complex-item, "
                ".conf .complex-wrap > .complex-item_multi"
            ).first.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise TaobaoListingError("淘宝休闲裤类目应用后，类目属性未渲染") from exc
        actual = await self._category_text()
        if self._normalize_category_path(actual) != self._normalize_category_path(
            expected
        ):
            raise TaobaoListingError(
                f"淘宝类目应用后校验失败：期望 {expected!r}，页面为 {actual!r}"
            )
        if self.logger is not None:
            self.logger.info("已应用淘宝指定类目：%s", actual)
        return actual

    async def apply_category(self, mode: str) -> str:
        if mode == "casual-pants":
            return await self.apply_casual_pants_category()
        if mode == "recommended":
            return await self.apply_recommended_category()
        raise TaobaoListingError(f"不支持的淘宝类目模式：{mode!r}")

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        items = self.panel.locator(
            ".conf .complex-wrap > .complex-item, "
            ".conf .complex-wrap > .complex-item_multi"
        )
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        for index in range(await items.count()):
            item = items.nth(index)
            if not await item.is_visible():
                continue
            labels = item.locator(":scope > .el-form-item > .el-form-item__label")
            if not await labels.count():
                continue
            label = (await labels.first.inner_text()).strip()
            label = re.sub(r"^\s*\*\s*", "", label).replace("重要", "").strip()
            normalized = normalize_label(label)
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else f"{normalized}#{counts[normalized]}"
            result[key] = (label, item)
        if not result:
            raise TaobaoListingError("淘宝类目属性区域为空")
        return result

    async def _required_attribute_errors(
        self,
    ) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        """从当前 DOM 一次性读取必填错误。

        材质组件校验会让 Vue 重新渲染整个属性区域。如果继续逐个
        使用填写前保存的 ``nth`` locator，共享编辑器中某个序号可能已
        不存在，Playwright 会一直等到全局超时。这里只对当前快照
        做同步 DOM 查询，不保留跨重渲染的节点定位。
        """
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        records = await self.panel.evaluate(
            """root => {
              const visible = element => {
                const style = getComputedStyle(element);
                return Boolean(element.getClientRects().length)
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const selector = [
                '.conf .complex-wrap > .complex-item',
                '.conf .complex-wrap > .complex-item_multi'
              ].join(', ');
              return [...root.querySelectorAll(selector)]
                .filter(visible)
                .map(item => {
                  const form = item.querySelector(':scope > .el-form-item');
                  if (!form || !form.classList.contains('is-required')) return null;
                  const label = form.querySelector(':scope > .el-form-item__label');
                  const content = form.querySelector(':scope > .el-form-item__content');
                  const errors = content
                    ? [...content.querySelectorAll(':scope > .el-form-item__error')]
                        .filter(visible)
                        .map(error => String(error.innerText || '').trim())
                        .filter(Boolean)
                    : [];
                  if (!errors.length) return null;
                  return {
                    label: String((label && label.innerText) || '').trim(),
                    errors
                  };
                })
                .filter(Boolean);
            }"""
        )
        result = []
        for record in records:
            label = re.sub(
                r"^\s*\*\s*", "", str(record.get("label") or "")
            ).replace("重要", "").strip()
            errors = tuple(str(value).strip() for value in record.get("errors", ()) if str(value).strip())
            if label and errors:
                result.append((label, errors))
        return tuple(result)

    async def _open_select(self, select: Any, *, multi: bool) -> None:
        if multi:
            search = select.locator(".el-select__tags input.el-select__input").first
            if await search.count():
                await search.click(timeout=4000)
                return
        input_box = select.locator("input.el-input__inner").first
        if not await input_box.count():
            raise TaobaoListingError("淘宝属性下拉框中找不到可点击输入框")
        await input_box.click(timeout=4000)

    async def _dismiss_select_dropdown(self, select: Any) -> None:
        inputs = select.locator("input")
        if await inputs.count():
            try:
                await inputs.first.press("Escape", timeout=2000)
            except Exception:
                await self.page.keyboard.press("Escape")
        else:
            await self.page.keyboard.press("Escape")
        deadline = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < deadline:
            if not await self.page.locator(".el-select-dropdown:visible").count():
                return
            await asyncio.sleep(0.05)
        raise TaobaoListingError("未匹配淘宝属性值的下拉层无法关闭")

    async def _active_select_dropdown(self, select: Any) -> Optional[Any]:
        local = select.locator(".el-select-dropdown:visible")
        if await local.count() == 1:
            return local.first
        if await local.count() > 1:
            raise TaobaoListingError("当前淘宝属性内部出现多个可见下拉框")
        global_dropdowns = self.page.locator(".el-select-dropdown:visible")
        count = await global_dropdowns.count()
        if count == 0:
            return None
        if count == 1:
            return global_dropdowns.first
        raise TaobaoListingError(f"页面同时存在 {count} 个淘宝属性下拉框")

    async def _visible_dom_options(
        self,
        select: Any,
        timeout_seconds: float = 5,
    ) -> Tuple[Any, List[Mapping[str, Any]]]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            dropdown = await self._active_select_dropdown(select)
            if dropdown is None:
                await asyncio.sleep(0.05)
                continue
            result = await dropdown.evaluate(
                """element => Array.from(
                    element.querySelectorAll('.el-select-dropdown__item')
                  ).map((option, index) => ({
                    name: (option.innerText || '').trim(),
                    index: String(index),
                    value: (() => {
                      const raw = option.__vue__ && option.__vue__.value;
                      if (raw === undefined || raw === null) return '';
                      if (typeof raw !== 'object') return String(raw);
                      try { return JSON.stringify(raw); } catch (_error) { return '[object]'; }
                    })(),
                    created: Boolean(option.__vue__ && option.__vue__.created),
                    disabled: option.classList.contains('is-disabled'),
                    visible: option.getClientRects().length > 0
                      && getComputedStyle(option).display !== 'none'
                      && getComputedStyle(option).visibility !== 'hidden'
                  })).filter(item => item.name && !item.disabled && item.visible)
                """
            )
            if result:
                return dropdown, result
            await asyncio.sleep(0.05)
        raise TaobaoListingError("打开淘宝属性下拉框后未找到可见选项")

    async def _clear_multi_select(self, select: Any) -> None:
        while True:
            close_buttons = select.locator(".el-select__tags .el-tag__close")
            before = await close_buttons.count()
            if before == 0:
                return
            await close_buttons.first.click()
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                if await close_buttons.count() < before:
                    break
                await asyncio.sleep(0.02)
            else:
                raise TaobaoListingError("清空淘宝属性多选值失败")

    async def _read_select_values(self, select: Any, *, multi: bool) -> Tuple[str, ...]:
        if multi:
            tags = select.locator(".el-select__tags .el-tag")
            values = []
            for index in range(await tags.count()):
                text = (await tags.nth(index).inner_text()).strip()
                if text:
                    values.append(text)
            return tuple(values)
        input_box = select.locator("input.el-input__inner").first
        return ((await input_box.input_value()).strip(),)

    async def _search_select(self, select: Any, expected: str) -> bool:
        return bool(
            await select.evaluate(
                """async (element, value) => {
                  const input = Array.from(element.querySelectorAll('input'))
                    .find(node => !node.hasAttribute('readonly'));
                  if (!input) return false;
                  input.focus();
                  const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                  ).set;
                  setter.call(input, value);
                  input.dispatchEvent(new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                  }));
                  input.dispatchEvent(new Event('change', {bubbles: true}));
                  const component = element.__vue__;
                  if (component) {
                    component.query = value;
                    if (typeof component.handleQueryChange === 'function') {
                      component.handleQueryChange(value);
                    }
                    if (typeof component.$nextTick === 'function') {
                      await new Promise(resolve => component.$nextTick(resolve));
                    }
                  }
                  return true;
                }""",
                expected,
            )
        )

    @staticmethod
    def _matching_options(
        expected: str,
        options: Sequence[Mapping[str, Any]],
    ) -> List[Mapping[str, Any]]:
        return [
            option
            for option in options
            if normalize_option(option.get("name", "")) == normalize_option(expected)
        ]

    @classmethod
    def _matching_option(
        cls,
        expected: str,
        options: Sequence[Mapping[str, Any]],
    ) -> Optional[Mapping[str, Any]]:
        matches = cls._matching_options(expected, options)
        # 淘宝少数类目会返回多个同名但不同 ID 的选项。没有额外证据时
        # 不任选其一；交给调用方尝试下一个 OR 候选或记录为跳过。
        if len(matches) > 1:
            return None
        return matches[0] if matches else None

    async def _choose_one_option(
        self,
        select: Any,
        candidates: Sequence[str],
        *,
        label: str,
        multi: bool,
    ) -> Optional[str]:
        await self._open_select(select, multi=multi)
        dropdown, options = await self._visible_dom_options(select)
        initial_option_names = tuple(
            dict.fromkeys(
                str(option.get("name", "")).strip()
                for option in options
                if str(option.get("name", "")).strip()
            )
        )
        custom_allowed = (
            (await select.get_attribute("caninputcustom") or "").casefold() == "true"
        )
        created_exact_click_allowed = bool(
            getattr(self, "allow_created_exact_dom_option", False)
        )
        chosen = None
        chosen_text = ""
        ambiguous_initial = set()
        observed_matches: Dict[str, List[Mapping[str, Any]]] = {}
        for candidate in candidates:
            matches = [
                match
                for match in self._matching_options(candidate, options)
                if not match.get("created")
                or custom_allowed
                or created_exact_click_allowed
            ]
            observed_matches[candidate] = list(matches)
            if len(matches) > 1:
                ambiguous_initial.add(normalize_option(candidate))
                continue
            chosen = matches[0] if matches else None
            if (
                chosen is not None
                and chosen.get("created")
                and not custom_allowed
                and not created_exact_click_allowed
            ):
                chosen = None
            if chosen is not None:
                chosen_text = candidate
                break

        # OR 值允许任一候选。若没有唯一项，但某个候选存在多个同名的
        # 平台真实选项，则按 Excel 优先级和平台展示顺序选第一个。
        # 单值字段仍保留重名拒绝策略，避免把这一例外扩散到普通属性。
        if chosen is None and len(candidates) > 1:
            for candidate in candidates:
                matches = observed_matches.get(candidate, [])
                if len(matches) <= 1:
                    continue
                chosen = matches[0]
                chosen_text = candidate
                if self.logger is not None:
                    self.logger.info(
                        "淘宝属性“%s”：OR 候选 %s 有 %s 个同名平台项，"
                        "按平台顺序选择第一个[%s]",
                        label,
                        candidate,
                        len(matches),
                        chosen.get("value", ""),
                    )
                break

        if chosen is None:
            for candidate in candidates:
                # 当前候选列表中已确认重名的值不会因为重复搜索而变唯一；
                # 直接尝试下一个 OR 候选，避免口袋/多口袋之间反复切换。
                if normalize_option(candidate) in ambiguous_initial:
                    continue
                if not await self._search_select(select, candidate):
                    continue
                search_deadline = (
                    asyncio.get_running_loop().time() + REMOTE_OPTION_SEARCH_SECONDS
                )
                while asyncio.get_running_loop().time() < search_deadline:
                    await asyncio.sleep(0.1)
                    try:
                        dropdown, options = await self._visible_dom_options(
                            select,
                            timeout_seconds=0.35,
                        )
                    except TaobaoListingError:
                        continue
                    chosen = self._matching_option(candidate, options)
                    observed_matches[candidate] = self._matching_options(candidate, options)
                    if (
                        chosen is not None
                        and chosen.get("created")
                        and not custom_allowed
                        and not created_exact_click_allowed
                    ):
                        chosen = None
                    if chosen is not None:
                        chosen_text = candidate
                        break
                if chosen is not None:
                    break

        if chosen is None:
            has_platform_match = any(observed_matches.values())
            if not has_platform_match and custom_allowed:
                custom_value = candidates[0]
                custom_applied = await select.evaluate(
                    """async (element, value) => {
                      const component = element.__vue__;
                      if (!component || typeof component.handleOptionSelect !== 'function') {
                        return false;
                      }
                      const customOption = {
                        value,
                        currentLabel: value,
                        label: value,
                        created: true,
                        disabled: false,
                        visible: true
                      };
                      component.handleOptionSelect(customOption, true);
                      if (typeof component.$nextTick === 'function') {
                        await new Promise(resolve => component.$nextTick(resolve));
                      }
                      if (!component.multiple) {
                        component.selectedLabel = value;
                        component.query = value;
                      }
                      return true;
                    }""",
                    custom_value,
                )
                if custom_applied:
                    deadline = asyncio.get_running_loop().time() + 2
                    while asyncio.get_running_loop().time() < deadline:
                        actual = await self._read_select_values(select, multi=multi)
                        if any(
                            normalize_option(value) == normalize_option(custom_value)
                            for value in actual
                        ):
                            if multi:
                                await self._dismiss_select_dropdown(select)
                            if self.logger is not None:
                                self.logger.info(
                                    "淘宝属性“%s”：平台无候选，已直接填写 Excel 值 %s",
                                    label,
                                    custom_value,
                                )
                            return custom_value
                        await asyncio.sleep(0.05)
                raise TaobaoListingError(
                    f"淘宝属性“{label}”没有平台候选，直接填写 Excel 值"
                    f"“{custom_value}”后回读失败"
                )

            if not has_platform_match and not custom_allowed:
                if self.logger is not None:
                    self.logger.info(
                        "淘宝属性“%s”没有平台候选，且该下拉不支持自定义值；"
                        "跳过 Excel 值：%s；初始下拉候选：%s",
                        label,
                        "/".join(candidates),
                        " / ".join(initial_option_names) or "无",
                    )
                await self._dismiss_select_dropdown(select)
                return None

            if self.logger is not None:
                details = []
                for candidate in candidates:
                    matches = observed_matches.get(candidate, [])
                    rendered = ", ".join(
                        f"{match.get('name', '')}[{match.get('value', '')}]"
                        for match in matches
                    ) or "无"
                    details.append(f"{candidate}=>{rendered}")
                self.logger.info(
                    "淘宝属性“%s”候选检查未命中唯一项：%s",
                    label,
                    "；".join(details),
                )
            await self._dismiss_select_dropdown(select)
            return None

        if len(candidates) > 1 and self.logger is not None:
            self.logger.info(
                "淘宝属性“%s”：候选优先使用平台已有值 %s",
                label,
                chosen_text,
            )

        clicked = await dropdown.evaluate(
            """(element, index) => {
                const option = element.querySelectorAll(
                  '.el-select-dropdown__item'
                )[index];
                if (!option || option.classList.contains('is-disabled')) return false;
                option.scrollIntoView({block: 'nearest'});
                option.click();
                return true;
            }""",
            int(chosen["index"]),
        )
        if not clicked:
            raise TaobaoListingError(f"淘宝属性“{label}”的精确候选节点已失效")
        return chosen_text

    async def _select_values(
        self,
        select: Any,
        expected_groups: Sequence[Sequence[str]],
        *,
        label: str,
        multi: bool,
    ) -> Optional[Tuple[str, ...]]:
        current = await self._read_select_values(select, multi=multi)
        if len(expected_groups) == 1 and any(
            normalize_option(current_value) == normalize_option(candidate)
            for current_value in current
            for candidate in expected_groups[0]
        ):
            return current

        if multi:
            expected_existing = []
            for group in expected_groups:
                match = next(
                    (
                        current_value
                        for current_value in current
                        if any(
                            normalize_option(current_value) == normalize_option(candidate)
                            for candidate in group
                        )
                    ),
                    None,
                )
                if match is not None:
                    expected_existing.append(match)
            if len(expected_existing) == len(expected_groups):
                return tuple(expected_existing)
            await self._clear_multi_select(select)

        chosen_values = []
        for group in expected_groups:
            chosen = await self._choose_one_option(
                select,
                group,
                label=label,
                multi=multi,
            )
            if chosen is None:
                if multi:
                    await self._clear_multi_select(select)
                return None
            chosen_values.append(chosen)

        actual = await self._read_select_values(select, multi=multi)
        expected_counter = Counter(normalize_option(value) for value in chosen_values)
        actual_counter = Counter(normalize_option(value) for value in actual)
        if actual_counter != expected_counter:
            raise TaobaoListingError(
                f"淘宝属性“{label}”选择后校验失败："
                f"期望 {chosen_values!r}，页面为 {list(actual)!r}"
            )
        # 每个字段回读成功后都关闭其候选层。淘宝的可自定义
        # 单选项通过 Vue 组件写入时不会自动收起，否则下一个字段
        # 会与上一个残留层同时可见。
        await self._dismiss_select_dropdown(select)
        return actual

    async def fill_attribute(
        self,
        label: str,
        expected: object,
        *,
        exact_values: Optional[Sequence[str]] = None,
        item: Optional[Any] = None,
    ) -> Optional[Tuple[str, ...]]:
        page_label = label
        if item is None:
            items = await self._attribute_items()
            records = [
                record
                for key, record in items.items()
                if key.split("#", 1)[0] == normalize_label(label)
            ]
            if len(records) != 1:
                raise TaobaoListingError(
                    f"当前淘宝类目中属性“{label}”匹配数为 {len(records)}"
                )
            page_label, item = records[0]
        await item.scroll_into_view_if_needed()

        selects = item.locator(":scope > .el-form-item > .el-form-item__content .el-select")
        standalone_inputs = item.locator(
            ":scope > .el-form-item > .el-form-item__content input:not([readonly])"
        )
        visible_inputs = []
        for index in range(await standalone_inputs.count()):
            candidate = standalone_inputs.nth(index)
            if await candidate.is_visible() and not await candidate.evaluate(
                "element => Boolean(element.closest('.el-select'))"
            ):
                visible_inputs.append(candidate)

        if normalize_label(page_label) == normalize_label("吊牌价"):
            if len(visible_inputs) != 1:
                raise TaobaoListingError("淘宝吊牌价找不到唯一数值输入框")
            number = str(expected).strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", number):
                raise TaobaoListingError(f"淘宝吊牌价不是有效数字：{expected!r}")
            input_box = visible_inputs[0]
            if (await input_box.input_value()).strip() != number:
                await input_box.fill(number)
                await input_box.press("Tab")
            actual = (await input_box.input_value()).strip()
            if actual != number:
                raise TaobaoListingError(
                    f"淘宝吊牌价填写后校验失败：期望 {number!r}，页面为 {actual!r}"
                )
            return (actual,)

        if await selects.count():
            if await selects.count() != 1:
                raise TaobaoListingError(f"淘宝属性“{page_label}”下拉框不唯一")
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            if exact_values is not None:
                groups = tuple((str(value),) for value in exact_values)
            else:
                groups = selection_value_groups(page_label, expected)
                if not groups:
                    raise TaobaoListingError(f"淘宝属性“{page_label}”期望值为空")
            if not multi and len(groups) > 1:
                rendered = "，".join("/".join(group) for group in groups)
                raise TaobaoListingError(
                    f"淘宝属性“{page_label}”是单选，Excel 却提供了"
                    f"多个逗号分组：{rendered}"
                )
            return await self._select_values(
                select,
                groups,
                label=page_label,
                multi=multi,
            )

        if len(visible_inputs) != 1:
            raise TaobaoListingError(
                f"淘宝属性“{page_label}”不是唯一的下拉或文本输入控件"
            )
        expected_text = str(expected).strip()
        input_box = visible_inputs[0]
        if (await input_box.input_value()).strip() != expected_text:
            await input_box.fill(expected_text)
            await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if actual != expected_text:
            raise TaobaoListingError(
                f"淘宝属性“{page_label}”填写后校验失败："
                f"期望 {expected_text!r}，页面为 {actual!r}"
            )
        return (actual,)

    async def _material_rows(self, item: Any) -> List[Any]:
        rows = item.locator(".multi-complex-items > div")
        material_rows = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            select_input = row.locator(".el-select input.el-input__inner").first
            inputs = row.locator("input")
            percentage_inputs = []
            for input_index in range(await inputs.count()):
                candidate = inputs.nth(input_index)
                if not await candidate.is_visible():
                    continue
                if await candidate.evaluate(
                    "element => Boolean(element.closest('.el-select'))"
                ):
                    continue
                percentage_inputs.append(candidate)
            if not await select_input.count() or len(percentage_inputs) != 1:
                continue
            material_rows.append(row)
        return material_rows

    async def _read_materials(self, item: Any) -> Tuple[Tuple[str, int], ...]:
        actual = []
        for index, row in enumerate(await self._material_rows(item)):
            select_input = row.locator(".el-select input.el-input__inner").first
            percentage_box = None
            inputs = row.locator("input")
            for input_index in range(await inputs.count()):
                candidate = inputs.nth(input_index)
                if not await candidate.is_visible():
                    continue
                if await candidate.evaluate(
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
            if not percentage_text.isdigit():
                raise TaobaoListingError(
                    f"淘宝材质成分第 {index + 1} 行含量不是整数：{percentage_text!r}"
                )
            actual.append((name, int(percentage_text)))
        return tuple(actual)

    async def _sync_material_component(
        self,
        item: Any,
        expected: Sequence[Tuple[str, int]],
    ) -> Mapping[str, Any]:
        """用页面自身的多实例组件同步材质数据。

        DOM 输入仍用于真实交互和回读；这一步确保 Vuex 中的
        material_prop_name/material_prop_content 也同步，避免页面
        虽显示“棉/100”却仍被自身校验器判空。
        """
        return await item.evaluate(
            """async (root, expectedRows) => {
              const host = root.querySelector('.multi-complex-items');
              const clean = value => String(value == null ? '' : value)
                .normalize('NFKC').replace(/[\\s*:：%()（）]+/g, '').toLowerCase();
              const elements = [root, ...root.querySelectorAll('*')];
              let ancestor = root.parentElement;
              for (let depth = 0; ancestor && depth < 8; depth += 1) {
                elements.push(ancestor);
                ancestor = ancestor.parentElement;
              }
              const seen = new Set();
              const components = [];
              for (const element of [host, ...elements]) {
                const candidate = element && element.__vue__;
                if (!candidate || seen.has(candidate)) continue;
                seen.add(candidate);
                if (!Array.isArray(candidate.childrenUI)
                    || !Array.isArray(candidate.list)
                    || typeof candidate.updateForm !== 'function') continue;
                components.push(candidate);
              }
              const component = components.find(candidate => {
                const children = candidate.childrenUI || [];
                const hasMaterial = children.some(ui =>
                  (ui && ui.name === 'material_prop_name')
                  || clean(ui && ui.label) === clean('材质'));
                const hasContent = children.some(ui =>
                  (ui && ui.name === 'material_prop_content')
                  || clean(ui && ui.label).includes(clean('含量')));
                return hasMaterial && hasContent;
              });
              if (!component) {
                return {
                  found: false,
                  key: '',
                  rows: [],
                  candidateKeys: components.map(candidate => String(candidate.key || ''))
                };
              }
              const byName = name => component.childrenUI.find(ui => ui && ui.name === name);
              const materialUi = byName('material_prop_name')
                || component.childrenUI.find(ui => clean(ui && ui.label) === clean('材质'));
              const contentUi = byName('material_prop_content')
                || component.childrenUI.find(ui => clean(ui && ui.label).includes(clean('含量')));
              if (!materialUi || !contentUi) {
                return {
                  found: true,
                  key: String(component.key || ''),
                  error: '找不到材质或含量子字段',
                  children: component.childrenUI.map(ui => ({
                    name: String((ui && ui.name) || ''),
                    label: String((ui && ui.label) || '')
                  }))
                };
              }
              if (component.list.length !== expectedRows.length) {
                return {
                  found: true,
                  key: String(component.key || ''),
                  error: `材质行数 ${component.list.length} != ${expectedRows.length}`
                };
              }
              const options = Array.isArray(materialUi.options)
                ? materialUi.options
                : (materialUi.component
                  && materialUi.component.props
                  && materialUi.component.props.dataSource
                  && Array.isArray(materialUi.component.props.dataSource.options)
                    ? materialUi.component.props.dataSource.options : []);
              const next = component.list.map((row, index) => {
                const [name, percentage] = expectedRows[index];
                const matches = options.filter(option =>
                  clean(option && option.displayName) === clean(name));
                if (matches.length !== 1) {
                  throw new Error(
                    `材质“${name}”平台精确值匹配数为 ${matches.length}`);
                }
                return Object.assign({}, row, {
                  [materialUi.name]: matches[0].value,
                  [contentUi.name]: String(percentage)
                });
              });
              component.updateForm({[component.key]: next});
              if (typeof component.$nextTick === 'function') {
                await new Promise(resolve => {
                  let settled = false;
                  const finish = () => {
                    if (settled) return;
                    settled = true;
                    resolve();
                  };
                  const timer = setTimeout(finish, 500);
                  try {
                    component.$nextTick(() => {
                      clearTimeout(timer);
                      finish();
                    });
                  } catch (_error) {
                    clearTimeout(timer);
                    finish();
                  }
                });
              }
              const rows = component.list.map(row => Object.fromEntries(
                Object.entries(row || {}).map(([key, value]) => [
                  key, value === undefined ? '<undefined>' : value
                ])
              ));
              return {found: true, key: String(component.key || ''), rows};
            }""",
            tuple((name, percentage) for name, percentage in expected),
        )

    async def fill_materials(
        self,
        materials: Sequence[MaterialComponent],
    ) -> Tuple[Tuple[str, int], ...]:
        items = await self._attribute_items()
        record = items.get(normalize_label("材质成分"))
        if record is None:
            raise TaobaoListingError("当前淘宝类目中找不到属性“材质成分”")
        _label, item = record
        expected = tuple((material.name, material.percentage) for material in materials)
        if not expected:
            raise TaobaoListingError("Excel 中没有可用于淘宝材质成分的内容")
        if sum(percentage for _name, percentage in expected) != 100:
            raise TaobaoListingError("淘宝材质成分含量合计必须为 100")

        async def sync_and_validate_component() -> None:
            if self.logger is not None:
                self.logger.info("淘宝材质成分：开始同步页面内部组件")
            component_state = await self._sync_material_component(item, expected)
            if component_state.get("error"):
                raise TaobaoListingError(
                    "淘宝材质成分组件同步失败："
                    + str(component_state["error"])
                )
            if not component_state.get("found") or not component_state.get("key"):
                raise TaobaoListingError(
                    "淘宝材质成分已显示，但无法定位页面内部表单组件："
                    + str(component_state)
                )
            if self.logger is not None:
                self.logger.info("淘宝材质成分：组件同步完成，开始父表单校验")
            form_validation = await self._revalidate_form_property(
                str(component_state["key"])
            )
            dom_validation = await item.evaluate(
                """root => {
                  const visible = element => {
                    const style = getComputedStyle(element);
                    return Boolean(element.getClientRects().length)
                      && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const describe = component => {
                    if (!component) return null;
                    const scalar = {};
                    for (const [key, value] of Object.entries(component)) {
                      if (!/(valid|error|empty|material|prop)/i.test(key)) continue;
                      if (value == null || ['string', 'number', 'boolean'].includes(typeof value)) {
                        scalar[key] = value;
                      }
                    }
                    return {
                      name: String((component.$options && component.$options.name) || ''),
                      prop: String(component.prop || ''),
                      validateState: String(component.validateState || ''),
                      validateMessage: String(component.validateMessage || ''),
                      scalar
                    };
                  };
                  return [...root.querySelectorAll('.el-form-item__error')]
                    .filter(visible)
                    .map(error => {
                      const chain = [];
                      let element = error;
                      for (let depth = 0; element && depth < 8; depth += 1) {
                        if (element.__vue__) chain.push(describe(element.__vue__));
                        element = element.parentElement;
                      }
                      return {
                        text: String(error.innerText || '').trim(),
                        parentClass: String((error.parentElement && error.parentElement.className) || ''),
                        chain
                      };
                    });
                }"""
            )
            self.material_validation = {
                "component": component_state,
                "form": form_validation,
                "dom": dom_validation,
            }
            if form_validation.get("message"):
                raise TaobaoListingError(
                    "淘宝材质成分组件校验失败："
                    + str(form_validation["message"])
                )
            if self.logger is not None:
                self.logger.info("淘宝材质成分：父表单校验完成")

        if await self._read_materials(item) == expected:
            await sync_and_validate_component()
            return expected

        await item.scroll_into_view_if_needed()
        while await self._material_rows(item):
            rows = await self._material_rows(item)
            remove = rows[-1].get_by_text("移除", exact=True)
            if await remove.count() != 1:
                raise TaobaoListingError("淘宝材质成分已有内容，但找不到唯一“移除”按钮")
            before = len(rows)
            await remove.click()
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                if len(await self._material_rows(item)) < before:
                    break
                await asyncio.sleep(0.05)
            else:
                raise TaobaoListingError("移除淘宝材质成分旧行失败")

        add_button = item.get_by_role("button", name="添加", exact=True)
        if await add_button.count() != 1:
            raise TaobaoListingError("淘宝材质成分找不到唯一“添加”按钮")

        for index, (name, percentage) in enumerate(expected):
            before = len(await self._material_rows(item))
            await add_button.click()
            deadline = asyncio.get_running_loop().time() + 3
            rows = await self._material_rows(item)
            while len(rows) != before + 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                rows = await self._material_rows(item)
            if len(rows) != before + 1:
                raise TaobaoListingError(f"点击“添加”后未生成材质成分第 {index + 1} 行")

            row = rows[-1]
            selects = row.locator(".el-select")
            if await selects.count() != 1:
                raise TaobaoListingError(
                    f"淘宝材质成分第 {index + 1} 行找不到唯一材质下拉框"
                )
            actual_name = await self._select_values(
                selects.first,
                ((name,),),
                label=f"材质成分第 {index + 1} 行材质",
                multi=False,
            )
            if actual_name is None:
                raise TaobaoListingError(
                    f"淘宝材质成分第 {index + 1} 行没有 Excel 材质“{name}”的精确候选"
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
                raise TaobaoListingError(
                    f"淘宝材质成分第 {index + 1} 行找不到唯一含量输入框"
                )
            await percentage_inputs[0].fill(str(percentage))
            await percentage_inputs[0].press("Tab")

        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            actual = await self._read_materials(item)
            if actual == expected:
                await sync_and_validate_component()
                return actual
            await asyncio.sleep(0.05)
        raise TaobaoListingError(
            f"淘宝材质成分填写后校验失败：期望 {expected!r}，页面为 {actual!r}"
        )

    @staticmethod
    def _material_validation_is_confirmed(
        validation: Mapping[str, Any],
    ) -> bool:
        """只在材质值、合计和 Element Form 状态均一致时认定有效。"""
        component = validation.get("component")
        form = validation.get("form")
        if not isinstance(component, Mapping) or not isinstance(form, Mapping):
            return False
        if not component.get("found") or not component.get("key"):
            return False
        if not form.get("found") or str(form.get("message") or "").strip():
            return False

        def normalized_rows(value: Any) -> Optional[Tuple[Tuple[str, int], ...]]:
            if not isinstance(value, list) or not value:
                return None
            rows = []
            for row in value:
                if not isinstance(row, Mapping):
                    return None
                name = str(row.get("material_prop_name") or "").strip()
                content = str(row.get("material_prop_content") or "").strip()
                if not name or not content.isdigit():
                    return None
                rows.append((name, int(content)))
            return tuple(rows)

        expected_rows = normalized_rows(component.get("rows"))
        if expected_rows is None or sum(content for _name, content in expected_rows) != 100:
            return False
        fields = form.get("fields")
        if not isinstance(fields, list) or not fields:
            return False
        for field in fields:
            if not isinstance(field, Mapping):
                return False
            if str(field.get("prop") or "") != str(component.get("key") or ""):
                return False
            if str(field.get("validateState") or "").strip():
                return False
            if str(field.get("validateMessage") or "").strip():
                return False
            if normalized_rows(field.get("fieldValue")) != expected_rows:
                return False
        return True

    async def _build_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        target_sources: Dict[str, List[Tuple[str, str]]] = {}
        special = {
            normalize_label("面料"),
            normalize_label("材质成分"),
            *TAOBAO_IGNORED_FIELDS,
        }
        for key, value in fields.items():
            source_aliases = set(excel_aliases(key))
            for normalized_page, (page_label, _item) in page_items.items():
                base_normalized = normalized_page.split("#", 1)[0]
                if base_normalized in special:
                    continue
                accepted = {base_normalized}
                accepted.update(TAOBAO_FIELD_ALIASES.get(base_normalized, ()))
                if source_aliases.intersection(accepted):
                    target_sources.setdefault(normalized_page, []).append((key, value))

        assignments: Dict[str, Tuple[str, str]] = {}
        for normalized_page, (page_label, _item) in page_items.items():
            if normalized_page.split("#", 1)[0] in special:
                continue
            sources = target_sources.get(normalized_page, [])
            if len(sources) > 1:
                keys = "、".join(key for key, _value in sources)
                raise TaobaoListingError(
                    f"淘宝属性“{page_label}”匹配到多个 Excel 字段：{keys}"
                )
            if sources:
                assignments[normalized_page] = (page_label, sources[0][1])
        return assignments

    async def apply_excel_attributes(
        self,
        fields: Any,
        *,
        category_mode: str = "recommended",
    ) -> Mapping[str, Any]:
        """按所选类目模式应用类目，并匹配填写 Excel。"""
        category = await self.apply_category(category_mode)
        page_items = await self._attribute_items()
        source_fields = fields.fields
        assignments = await self._build_assignments(source_fields, page_items)
        materials = parse_taobao_materials(source_fields)
        fabrics = parse_taobao_fabrics(source_fields)

        applied: Dict[str, Tuple[str, ...]] = {}
        skipped_values: Dict[str, str] = {}
        fabric_label = normalize_label("面料")
        materials_label = normalize_label("材质成分")
        for normalized_page, (page_label, _item) in page_items.items():
            base_normalized = normalized_page.split("#", 1)[0]
            occurrence = normalized_page.split("#", 1)[1] if "#" in normalized_page else ""
            report_label = f"{page_label}#{occurrence}" if occurrence else page_label
            if base_normalized in TAOBAO_IGNORED_FIELDS:
                continue

            if base_normalized == fabric_label:
                if self.logger is not None:
                    self.logger.info("正在填写淘宝属性：%s", page_label)
                if not fabrics:
                    skipped_values[report_label] = "Excel 未提供可解析的面料字段"
                    continue
                fabric_names = tuple(material.name for material in fabrics)
                actual = await self.fill_attribute(
                    page_label,
                    "/".join(fabric_names),
                    exact_values=fabric_names,
                    item=_item,
                )
                if actual is None:
                    skipped_values[report_label] = "/".join(fabric_names)
                else:
                    applied[report_label] = actual
                continue

            if base_normalized == materials_label:
                if self.logger is not None:
                    self.logger.info("正在填写淘宝属性：%s", page_label)
                material_rows = await self.fill_materials(materials)
                applied[report_label] = tuple(
                    f"{name}{percentage}%" for name, percentage in material_rows
                )
                continue

            assignment = assignments.get(normalized_page)
            if assignment is None:
                continue
            _assigned_label, value = assignment
            if self.logger is not None:
                self.logger.info("正在填写淘宝属性：%s", page_label)
            actual = await self.fill_attribute(page_label, value, item=_item)
            if actual is None:
                skipped_values[report_label] = str(value)
                if self.logger is not None:
                    self.logger.info(
                        "淘宝属性“%s”无平台精确候选，已跳过 Excel 值：%s",
                        page_label,
                        value,
                    )
                continue
            applied[report_label] = actual

        if self.logger is not None:
            self.logger.info("淘宝属性填写完成，正在复核当前必填状态")
        required_errors = []
        for page_label, error_texts in await self._required_attribute_errors():
            stale_material_error = (
                normalize_label(page_label) == materials_label
                and set(error_texts) == {"材质成分子项请勿留空"}
                and self._material_validation_is_confirmed(self.material_validation)
            )
            if stale_material_error:
                self.material_validation["ignored_dom_errors"] = error_texts
                if self.logger is not None:
                    self.logger.info(
                        "淘宝材质成分内部值与父表单校验已确认，"
                        "忽略 Element UI 未移除的陈旧提示：%s",
                        "、".join(error_texts),
                    )
                continue
            required_errors.append(page_label)
        if self.logger is not None:
            self.logger.info("淘宝当前必填状态复核完成")
        if required_errors:
            if self.logger is not None and normalize_label("材质成分") in {
                normalize_label(label) for label in required_errors
            }:
                self.logger.info(
                    "淘宝材质成分组件校验现场：%s",
                    self.material_validation,
                )
            raise TaobaoListingError(
                "淘宝类目必填属性仍未完成：" + "、".join(required_errors)
            )

        ignored_fields = tuple(
            page_label
            for normalized, (page_label, _item) in page_items.items()
            if normalized.split("#", 1)[0] in TAOBAO_IGNORED_FIELDS
        )
        if ignored_fields and self.logger is not None:
            self.logger.info(
                "淘宝属性按规则保持空白：%s",
                "、".join(ignored_fields),
            )

        report = {
            "category": category,
            "category_clicked": self.category_clicked,
            "attributes": applied,
            "skipped_values": skipped_values,
            "ignored_fields": ignored_fields,
            "material_validation": self.material_validation,
        }
        if self.logger is not None:
            self.logger.info(
                "淘宝推荐类目与 Excel 属性填写完成：已填 %s 项，跳过 %s 项",
                len(applied),
                len(skipped_values),
            )
        return report

    async def fill_size_chart_lengths(
        self,
        lengths: Sequence[Any],
        garment_kind: str,
    ) -> Mapping[str, Any]:
        """按尺码文本填写淘宝尺码表中的衣长或裤长。"""
        if garment_kind not in {"pants", "clothing"}:
            raise TaobaoListingError(f"不支持的淘宝尺码类型：{garment_kind!r}")
        field_label = "裤长（cm）" if garment_kind == "pants" else "衣长（cm）"
        by_size: Dict[str, str] = {}
        for item in lengths:
            size = normalize_size_name(getattr(item, "size", None))
            if size is None:
                raise TaobaoListingError(
                    f"淘宝尺码表识别结果包含无效尺码：{getattr(item, 'size', None)!r}"
                )
            if size in by_size:
                raise TaobaoListingError(f"淘宝尺码表识别结果包含重复尺码：{size}")
            by_size[size] = form_number(getattr(item, "length", None))
        if not by_size:
            raise TaobaoListingError("淘宝尺码表识别结果为空")

        wrap = await self._wrap_item("尺码表")
        async def find_parameter_label(name: str) -> Any:
            matches = []
            labels = wrap.locator("label.el-checkbox")
            for label_index in range(await labels.count()):
                label = labels.nth(label_index)
                checkbox = label.locator('input[type="checkbox"]').first
                if not await checkbox.count():
                    continue
                if normalize_label(await label.inner_text()) == normalize_label(name):
                    matches.append(label)
            if len(matches) != 1:
                raise TaobaoListingError(
                    f"淘宝尺码表参数“{name}”匹配数为 {len(matches)}"
                )
            return matches[0]

        await self._wait_for_loading_masks()
        # 类目切换后尺码组件可能二次重绘。每次操作都重新定位当前组件，
        # 并以新 DOM 的 checked 状态作为成功标准。
        labels = wrap.locator("label.el-checkbox")
        for index in range(await labels.count()):
            label = labels.nth(index)
            checkbox = label.locator('input[type="checkbox"]').first
            if not await checkbox.count():
                continue
            text = (await label.inner_text()).strip()
            if (
                normalize_label(text) != normalize_label(field_label)
                and await checkbox.is_checked()
            ):
                await label.click(timeout=10_000, force=True)

        target_selected = False
        for _attempt in range(4):
            target_label = await find_parameter_label(field_label)
            target_checkbox = target_label.locator('input[type="checkbox"]').first
            if await target_checkbox.is_checked():
                target_selected = True
                break
            await target_label.click(timeout=10_000, force=True)
            await asyncio.sleep(0.5)
            await self._wait_for_loading_masks()
        if not target_selected:
            target_label = await find_parameter_label(field_label)
            target_selected = await target_label.locator(
                'input[type="checkbox"]'
            ).first.is_checked()
        if not target_selected:
            raise TaobaoListingError(f"淘宝尺码参数“{field_label}”勾选失败")

        deadline = asyncio.get_running_loop().time() + 10
        table = None
        while asyncio.get_running_loop().time() < deadline:
            candidates = wrap.locator(".el-table")
            matches = []
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                headers = candidate.locator(
                    ":scope > .el-table__header-wrapper thead th"
                )
                header_texts = [
                    (await headers.nth(header_index).inner_text()).strip()
                    for header_index in range(await headers.count())
                ]
                if any(
                    normalize_label(text).startswith(normalize_label(field_label))
                    for text in header_texts
                ):
                    matches.append(candidate)
            if len(matches) == 1:
                table = matches[0]
                break
            await asyncio.sleep(0.05)
        if table is None:
            raise TaobaoListingError(f"淘宝尺码表找不到唯一“{field_label}”主表")

        headers = table.locator(":scope > .el-table__header-wrapper thead th")
        header_texts = [
            (await headers.nth(index).inner_text()).strip()
            for index in range(await headers.count())
        ]
        size_indexes = [
            index
            for index, text in enumerate(header_texts)
            if normalize_label(text) == normalize_label("尺码")
        ]
        value_indexes = [
            index
            for index, text in enumerate(header_texts)
            if normalize_label(text).startswith(normalize_label(field_label))
        ]
        rows = table.locator(":scope > .el-table__body-wrapper tbody > tr")
        # Element 固定首列时，主表的“尺码”表头可能为空，但行单元格仍有
        # S/M/L 等值。用 Excel 尺码集合精确反推该列，避免读取固定表副本。
        if not size_indexes and await rows.count():
            column_count = await rows.first.locator(":scope > td").count()
            inferred = []
            for column_index in range(column_count):
                values = []
                valid = True
                for row_index in range(await rows.count()):
                    cells = rows.nth(row_index).locator(":scope > td")
                    if await cells.count() <= column_index:
                        valid = False
                        break
                    value = normalize_size_name(
                        await cells.nth(column_index).inner_text()
                    )
                    if value is None:
                        valid = False
                        break
                    values.append(value)
                if valid and len(values) == len(set(values)) and set(values) == set(by_size):
                    inferred.append(column_index)
            size_indexes = inferred
        fixed_row_sizes: List[str] = []
        if not size_indexes and await rows.count():
            fixed_wrappers = table.locator(
                ":scope > .el-table__fixed .el-table__fixed-body-wrapper, "
                ":scope > .el-table__fixed-body-wrapper"
            )
            candidates: List[List[str]] = []
            for wrapper_index in range(await fixed_wrappers.count()):
                fixed_rows = fixed_wrappers.nth(wrapper_index).locator("tbody > tr")
                if await fixed_rows.count() != await rows.count():
                    continue
                max_columns = 0
                for row_index in range(await fixed_rows.count()):
                    max_columns = max(
                        max_columns,
                        await fixed_rows.nth(row_index).locator(":scope > td").count(),
                    )
                for column_index in range(max_columns):
                    values: List[str] = []
                    valid = True
                    for row_index in range(await fixed_rows.count()):
                        cells = fixed_rows.nth(row_index).locator(":scope > td")
                        if await cells.count() <= column_index:
                            valid = False
                            break
                        value = normalize_size_name(
                            await cells.nth(column_index).inner_text()
                        )
                        if value is None:
                            valid = False
                            break
                        values.append(value)
                    if (
                        valid
                        and len(values) == len(set(values))
                        and set(values) == set(by_size)
                    ):
                        candidates.append(values)
            if len(candidates) == 1:
                fixed_row_sizes = candidates[0]
        if (len(size_indexes) != 1 and not fixed_row_sizes) or len(value_indexes) != 1:
            raise TaobaoListingError(
                "淘宝尺码表列定位失败："
                f"尺码={size_indexes}，固定列尺码={fixed_row_sizes}，"
                f"{field_label}={value_indexes}"
            )

        page_rows: Dict[str, Any] = {}
        for index in range(await rows.count()):
            row = rows.nth(index)
            cells = row.locator(":scope > td")
            required_index = value_indexes[0]
            if size_indexes:
                required_index = max(required_index, size_indexes[0])
            if await cells.count() <= required_index:
                raise TaobaoListingError(f"淘宝尺码表第 {index + 1} 行缺少目标列")
            if fixed_row_sizes:
                size = fixed_row_sizes[index]
            else:
                size = normalize_size_name(
                    await cells.nth(size_indexes[0]).inner_text()
                )
            if size is None:
                raise TaobaoListingError(f"淘宝尺码表第 {index + 1} 行尺码无法识别")
            if size in page_rows:
                raise TaobaoListingError(f"淘宝尺码表页面包含重复尺码：{size}")
            page_rows[size] = row
        if set(page_rows) != set(by_size):
            missing = sorted(set(by_size) - set(page_rows))
            extra = sorted(set(page_rows) - set(by_size))
            raise TaobaoListingError(
                f"淘宝尺码表尺码不一致：缺少 {missing!r}，多出 {extra!r}"
            )

        actual: Dict[str, str] = {}
        for size, row in page_rows.items():
            cells = row.locator(":scope > td")
            inputs = cells.nth(value_indexes[0]).locator(
                'input:not([readonly]):not([disabled])'
            )
            if await inputs.count() != 1:
                raise TaobaoListingError(
                    f"淘宝尺码 {size} 的“{field_label}”输入框不唯一"
                )
            control = inputs.first
            expected = by_size[size]
            if (await control.input_value()).strip() != expected:
                await control.fill(expected)
                await control.press("Tab")
            value = (await control.input_value()).strip()
            if value != expected:
                raise TaobaoListingError(
                    f"淘宝尺码 {size} 的“{field_label}”回读失败：{value!r}"
                )
            actual[size] = value
        return {
            "field": field_label,
            "garment_kind": garment_kind,
            "rows": actual,
        }

    async def _form_item(self, label: str) -> Any:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        labels = self.panel.locator(".el-form-item > .el-form-item__label")
        matches = []
        for index in range(await labels.count()):
            candidate = labels.nth(index)
            if not await candidate.is_visible():
                continue
            if normalize_label(await candidate.inner_text()) == normalize_label(label):
                matches.append(candidate.locator("xpath=.."))
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝页面字段“{label}”不是唯一项：{len(matches)}"
            )
        return matches[0]

    async def _wrap_item(self, label: str) -> Any:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        labels = self.panel.locator(".wrap-item > .wrap-item_label")
        matches = []
        for index in range(await labels.count()):
            candidate = labels.nth(index)
            if not await candidate.is_visible():
                continue
            if normalize_label(await candidate.inner_text()) == normalize_label(label):
                matches.append(candidate.locator("xpath=.."))
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝页面区域“{label}”不是唯一项：{len(matches)}"
            )
        return matches[0]

    async def _store_row(self, form_item: Any, shop_name: str) -> Any:
        rows = form_item.locator(".ship-wrp > .item")
        matches = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            title = row.locator(".shopname span[title]").first
            if await title.count() and (await title.get_attribute("title") or "").strip() == shop_name:
                matches.append(row)
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝店铺中分类找不到唯一店铺“{shop_name}”"
            )
        return matches[0]

    async def _read_cascader_values(self, cascader: Any) -> Tuple[str, ...]:
        component_values = await cascader.evaluate(
            """element => {
              const components = [element.__vue__];
              if (element.__vue__ && element.__vue__.$refs) {
                components.push(element.__vue__.$refs.panel);
              }
              for (const component of components) {
                if (!component || typeof component.getCheckedNodes !== 'function') continue;
                let nodes = [];
                try { nodes = component.getCheckedNodes(true); } catch (_error) {
                  try { nodes = component.getCheckedNodes(); } catch (_ignored) {}
                }
                const values = (nodes || []).map(node => {
                  if (node && node.label != null) return String(node.label).trim();
                  if (node && node.data && node.data.label != null) {
                    return String(node.data.label).trim();
                  }
                  return '';
                }).filter(Boolean);
                if (values.length) return values;
              }
              return [];
            }"""
        )
        if component_values:
            return tuple(str(value).strip() for value in component_values if str(value).strip())
        tags = cascader.locator(".el-cascader__tags .el-tag")
        values = []
        for index in range(await tags.count()):
            text = re.sub(r"\s+", " ", (await tags.nth(index).inner_text()).strip())
            # Element Cascader 的 collapse-tags 会显示“+ 1”，它只是
            # 隐藏的已选数量，不是店铺分类。
            if text and not re.fullmatch(r"\+\s*\d+", text):
                values.append(text)
        if values:
            return tuple(values)
        input_box = cascader.locator("input.el-input__inner").first
        value = (await input_box.input_value()).strip()
        return (value,) if value else ()

    async def _visible_cascader_nodes(self, cascader: Any) -> List[Tuple[str, Any]]:
        local = cascader.locator(".el-cascader__dropdown:visible")
        dropdown = local.first if await local.count() else self.page.locator(
            ".el-cascader__dropdown:visible"
        ).first
        if not await dropdown.count():
            return []
        nodes = dropdown.locator(".el-cascader-node:visible")
        result = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            label = node.locator(":scope > .el-cascader-node__label")
            if not await label.count():
                label = node.locator(".el-cascader-node__label").first
            text = (await label.inner_text()).strip() if await label.count() else ""
            if text and "is-disabled" not in (await node.get_attribute("class") or ""):
                result.append((text, node))
        return result

    async def fill_store_categories(
        self,
        fields: Mapping[str, str],
        *,
        shop_name: str = "钊叔制",
    ) -> Tuple[str, ...]:
        """仅修改指定店铺的店铺分类；Excel 斜杠值表示多个分类。"""
        expected_text = _required_excel_value(
            fields, ("店铺中分类", "店铺分类"), "店铺中分类"
        )
        # 店铺中分类与其他 Excel 选择字段共用同一规则。
        groups = selection_value_groups("店铺中分类", expected_text)
        if not groups:
            raise TaobaoListingError("Excel 店铺中分类没有可用值")
        form_item = await self._form_item("店铺中分类")
        row = await self._store_row(form_item, shop_name)
        cascaders = row.locator(".el-cascader")
        if await cascaders.count() != 1:
            raise TaobaoListingError(f"店铺“{shop_name}”的店铺分类控件不唯一")
        cascader = cascaders.first
        await cascader.scroll_into_view_if_needed()

        for group in groups:
            actual = await self._read_cascader_values(cascader)
            existing = next(
                (
                    item
                    for item in actual
                    if any(
                        normalize_option(item) == normalize_option(candidate)
                        for candidate in group
                    )
                ),
                None,
            )
            if existing is not None:
                continue
            await cascader.locator("input.el-input__inner").first.evaluate(
                "element => element.click()"
            )
            deadline = asyncio.get_running_loop().time() + 8
            nodes: List[Tuple[str, Any]] = []
            while asyncio.get_running_loop().time() < deadline:
                nodes = await self._visible_cascader_nodes(cascader)
                if nodes:
                    break
                await asyncio.sleep(0.1)
            chosen_value = ""
            matches = []
            for candidate in group:
                candidate_matches = [
                    node
                    for text, node in nodes
                    if normalize_option(text) == normalize_option(candidate)
                ]
                if len(candidate_matches) == 1:
                    chosen_value = candidate
                    matches = candidate_matches
                    break
                if len(candidate_matches) > 1:
                    continue
            if len(matches) != 1:
                choices = "、".join(dict.fromkeys(text for text, _node in nodes)) or "无"
                raise TaobaoListingError(
                    f"店铺“{shop_name}”分类 OR 组“{'/'.join(group)}”"
                    "没有唯一可见候选；"
                    f"当前候选：{choices}"
                )
            node = matches[0]
            if self.logger is not None:
                self.logger.info(
                    "淘宝店铺中分类“%s”：OR 组 %s 选择 %s",
                    shop_name,
                    "/".join(group),
                    chosen_value,
                )
            checkbox = node.locator(":scope > .el-checkbox")
            click_target = checkbox.first if await checkbox.count() else node.locator(
                ":scope > .el-cascader-node__label"
            ).first
            # Element Cascader 的多选框可能是零尺寸包装节点，
            # Playwright 的坐标点击会长时间等待“可见”。直接触发
            # 页面已绑定的 click 事件，随后仍以 DOM 标签回读为准。
            await click_target.evaluate("element => element.click()")
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                actual = await self._read_cascader_values(cascader)
                if any(
                    normalize_option(item) == normalize_option(chosen_value)
                    for item in actual
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                raise TaobaoListingError(
                    f"店铺“{shop_name}”分类“{chosen_value}”选择后回读失败"
                )

        await self.page.keyboard.press("Escape")
        actual = await self._read_cascader_values(cascader)
        missing = []
        for group in groups:
            if not any(
                normalize_option(item) == normalize_option(candidate)
                for item in actual
                for candidate in group
            ):
                missing.append("/".join(group))
        if missing:
            raise TaobaoListingError(
                f"店铺“{shop_name}”分类缺少：{'、'.join(missing)}；页面为 {actual!r}"
            )
        return actual

    async def uncheck_spec_name(self, spec_name: str = "颜色") -> Optional[bool]:
        specifications = await self._wrap_item("商品规格")
        title_rows = specifications.locator(".block-specification .title-bg")
        matches = []
        for index in range(await title_rows.count()):
            row = title_rows.nth(index)
            inputs = row.locator("input:not([type=checkbox]):not([type=radio])")
            if not await inputs.count():
                continue
            if normalize_option(await inputs.first.input_value()) != normalize_option(
                spec_name
            ):
                continue
            checkbox = row.locator("input[type=checkbox]").first
            if not await checkbox.count():
                raise TaobaoListingError(f"淘宝规格名“{spec_name}”找不到勾选框")
            matches.append((row, checkbox))
        if len(matches) < 2:
            self.specification_state = {
                "found": bool(matches),
                "duplicate": False,
                "match_count": len(matches),
                "reason": (
                    f"子组件中规格名“{spec_name}”数量为 {len(matches)}，"
                    "未重复，已跳过"
                ),
            }
            if self.logger is not None:
                self.logger.info(
                    "淘宝商品规格子组件中“%s”未重复，"
                    "跳过取消勾选",
                    spec_name,
                )
            return None
        # 用户框选的是商品规格子组件中最后一个“颜色”。
        # 即使前面还有同名且已取消的项，也不会误操作它。
        row, original = matches[-1]
        if await original.is_checked():
            label = row.locator("label.el-checkbox").first
            if not await label.count():
                raise TaobaoListingError(f"淘宝规格名“{spec_name}”找不到可点击勾选框")
            await label.click()
        if await original.is_checked():
            raise TaobaoListingError(f"淘宝规格名“{spec_name}”取消勾选失败")
        self.specification_state = dict(
            await self._sync_specification_component(spec_name)
        )
        self.specification_state.update(
            {
                "duplicate": True,
                "match_count": len(matches),
                "target_occurrence": len(matches),
            }
        )
        if self.specification_state.get("error"):
            raise TaobaoListingError(
                "淘宝规格组件同步失败："
                + str(self.specification_state["error"])
            )
        return False

    async def _sync_specification_component(self, spec_name: str) -> Mapping[str, Any]:
        """把页面可见规格名/勾选状态同步到平台 Vue 组件。"""
        if self.panel is None:
            return {"found": False, "error": "淘宝资料面板未打开"}
        return await self.panel.evaluate(
            """async (root, targetName) => {
              const clean = value => String(value == null ? '' : value)
                .normalize('NFKC').replace(/[\\s*:：]+/g, '').toLowerCase();
              const seen = new Set();
              const candidates = [];
              for (const element of [root, ...root.querySelectorAll('*')]) {
                const component = element.__vue__;
                if (!component || seen.has(component)) continue;
                seen.add(component);
                if (!Array.isArray(component.specifications)) continue;
                if (!component.$el || !component.$el.getClientRects().length) continue;
                if (!component.$el.matches('.block-specification')
                    && !component.$el.querySelector('.block-specification')) continue;
                const titleRows = Array.from(component.$el.querySelectorAll('.title-bg'));
                const domRows = titleRows.map(row => {
                  const textInput = Array.from(row.querySelectorAll('input'))
                    .find(input => input.type !== 'checkbox' && input.type !== 'radio');
                  const checkbox = row.querySelector('input[type="checkbox"]');
                  return {
                    name: String((textInput && textInput.value) || '').trim(),
                    checked: Boolean(checkbox && checkbox.checked)
                  };
                }).filter(row => row.name);
                const stateNames = component.specifications.map(specification =>
                  String((specification && specification.name) || '').trim());
                if (![...domRows.map(row => row.name), ...stateNames]
                    .some(name => clean(name) === clean(targetName))) continue;
                candidates.push({
                  component,
                  domRows,
                  direct: component.$el.matches('.block-specification')
                });
              }
              const directCandidates = candidates.filter(candidate => candidate.direct);
              const usableCandidates = directCandidates.length ? directCandidates : candidates;
              if (usableCandidates.length !== 1) {
                return {
                  found: false,
                  error: `含规格“${targetName}”的最内层可见组件数为 `
                    + `${usableCandidates.length}`
                };
              }
              const {component, domRows} = usableCandidates[0];
              if (domRows.length !== component.specifications.length) {
                return {
                  found: true,
                  error: `页面规格行 ${domRows.length} 与组件规格行 `
                    + `${component.specifications.length} 不一致`,
                  dom: domRows,
                  component: component.specifications.map(specification => ({
                    name: String((specification && specification.name) || ''),
                    checked: Boolean(specification && specification.checked)
                  }))
                };
              }
              const before = component.specifications.map(specification => ({
                name: String((specification && specification.name) || ''),
                checked: Boolean(specification && specification.checked)
              }));
              component.specifications.forEach((specification, index) => {
                const row = domRows[index];
                if (typeof component.$set === 'function') {
                  component.$set(specification, 'name', row.name);
                  component.$set(specification, 'checked', row.checked);
                } else {
                  specification.name = row.name;
                  specification.checked = row.checked;
                }
              });
              const matchingTargets = component.specifications.filter(specification =>
                clean(specification && specification.name) === clean(targetName));
              const target = matchingTargets[matchingTargets.length - 1];
              if (!target || target.checked) {
                return {found: true, error: `规格“${targetName}”组件状态仍为勾选`};
              }
              if (typeof component.onChangeSpCheckbox === 'function') {
                component.onChangeSpCheckbox(target);
              } else if (typeof component.$emit === 'function') {
                component.$emit('change', component.specifications);
              }
              if (typeof component.$nextTick === 'function') {
                await new Promise(resolve => component.$nextTick(resolve));
              }
              return {
                found: true,
                platform: String(component.platform || ''),
                before,
                dom: domRows,
                after: component.specifications.map(specification => ({
                  name: String((specification && specification.name) || ''),
                  checked: Boolean(specification && specification.checked)
                }))
              };
            }""",
            spec_name,
        )

    async def _specification_payload_snapshot(self) -> Mapping[str, Any]:
        """读取淘宝父级销售属性组件最终交给保存流程的规格数据。"""
        if self.panel is None:
            return {"found": False, "error": "淘宝资料面板未打开"}
        result = await self.panel.evaluate(
            """async root => {
              const seen = new Set();
              const candidates = [];
              for (const element of [root, ...root.querySelectorAll('*')]) {
                const component = element.__vue__;
                if (!component || seen.has(component)) continue;
                seen.add(component);
                if (typeof component.method_getData !== 'function') continue;
                if (!component.$el || !component.$el.getClientRects().length) continue;
                if (!component.$el.querySelector('.block-specification')) continue;
                candidates.push(component);
              }
              if (candidates.length !== 1) {
                return {
                  found: false,
                  error: `淘宝销售属性父组件数为 ${candidates.length}`
                };
              }
              let payload;
              try {
                payload = await Promise.resolve(candidates[0].method_getData());
              } catch (error) {
                return {
                  found: true,
                  error: error && error.message ? error.message : String(error)
                };
              }
              if (!Array.isArray(payload) || !Array.isArray(payload[0])) {
                return {found: true, error: '淘宝销售属性父组件未返回规格数组'};
              }
              const specifications = payload[0].map(specification => ({
                name: String((specification && specification.name) || ''),
                checked: specification && Object.prototype.hasOwnProperty.call(
                  specification, 'checked'
                ) ? Boolean(specification.checked) : true
              }));
              const normalizedNames = specifications.map(specification =>
                specification.name.normalize('NFKC').replace(/[\\s*:：]+/g, '')
                  .toLowerCase()
              );
              const duplicateNames = normalizedNames.filter(
                (name, index) => name && normalizedNames.indexOf(name) !== index
              );
              return {
                found: true,
                specifications,
                sku_count: Array.isArray(payload[1]) ? payload[1].length : 0,
                duplicate_names: [...new Set(duplicateNames)]
              };
            }"""
        )
        if result.get("error"):
            raise TaobaoListingError(
                "淘宝保存规格数据读取失败：" + str(result["error"])
            )
        if result.get("duplicate_names"):
            raise TaobaoListingError(
                "淘宝保存规格数据仍有重名："
                + "、".join(str(name) for name in result["duplicate_names"])
            )
        return result

    async def _sku_batch_row(self, product_details: Any) -> Any:
        rows = product_details.locator(".sku-batch-row")
        matches = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            if await row.get_by_role("button", name="批量设置", exact=True).count():
                matches.append(row)
        if len(matches) != 1:
            raise TaobaoListingError(f"淘宝 SKU 批量设置行不唯一：{len(matches)}")
        return matches[0]

    async def _sku_batch_item(self, row: Any, label: str) -> Any:
        items = row.locator(":scope > .sku-batch-item")
        matches = []
        for index in range(await items.count()):
            item = items.nth(index)
            item_label = item.locator(":scope > .sku-batch-item_label")
            if await item_label.count() and normalize_label(
                await item_label.inner_text()
            ) == normalize_label(label):
                matches.append(item)
        if len(matches) != 1:
            raise TaobaoListingError(f"淘宝 SKU 批量字段“{label}”不唯一：{len(matches)}")
        return matches[0]

    async def _fill_batch_text(self, row: Any, label: str, expected: str) -> str:
        item = await self._sku_batch_item(row, label)
        inputs = item.locator("input.el-input__inner:not([readonly])")
        if await inputs.count() != 1:
            raise TaobaoListingError(f"淘宝 SKU 批量字段“{label}”输入框不唯一")
        input_box = inputs.first
        if (await input_box.input_value()).strip() != expected:
            await input_box.fill(expected)
            await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if actual != expected:
            raise TaobaoListingError(
                f"淘宝 SKU 批量字段“{label}”回读失败：{actual!r}"
            )
        return actual

    async def _fill_batch_select(
        self,
        row: Any,
        label: str,
        candidates: Sequence[str],
    ) -> str:
        item = await self._sku_batch_item(row, label)
        selects = item.locator(".el-select")
        if await selects.count() != 1:
            raise TaobaoListingError(f"淘宝 SKU 批量字段“{label}”下拉框不唯一")
        actual = await self._select_values(
            selects.first,
            (tuple(candidates),),
            label=f"SKU批量{label}",
            multi=False,
        )
        if actual is None:
            # 保留失败字段的候选列表，便于自动错误截图直接呈现现场。
            try:
                await self._open_select(selects.first, multi=False)
                await self._visible_dom_options(selects.first, timeout_seconds=2)
            except Exception:
                pass
            raise TaobaoListingError(
                f"淘宝 SKU 批量字段“{label}”没有可用候选："
                f"{'/'.join(candidates)}"
            )
        return actual[0]

    async def _sku_table_snapshot(self, product_details: Any) -> Mapping[str, Any]:
        return await product_details.evaluate(
            """root => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const table = root.querySelector('.el-table__main-wrapper') || root;
              const headers = Array.from(table.querySelectorAll(
                '.el-table__header-wrapper thead th'
              )).map(header => clean(header.innerText));
              const rows = Array.from(table.querySelectorAll(
                '.el-table__body-wrapper tbody tr'
              )).map(row => Array.from(row.querySelectorAll(':scope > td')).map(cell => {
                const inputs = Array.from(cell.querySelectorAll('input'))
                  .filter(input => input.type !== 'checkbox' && input.type !== 'radio')
                  .map(input => clean(input.value));
                if (inputs.length) return inputs.join('|');
                return clean(cell.innerText);
              }));
              return {headers, rows};
            }"""
        )

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        matches = [
            index
            for index, header in enumerate(headers)
            if normalize_label(header) == wanted or normalize_label(header).startswith(wanted)
        ]
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝 SKU 表格列“{label}”不是唯一项：{matches}"
            )
        return matches[0]

    @staticmethod
    def _numeric_equal(actual: str, expected: str) -> bool:
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False

    def _validate_sku_snapshot(
        self,
        snapshot: Mapping[str, Any],
        expected: Mapping[str, str],
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = snapshot.get("rows", ())
        if not raw_rows:
            raise TaobaoListingError("淘宝 SKU 表格没有可校验的明细行")
        indices = {
            label: self._sku_column_index(headers, label) for label in expected
        }
        result = []
        errors = []
        for row_number, raw_row in enumerate(raw_rows, start=1):
            row = tuple(str(value) for value in raw_row)
            values = {label: row[index] for label, index in indices.items()}
            if not self._numeric_equal(values["价格"], expected["价格"]):
                errors.append(f"第{row_number}行价格={values['价格']!r}")
            if not self._numeric_equal(values["数量"], expected["数量"]):
                errors.append(f"第{row_number}行数量={values['数量']!r}")
            for label in expected:
                if label in {"价格", "数量"}:
                    continue
                if normalize_option(values[label]) != normalize_option(expected[label]):
                    errors.append(f"第{row_number}行{label}={values[label]!r}")
            result.append(values)
        if errors:
            raise TaobaoListingError(
                "淘宝 SKU 批量设置后校验失败：" + "；".join(errors[:12])
            )
        return tuple(result)

    async def fill_sku_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        price = _required_excel_value(
            fields,
            ("价格", "京东价", "市场价", "售卖价", "售价", "吊牌价", "基本售价"),
            "SKU 价格",
        )
        quantity = _required_excel_value(fields, ("数量",), "SKU 数量")
        if not re.fullmatch(r"\d+(?:\.\d+)?", price):
            raise TaobaoListingError(f"Excel SKU 价格不是有效数字：{price!r}")
        if not quantity.isdigit():
            raise TaobaoListingError(f"Excel SKU 数量不是整数：{quantity!r}")
        fleece_source = _required_excel_value(fields, ("是否加绒",), "SKU 是否加绒")
        # 严格使用 Excel 原值；不把“否”改写成“不加绒”，也不补充程序候选。
        # Excel 自身用斜杠给出的 OR 值仍按原顺序逐个匹配。
        fleece_candidates = list(
            single_selection_candidates("是否加绒", fleece_source)
        )
        if not fleece_candidates:
            raise TaobaoListingError(f"Excel SKU 是否加绒值无法识别：{fleece_source!r}")
        sku_category = _required_excel_value(fields, ("SKU分类",), "SKU 分类")
        body_type = _required_excel_value(fields, ("适用体型",), "SKU 适用体型")

        product_details = await self._wrap_item("商品明细")
        row = await self._sku_batch_row(product_details)
        before = await self._sku_table_snapshot(product_details)
        platform_index = self._sku_column_index(before.get("headers", ()), "平台规格编码")
        platform_codes_before = tuple(
            str(raw_row[platform_index]) for raw_row in before.get("rows", ())
        )

        await self._fill_batch_text(row, "价格", price)
        await self._fill_batch_text(row, "数量", quantity)
        actual_fleece = await self._fill_batch_select(
            row, "是否加绒", tuple(fleece_candidates)
        )
        actual_sku_category = await self._fill_batch_select(
            row, "SKU分类", single_selection_candidates("SKU分类", sku_category)
        )
        actual_body_type = await self._fill_batch_select(
            row, "适用体型", single_selection_candidates("适用体型", body_type)
        )
        dynamic_values: Dict[str, str] = {}
        batch_items = row.locator(":scope > .sku-batch-item")
        fixed_labels = {"价格", "数量", "平台规格编码", "是否加绒", "SKU分类", "适用体型"}
        for item_index in range(await batch_items.count()):
            item = batch_items.nth(item_index)
            label_node = item.locator(":scope > .sku-batch-item_label")
            if not await label_node.count():
                continue
            label = (await label_node.inner_text()).strip().rstrip("：:").strip()
            if not label or label in fixed_labels:
                continue
            source = _single_source(fields, (label,), f"SKU {label}")
            if source is None:
                continue
            _source_key, source_value = source
            if await item.locator(".el-select").count() != 1:
                raise TaobaoListingError(
                    f"淘宝 SKU 动态字段“{label}”不是唯一的下拉框"
                )
            dynamic_values[label] = await self._fill_batch_select(
                row,
                label,
                single_selection_candidates(label, source_value),
            )
        button = row.get_by_role("button", name="批量设置", exact=True)
        await button.click()

        expected = {
            "价格": price,
            "数量": quantity,
            "是否加绒": actual_fleece,
            "SKU分类": actual_sku_category,
            "适用体型": actual_body_type,
            **dynamic_values,
        }
        deadline = asyncio.get_running_loop().time() + 8
        last_snapshot: Mapping[str, Any] = {}
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            last_snapshot = await self._sku_table_snapshot(product_details)
            try:
                rows = self._validate_sku_snapshot(last_snapshot, expected)
                break
            except TaobaoListingError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        else:
            raise TaobaoListingError(str(last_error or "淘宝 SKU 批量设置超时"))

        platform_index_after = self._sku_column_index(
            last_snapshot.get("headers", ()), "平台规格编码"
        )
        platform_codes_after = tuple(
            str(raw_row[platform_index_after])
            for raw_row in last_snapshot.get("rows", ())
        )
        if platform_codes_after != platform_codes_before:
            raise TaobaoListingError(
                "淘宝 SKU 批量设置意外改动了平台规格编码"
            )
        return {
            "batch_clicked": True,
            "row_count": len(rows),
            "values": expected,
            "rows": rows,
            "platform_codes_preserved": True,
            "platform_codes": platform_codes_after,
        }

    async def _fill_named_input(self, name: str, label: str, expected: str) -> str:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        form_item = await self._form_item(label)
        inputs = form_item.locator(f'input[name="{name}"]')
        if await inputs.count() != 1:
            raise TaobaoListingError(f"淘宝字段“{label}”输入框不唯一")
        input_box = inputs.first
        if (await input_box.input_value()).strip() != expected:
            await input_box.fill(expected)
            await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if label == "一口价":
            valid = self._numeric_equal(actual, expected)
        else:
            valid = actual == expected
        if not valid:
            raise TaobaoListingError(
                f"淘宝字段“{label}”填写后校验失败：{actual!r}"
            )
        return actual

    async def fill_sales_fields(self, fields: Mapping[str, str]) -> Mapping[str, str]:
        price = _required_excel_value(fields, ("一口价",), "一口价")
        outer_id = _required_excel_value(
            fields, ("商家编码", "货号", "商家外部编码"), "商家编码"
        )
        if not re.fullmatch(r"\d+(?:\.\d+)?", price):
            raise TaobaoListingError(f"Excel 一口价不是有效数字：{price!r}")
        return {
            "一口价": await self._fill_named_input("price", "一口价", price),
            "商家编码": await self._fill_named_input(
                "outerId", "商家编码", outer_id
            ),
        }

    async def _ensure_radio(self, form_label: str, option_text: str) -> str:
        form_item = await self._form_item(form_label)
        options = form_item.locator("label.el-radio")
        matches = []
        for index in range(await options.count()):
            option = options.nth(index)
            rendered = await option.evaluate(
                """element => {
                  const style = getComputedStyle(element);
                  return Boolean(element.getClientRects().length)
                    && style.display !== 'none'
                    && style.visibility !== 'hidden';
                }"""
            )
            if not rendered:
                continue
            label = option.locator(".el-radio__label").first
            if await label.count() and normalize_label(
                await label.inner_text()
            ) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝“{form_label}”选项“{option_text}”不唯一"
            )
        option = matches[0]
        radio = option.locator("input[type=radio]").first
        if not await radio.is_checked():
            await option.click()
        if not await radio.is_checked():
            raise TaobaoListingError(
                f"淘宝“{form_label}”选项“{option_text}”勾选失败"
            )
        return option_text

    async def _ensure_checkbox(self, form_label: str, option_text: str) -> bool:
        form_item = await self._form_item(form_label)
        options = form_item.locator("label.el-checkbox")
        matches = []
        for index in range(await options.count()):
            option = options.nth(index)
            rendered = await option.evaluate(
                """element => {
                  const style = getComputedStyle(element);
                  return Boolean(element.getClientRects().length)
                    && style.display !== 'none'
                    && style.visibility !== 'hidden';
                }"""
            )
            if not rendered:
                continue
            label = option.locator(".el-checkbox__label").first
            if await label.count() and normalize_label(
                await label.inner_text()
            ) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise TaobaoListingError(
                f"淘宝“{form_label}”勾选项“{option_text}”不唯一"
            )
        checkbox = matches[0].locator("input[type=checkbox]").first
        if not await checkbox.is_checked():
            await matches[0].click()
        if not await checkbox.is_checked():
            raise TaobaoListingError(
                f"淘宝“{form_label}”勾选项“{option_text}”勾选失败"
            )
        return True

    async def _ensure_checkbox_contains(
        self, form_label: str, text_fragment: str
    ) -> bool:
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        checkboxes = self.panel.get_by_role(
            "checkbox", name=re.compile(re.escape(text_fragment))
        )
        count = await checkboxes.count()
        if count == 0:
            raise TaobaoListingError(
                f"淘宝“{form_label}”未找到包含“{text_fragment}”的勾选项"
            )
        if count > 1 and self.logger is not None:
            self.logger.info(
                "淘宝“%s”包含“%s”的同义勾选节点有 %s 个，"
                "按页面顺序使用第一个可操作节点",
                form_label,
                text_fragment,
                count,
            )
        for index in range(count):
            checkbox = checkboxes.nth(index)
            if await checkbox.is_disabled():
                continue
            if await checkbox.is_checked():
                return True
            applied = await checkbox.evaluate(
                """input => {
                  if (input.disabled) return false;
                  input.click();
                  return Boolean(input.checked);
                }"""
            )
            if applied and await checkbox.is_checked():
                return True
        raise TaobaoListingError(
            f"淘宝“{form_label}”包含“{text_fragment}”的勾选项勾选失败"
        )

    async def apply_payment_and_service(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        stock_source = _required_excel_value(
            fields, ("拍下减库存", "库存扣减方式"), "库存扣减方式"
        )
        stock_options = []
        for candidate in single_selection_candidates("库存扣减方式", stock_source):
            normalized = normalize_option(candidate)
            if normalized in {
                normalize_option(value) for value in ("否", "付款减库存", "0")
            }:
                stock_options.append("付款减库存")
            elif normalized in {
                normalize_option(value) for value in ("是", "拍下减库存", "1")
            }:
                stock_options.append("拍下减库存")
        stock_options = list(dict.fromkeys(stock_options))
        if not stock_options:
            raise TaobaoListingError(f"Excel 库存扣减方式无法识别：{stock_source!r}")
        # 用户要求按页面顺序操作：库存扣减 -> 保修服务 -> 七天退货承诺。
        stock = ""
        last_error: Optional[Exception] = None
        for stock_option in stock_options:
            try:
                stock = await self._ensure_radio("库存扣减方式", stock_option)
                break
            except TaobaoListingError as exc:
                last_error = exc
        if not stock:
            raise TaobaoListingError(str(last_error or "库存扣减方式无可用候选"))
        warranty = await self._ensure_checkbox("售后服务", "保修服务")
        seven_day_return = await self._ensure_checkbox_contains(
            "售后服务", "七天退货"
        )
        return {
            "库存扣减方式": stock,
            "保修服务": warranty,
            "七天退货承诺": seven_day_return,
        }

    async def fill_freight_template(
        self,
        fields: Mapping[str, str],
        *,
        shop_name: str = "钊叔制",
    ) -> str:
        freight = _required_excel_value(
            fields, ("运费设置", "运费模板"), "运费模板"
        )
        if self.panel is None:
            raise TaobaoListingError("请先调用 open() 打开淘宝资料")
        rows = self.panel.locator(".set-ship")
        matches = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            title = row.locator(":scope > .shop-title")
            if await title.count() and (await title.inner_text()).strip() == shop_name:
                matches.append(row)
        if len(matches) != 1:
            raise TaobaoListingError(f"淘宝运费设置找不到唯一店铺“{shop_name}”")
        selects = matches[0].locator(":scope > .el-select")
        if await selects.count() != 1:
            raise TaobaoListingError(f"店铺“{shop_name}”的淘宝运费模板下拉框不唯一")
        actual = await self._select_values(
            selects.first,
            (value_candidates("运费模板", freight),),
            label=f"{shop_name}运费模板",
            multi=False,
        )
        if actual is None:
            raise TaobaoListingError(
                f"店铺“{shop_name}”没有 Excel 运费模板的精确候选：{freight}"
            )
        return actual[0]

    async def _visible_validation_errors(self) -> Tuple[str, ...]:
        if self.panel is None:
            return ()
        errors = self.panel.locator(".el-form-item__error:visible")
        result = []
        for index in range(await errors.count()):
            error = errors.nth(index)
            form_item = error.locator("xpath=ancestor::*[contains(@class, 'el-form-item')][1]")
            label = form_item.locator(":scope > .el-form-item__label")
            label_text = (await label.inner_text()).strip() if await label.count() else "未命名字段"
            error_text = (await error.inner_text()).strip()
            result.append(f"{label_text}：{error_text}")
        return tuple(dict.fromkeys(result))

    async def _revalidate_form_property(self, property_name: str) -> Mapping[str, Any]:
        """用 Element Form 自身规则重新校验，不直接隐藏错误。"""
        if self.panel is None:
            return {"found": False, "message": ""}
        return await self.drawer.evaluate(
            """async (root, propertyName) => {
              const seen = new Set();
              const components = [];
              for (const element of [root, ...root.querySelectorAll('*')]) {
                const component = element.__vue__;
                if (!component || seen.has(component)) continue;
                seen.add(component);
                if (typeof component.validateField !== 'function') continue;
                const fields = Array.isArray(component.fields) ? component.fields : [];
                if (!fields.some(field => field && field.prop === propertyName)) continue;
                components.push(component);
              }
              const messages = [];
              for (const component of components) {
                const message = await new Promise(resolve => {
                  let settled = false;
                  const finish = value => {
                    if (settled) return;
                    settled = true;
                    resolve(String(value || '').trim());
                  };
                  try {
                    component.validateField(propertyName, finish);
                    setTimeout(() => finish(''), 3000);
                  } catch (error) {
                    finish(error && error.message ? error.message : String(error));
                  }
                });
                if (!message && typeof component.clearValidate === 'function') {
                  component.clearValidate(propertyName);
                }
                if (typeof component.$nextTick === 'function') {
                  await new Promise(resolve => {
                    let settled = false;
                    const finish = () => {
                      if (settled) return;
                      settled = true;
                      resolve();
                    };
                    const timer = setTimeout(finish, 500);
                    try {
                      component.$nextTick(() => {
                        clearTimeout(timer);
                        finish();
                      });
                    } catch (_error) {
                      clearTimeout(timer);
                      finish();
                    }
                  });
                }
                if (message) messages.push(message);
              }
              const fields = [];
              for (const component of components) {
                for (const field of (Array.isArray(component.fields) ? component.fields : [])) {
                  if (!field || field.prop !== propertyName) continue;
                  let fieldValue = '<unreadable>';
                  try {
                    fieldValue = JSON.parse(JSON.stringify(field.fieldValue));
                  } catch (_error) {
                    fieldValue = String(field.fieldValue);
                  }
                  fields.push({
                    prop: String(field.prop || ''),
                    validateState: String(field.validateState || ''),
                    validateMessage: String(field.validateMessage || ''),
                    fieldValue
                  });
                }
              }
              return {
                found: components.length > 0,
                found_count: components.length,
                message: messages.join('；'),
                fields
              };
            }""",
            property_name,
        )

    async def _revalidate_specifications(self) -> Mapping[str, Any]:
        """调用淘宝资料组件的规格 + SKU 联合校验。"""
        if self.panel is None:
            return {"found": False, "valid": False, "result": ""}
        return await self.drawer.evaluate(
            """async root => {
              const seen = new Set();
              const candidates = [];
              for (const element of [root, ...root.querySelectorAll('*')]) {
                const component = element.__vue__;
                if (!component || seen.has(component)) continue;
                seen.add(component);
                if (typeof component.valid_specifications !== 'function') continue;
                if (component.$el && !component.$el.getClientRects().length) continue;
                if (!component.$el
                    || !component.$el.querySelector('.block-specification')) continue;
                candidates.push(component);
              }
              if (!candidates.length) {
                return {found: false, valid: false, result: ''};
              }
              const reports = [];
              for (const component of candidates) {
                let valid = false;
                let thrown = '';
                try {
                  valid = Boolean(await component.valid_specifications());
                } catch (error) {
                  thrown = error && error.message ? error.message : String(error);
                }
                if (typeof component.$nextTick === 'function') {
                  await new Promise(resolve => component.$nextTick(resolve));
                }
                reports.push({
                  valid,
                  result: thrown || (component.specificationsValidResult === true
                    ? '' : String(component.specificationsValidResult || '')),
                  platform: String(component.platform || ''),
                  specifications: (Array.isArray(component.specifications)
                    ? component.specifications : []).map(specification => ({
                    name: String((specification && specification.name) || ''),
                    checked: Boolean(specification && specification.checked),
                    nameKey: String((specification && specification.nameKey) || '')
                  }))
                });
              }
              return {
                found: true,
                found_count: reports.length,
                valid: reports.every(report => report.valid),
                result: reports.map(report => report.result).filter(Boolean).join('；'),
                specifications: reports[0].specifications
              };
            }"""
        )

    def _last_duplicate_spec_is_unchecked(self, spec_name: str) -> bool:
        """仅信任刚从可见 DOM 同步回组件的最后一个重复规格状态。"""
        state = self.specification_state
        if not state.get("duplicate"):
            return False
        rows = state.get("after")
        if not isinstance(rows, list):
            return False
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and normalize_option(row.get("name")) == normalize_option(spec_name)
        ]
        return len(matches) >= 2 and matches[-1].get("checked") is False

    @staticmethod
    def _ignore_exact_validation_warning(
        validation: Mapping[str, Any],
        *,
        key: str,
        warning: str,
        enabled: bool,
    ) -> Mapping[str, Any]:
        """只移除完全匹配的已知提示，保留同一字段中的其他错误。"""
        result = dict(validation)
        if not enabled:
            return result
        raw_message = str(result.get(key) or "").strip()
        parts = [part.strip() for part in raw_message.replace(";", "；").split("；")]
        remaining = [part for part in parts if part and part != warning]
        if len(remaining) == len([part for part in parts if part]):
            return result
        result[f"raw_{key}"] = raw_message
        result[f"ignored_{key}"] = warning
        result[key] = "；".join(remaining)
        if key == "result":
            result["raw_valid"] = bool(validation.get("valid"))
            result["valid"] = not remaining
        return result

    async def apply_extended_fields(
        self,
        fields: Any,
        *,
        size_lengths: Sequence[Any] = (),
        garment_kind: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """按用户指定顺序填写淘宝后半段基础资料。"""
        source_fields = fields.fields
        if self.logger is not None:
            self.logger.info("正在填写淘宝店铺中分类（钊叔制）")
        store_categories = await self.fill_store_categories(source_fields)
        if self.logger is not None:
            self.logger.info("正在取消淘宝规格名“颜色”")
        color_spec_checked = await self.uncheck_spec_name("颜色")
        if self.logger is not None:
            self.logger.info("正在填写淘宝 SKU 批量字段并点击“批量设置”")
        sku = await self.fill_sku_batch(source_fields)
        size_chart: Mapping[str, Any] = {"filled": False}
        if size_lengths:
            if garment_kind is None:
                raise TaobaoListingError("填写淘宝尺码表时缺少衣服/裤子类型")
            if self.logger is not None:
                self.logger.info(
                    "正在按尺码填写淘宝尺码表：%s",
                    "裤长" if garment_kind == "pants" else "衣长",
                )
            size_chart = {
                "filled": True,
                **await self.fill_size_chart_lengths(size_lengths, garment_kind),
            }
        if self.logger is not None:
            self.logger.info("正在填写淘宝一口价和商家编码")
        sales = await self.fill_sales_fields(source_fields)
        if self.logger is not None:
            self.logger.info("正在按顺序勾选淘宝库存扣减和保修服务")
        payment_service = await self.apply_payment_and_service(source_fields)
        listing_source = _required_excel_value(
            source_fields, ("商品状态", "上架时间"), "上架时间"
        )
        listing_aliases = {
            normalize_option("立即上架"): "立刻上架",
            normalize_option("立刻上架"): "立刻上架",
            normalize_option("放入仓库"): "放入仓库",
        }
        listing_options = tuple(
            dict.fromkeys(
                listing_aliases[normalize_option(candidate)]
                for candidate in single_selection_candidates("上架时间", listing_source)
                if normalize_option(candidate) in listing_aliases
            )
        )
        listing_option = listing_options[0] if listing_options else None
        if listing_option is None:
            raise TaobaoListingError(f"Excel 上架时间无法识别：{listing_source!r}")
        listing = await self._ensure_radio("上架时间", listing_option)
        freight = await self.fill_freight_template(source_fields)
        specification_validation = await self._revalidate_specifications()
        duplicate_color_unchecked = (
            color_spec_checked is False
            and self._last_duplicate_spec_is_unchecked("颜色")
        )
        duplicate_removal: Mapping[str, Any] = {
            "removed": False,
            "reason": (
                "保留重复颜色规格项，仅取消最后一个重复颜色的勾选"
                if duplicate_color_unchecked
                else "未满足重复颜色取消勾选条件"
            ),
        }
        duplicate_warning = "规格名【颜色】重复"
        specification_validation = self._ignore_exact_validation_warning(
            specification_validation,
            key="result",
            warning=duplicate_warning,
            enabled=duplicate_color_unchecked,
        )
        if duplicate_color_unchecked and specification_validation.get(
            "ignored_result"
        ):
            if self.logger is not None:
                self.logger.info(
                    "最后一个重复“颜色”已取消勾选；保留规格项并忽略该重复提示"
                )
        specification_payload = await self._specification_payload_snapshot()
        sale_prop_validation = await self._revalidate_form_property("saleProp")
        sale_prop_validation = self._ignore_exact_validation_warning(
            sale_prop_validation,
            key="message",
            warning=duplicate_warning,
            enabled=duplicate_color_unchecked,
        )
        raw_validation_errors = await self._visible_validation_errors()
        ignored_visible_validation_errors = tuple(
            error
            for error in raw_validation_errors
            if duplicate_color_unchecked
            and str(error).strip().split("：", 1)[-1].strip() == duplicate_warning
        )
        validation_errors = tuple(
            error
            for error in raw_validation_errors
            if error not in ignored_visible_validation_errors
        )
        return {
            "store_category": {"shop": "钊叔制", "values": store_categories},
            "specification": {
                "name": "颜色",
                "found": bool(self.specification_state.get("found")),
                "duplicate": bool(self.specification_state.get("duplicate")),
                "action_applied": color_spec_checked is not None,
                "checked": color_spec_checked,
                "component_state": self.specification_state,
                "duplicate_removal": duplicate_removal,
            },
            "sku_batch": sku,
            "size_chart": size_chart,
            "sales": sales,
            "payment_service": payment_service,
            "listing": {"上架时间": listing},
            "freight": {"shop": "钊叔制", "template": freight},
            "specification_validation": specification_validation,
            "specification_payload": specification_payload,
            "sale_prop_validation": sale_prop_validation,
            "visible_validation_errors": validation_errors,
            "ignored_visible_validation_errors": ignored_visible_validation_errors,
        }
