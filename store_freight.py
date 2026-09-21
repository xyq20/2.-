"""Store-scoped freight selection for supplier product forms.

Freight template IDs belong to a shop. Use native search/custom entry and
verify the committed value, never mutate Vue options or borrow another shop.
"""
import asyncio
import re
from typing import Any, Mapping
from dataclasses import replace

from playwright.async_api import Error as PlaywrightError

from attribute_runtime import AttributeRequest
from field_policies import FREIGHT_TEMPLATE_SHOPS, freight_alternatives
from learning_models import CandidateValue, canonical_sha256
from taobao_listing import TaobaoListingError, normalize_option


async def sync_store_freight(adapter: Any, fields: Mapping[str, str], *, read_only=False, expected=None):
    alternatives = freight_alternatives(fields)
    if not alternatives:
        return {"status": "no_excel_source", "stores": {}}
    targets = FREIGHT_TEMPLATE_SHOPS.get(getattr(adapter, "freight_platform_id", adapter.attribute_platform_id))
    if not targets:
        raise TaobaoListingError("未配置该平台的运费更新店铺，未操作")
    platform = getattr(adapter, "freight_platform_id", adapter.attribute_platform_id)
    rows = adapter.panel.locator(".row-item:visible" if platform == "xhs" else ".set-ship:visible")
    stores = []
    seen = set()
    for index in range(await rows.count()):
        row = rows.nth(index)
        section = await row.evaluate("""row => {
            for (let p = row.parentElement; p; p = p.parentElement) {
                if (!p.classList.contains('el-form-item')) continue;
                const label = p.querySelector(':scope > .el-form-item__label');
                const text = (label?.innerText || '').replace(/[\\s*：:]/g, '');
                if (text) return text;
            }
            return '';
        }""")
        if section not in ("运费模板", "运费设置", "运费模版"):
            continue
        names = row.locator(":scope > .shop-title")
        if await names.count() != 1:
            raise TaobaoListingError("运费店铺行结构不唯一，未操作")
        name = (await names.inner_text()).strip()
        if name not in targets:
            continue
        selects = row.locator(":scope > .el-select")
        if await selects.count() != 1:
            raise TaobaoListingError("运费店铺行结构不唯一，未操作")
        if not name or name in seen:
            raise TaobaoListingError(f"运费店铺名称为空或重复：{name}")
        seen.add(name)
        stores.append((name, selects.first))
    missing = set(targets) - seen
    if missing:
        if adapter.logger:
            diagnostics = await rows.evaluate_all("""rows => rows.map(row => ({
                shop: (row.querySelector('.shop-title')?.innerText || '').trim(),
                label: (row.closest('.el-form-item')?.querySelector('.el-form-item__label')?.innerText || '').trim(),
                selects: row.querySelectorAll('.el-select').length
            }))""")
            adapter.logger.warning("运费定位证据：%s", diagnostics)
        raise TaobaoListingError("未找到指定运费店铺：" + "、".join(sorted(missing)))
    expected_stores = expected.get("stores", {}) if expected is not None else None
    if read_only and expected_stores is not None and set(expected_stores) != seen:
        raise TaobaoListingError("保存前后运费店铺集合不一致")

    results = {}
    failures = []
    for shop, select in stores:
        try:
            verified = expected_stores.get(shop) if expected_stores is not None else None
            results[shop] = await _sync_store(adapter, shop, select, alternatives, read_only, verified)
        except TaobaoListingError as exc:
            # Check remaining shops rather than stopping at the first mismatch.
            failures.append(f"{shop}：{exc}")
    if failures:
        raise TaobaoListingError("店铺运费处理失败：" + "；".join(failures))
    return {"status": "review_required" if any(v["status"] == "review_required" for v in results.values()) else "verified", "stores": results}


async def _sync_store(adapter, shop, select, alternatives, read_only, verified=None):
    current_shop = await select.evaluate("""select =>
        (select.parentElement.querySelector(':scope > .shop-title')?.innerText || '').trim()
    """)
    if current_shop != shop:
        raise TaobaoListingError("店铺行已重新排序，停止使用旧定位，避免串店铺")
    await adapter._open_select(select, multi=False)
    try:
        try:
            dropdown, options = await adapter._visible_dom_options(select, timeout_seconds=0.75)
        except TaobaoListingError:
            dropdown, options = select, []
        raw_candidates = tuple(CandidateValue(
            str(option.get("value") or "dom-label:" + canonical_sha256(option["name"])[:20]),
            str(option["name"]),
        ) for option in options)
        candidates = tuple(sorted(set(raw_candidates), key=lambda value: (value.value_id, value.label)))
        platform = getattr(adapter, "freight_platform_id", adapter.attribute_platform_id)
        field_id = "store-freight:" + canonical_sha256(shop)[:20]
        request = AttributeRequest(
            platform_id=platform, category_leaf_id="store-freight",
            field_id=field_id, field_label=f"运费模板 · {shop}", candidates=candidates,
            excel_value="/".join(alternatives),
            evidence={"force_review": True, "excel": True, "live_dom": True,
                      "shop_name": shop, "scope": "store_freight",
                      "reason": "当前可见候选未匹配，尚需尝试搜索和手填"},
            custom_allowed=False,
            schema_version=canonical_sha256({"platform": platform, "shop": shop,
                "excel": alternatives, "options": sorted((v.value_id, v.label) for v in candidates)}),
        )
        runtime = adapter.attribute_runtime
        confirmed = runtime.confirmed_choice(request) if runtime is not None else None
        if read_only and verified is not None:
            if verified.get("status") != "verified" or not verified.get("value"):
                raise TaobaoListingError("保存前运费尚未确认，不应进入成功回读")
            wanted = (verified["value"],)
        else:
            wanted = (confirmed.label,) if confirmed is not None else alternatives
        chosen = None
        ambiguous = set()
        for name in wanted:
            matches = [v for v in raw_candidates if normalize_option(v.label) == normalize_option(name)]
            if len(matches) > 1:
                ambiguous.add(name)
                continue
            if len(matches) == 1:
                chosen = matches[0]
                break
        if chosen is None:
            # Persisted custom entries need not reappear in the option list.
            # A search input alone is not a committed selection.
            for name in wanted:
                bound = await _committed_freight_value(select, name)
                if bound and name not in ambiguous:
                    return {"status": "verified", "value": name, "value_id": bound,
                            "source": "human_override" if confirmed else "excel_native_input",
                            "schema_version": request.schema_version}
            if read_only:
                raise TaobaoListingError("没有可验证的已保存模板：" + "/".join(wanted))
            entered, attempts = await _try_native_freight(
                adapter, select, tuple(name for name in wanted if name not in ambiguous)
            )
            if entered is not None:
                if adapter.logger:
                    adapter.logger.info("店铺运费搜索/手填并回读通过：%s -> %s", shop, entered["value"])
                return {"status": "verified", **entered,
                        "source": "human_override" if confirmed else "excel_native_input",
                        "schema_version": request.schema_version}
            reason = "；".join(attempts) or "候选重名，无法唯一确定"
            request = replace(request, evidence={**request.evidence,
                "selection_only": True, "custom_input_attempted": True,
                "custom_input_attempts": attempts, "write_error": reason,
                "reason": "候选匹配和页面手填均未通过，无法手填：" + reason,
                "summary": "程序已尝试候选匹配及页面手填，未通过回读，无法手填。请从该店铺候选中选择。"})
            if runtime is None or not candidates:
                raise TaobaoListingError("候选匹配及手填失败：" + reason)
            # Input attempts may close or filter the dropdown. Restore it
            # before applying a previously confirmed review value.
            await adapter._open_select(select, multi=False)
            dropdown = await adapter._active_select_dropdown(select) or select
            resolved = await runtime.resolve(request)
            if resolved is None:
                return {"status": "review_required", "schema_version": request.schema_version}
            matches = [v for v in candidates if v.label == resolved.label and v.value_id == resolved.value_id]
            if len(matches) != 1:
                raise TaobaoListingError("审核模板在当前店铺候选中不唯一")
            chosen = matches[0]
        actual = await adapter._read_select_values(select, multi=False)
        if tuple(actual) != (chosen.label,):
            if read_only:
                raise TaobaoListingError(f"保存回读不同：期望 {chosen.label}，实际 {actual}")
            option = dropdown.locator(".el-select-dropdown__item:visible:not(.is-disabled)").filter(
                has_text=re.compile(r"^\s*" + re.escape(chosen.label) + r"\s*$"))
            if await option.count() != 1:
                raise TaobaoListingError("模板候选节点已变化或重名，未点击")
            await option.click(timeout=3000)
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                actual = await adapter._read_select_values(select, multi=False)
                if tuple(actual) == (chosen.label,):
                    break
                await asyncio.sleep(0.05)
            else:
                raise TaobaoListingError(f"模板填写回读失败：{actual}")
        if adapter.logger is not None:
            adapter.logger.info("店铺运费%s：%s -> %s", "保存回读" if read_only else "同步", shop, chosen.label)
        return {"status": "verified", "value": chosen.label, "value_id": chosen.value_id,
                "source": "human_override" if confirmed else "excel", "schema_version": request.schema_version}
    finally:
        await adapter._dismiss_select_dropdown(select)


async def _committed_freight_value(select, expected):
    return await select.evaluate("""(select, expected) => {
        const vm = select.__vue__;
        if (!vm || vm.value == null || vm.value === '' || Array.isArray(vm.value)) return '';
        const selected = vm.selected;
        const label = selected && typeof selected === 'object'
            ? String(selected.currentLabel ?? selected.label ?? '') : String(vm.selectedLabel ?? '');
        const clean = text => String(text).replace(/\\s+/g, '').toLowerCase();
        if (clean(label) !== clean(expected)) return '';
        if ((!selected || typeof selected !== 'object') && clean(vm.value) !== clean(expected)) return '';
        return typeof vm.value === 'object' ? JSON.stringify(vm.value) : String(vm.value);
    }""", expected)


async def _try_native_freight(adapter, select, alternatives):
    """Type each OR name unchanged; select a real result or commit via Enter.

    Empty lists are supported. Never accept uncommitted search-box text.
    """
    attempts = []
    for name in alternatives:
        try:
            await adapter._dismiss_select_dropdown(select)
            await adapter._open_select(select, multi=False)
            inputs = select.locator('input:not([readonly]):not([type="hidden"]):visible')
            if await inputs.count() != 1:
                attempts.append(f"{name}：当前控件无唯一可编辑输入框")
                continue
            input_box = inputs.first
            await input_box.fill(name, timeout=2000)
            # Give remote exact matches a bounded chance to render. This also
            # supports the native allow-create option used by Element Select.
            deadline = asyncio.get_running_loop().time() + 0.75
            clicked = False
            while asyncio.get_running_loop().time() < deadline:
                dropdown = await adapter._active_select_dropdown(select)
                if dropdown is not None:
                    options = dropdown.locator('.el-select-dropdown__item:visible:not(.is-disabled)').filter(
                        has_text=re.compile(r'^\s*' + re.escape(name) + r'\s*$'))
                    count = await options.count()
                    if count > 1:
                        attempts.append(f"{name}：搜索后仍有重名候选")
                        break
                    if count == 1:
                        await options.click(timeout=2000)
                        clicked = True
                        break
                await asyncio.sleep(0.05)
            else:
                count = 0
            if not clicked and count > 1:
                continue
            if not clicked:
                await input_box.press('Enter', timeout=2000)
            await input_box.press('Tab', timeout=2000)
            await adapter._dismiss_select_dropdown(select)
            actual = await adapter._read_select_values(select, multi=False)
            bound = await _committed_freight_value(select, name)
            if tuple(actual) == (name,) and (bound or clicked):
                return {"value": name, "value_id": bound or name}, attempts
            attempts.append(f"{name}：输入/确认后未形成有效选中值")
        except (TaobaoListingError, PlaywrightError) as exc:
            attempts.append(f"{name}：{type(exc).__name__}")
    return None, attempts
