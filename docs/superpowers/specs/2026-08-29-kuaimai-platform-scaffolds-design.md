# 快麦 ERP 七平台统一骨架设计

## 1. 目标

在现有“基础资料、抖音资料、淘宝资料”逻辑之上，为以下七个平台建立统一、可独立运行、后续可删减的页面骨架：

- 基础资料 `base`
- 抖音 `fxg`
- 淘宝 `tb`
- 天猫 `tm`
- 拼多多 `pdd`
- 微信小店（视频号）`wxsph`
- 小红书 `xhs`

抖音和淘宝保留当前实现，本次只抽取它们已经验证过的通用结构，不重写业务规则。天猫、拼多多、微信小店和小红书先建立字段发现、页面定位、报告和安全门禁，不预设填写值。用户后续确定填写规则后，再补各平台规则并删除不需要的字段处理器。

## 2. 本次边界

### 2.1 包含

- 通过快麦 ERP 同源 API 获取平台固定字段、类目树、类目属性、销售规格、物流及服务规则。
- 在真实 ERP 编辑页触发或点击推荐类目，使类目依赖区域展开。
- 用 DOM 快照确认区块顺序、字段标签、控件类型和显示条件。
- 为四个新增平台建立独立适配器、独立启动入口、统一平台注册和结构化发现报告。
- 为固定字段和类目动态字段分别建模。
- 为无类目、推荐类目错误、接口未开放、字段不一致等情况提供明确状态。

### 2.2 不包含

- 不根据商品标题猜填属性值。
- 不把 API 的预测属性当作用户规则。
- 不保存任何新增平台资料，不铺货。
- 不把新平台加入默认 `all` 执行。
- 不记录 Cookie、令牌、完整请求头、店铺隐私数据或原始超大候选列表。
- 不改动用户当前未提交的淘宝实现，除非后续接入统一注册表时确有必要且变更可隔离。

## 3. 已完成的真实页面发现

本次使用同一商品，在未保存的编辑会话中完成 API 与 DOM 交叉检查。

| 平台 | 固定框架 | 类目动态框架 | 真实发现结果 |
|---|---|---|---|
| 天猫 | 标题、主图、3:4 图、价格、库存、详情、发货地、发票、保修、退货、上下架、SKU 编码/价格/库存 | 品牌、货号、类目属性和商品匹配要求 | 推荐类目“男装 > 休闲裤”可正常展开 schema |
| 拼多多 | 商品名、描述、轮播图、详情图、SKU、市场价、成团人数、发货时限、运费和服务模板 | 品牌规则、类目属性、规格组、SKU/服务/资质规则 | 第一推荐误落童装且接口返回“类目尚未开放”；语义匹配的男装休闲裤候选可返回完整结构 |
| 微信小店 | 标题、短标题、副标题、主图、详情、价格、SKU、重量、物流、售后、资质和保障服务 | 图案、厚薄、腰型、裤长、面料、版型、成分含量、上市时间等 | 当前商品已有叶子类目，API 与 DOM 均返回完整属性结构 |
| 小红书 | 品牌、中文/英文标题、货号、主图、透明图、详情图、视频、物流、运费模板、上架设置 | 23 个类目属性、销售规格、价格库存、发货规则 | 选择“男装 > 休闲裤 > 工装休闲裤”后，属性、候选值、规格及发货接口全部展开 |

关键接口按平台登记，但不把一次抓取结果当作永久规则：

- 天猫：`/tm/detail.json`、`/tm/detailByOtherShop.json`、`/publish/fast/prediction/cat.json`、`/dsb/queryCategoryConfigInfo.json`、`/tm/getProductMatchSchema.json`
- 拼多多：`/pdd/detail.json`、`/publish/fast/prediction/cat.json`、`/pdd/getSpecList.json`、`/pdd/getBrandRequireRule.json`、`/pdd/getCategoryProperties.json`
- 微信小店：`/wxsph/detail.json`、`/wxsph/getCategoryTree.json`、`/wxsph/getCategoryProperties.json`、`/wxsph/getTemplateList.json`
- 小红书：`/xhs/detail.json`、`/xhs/getCategoryTree.json`、`/xhs/getAttributeList.json`、`/xhs/getAttributeValues.json`、`/xhs/getVariations.json`、`/xhs/getDeliveryRule.json`、物流和运费模板接口

## 4. 推荐方案

采用“平台注册表 + 公共发现器 + 四个薄适配器”，而不是复制四份抖音/淘宝代码，也不建立包含所有平台细节的巨型基类。

### 4.1 平台注册表

`PlatformSpec` 作为七个平台的唯一清单：

```text
PlatformSpec
  key
  display_name
  tab_label
  tab_id
  status                 # implemented / preview / scaffold
  enabled_in_all
  supports_save
  supports_publish
  adapter_factory
```

命令行平台选项、帮助文本、独立启动器和 `all` 的展开均从注册表取得。`scaffold` 平台固定 `enabled_in_all=false`、`supports_save=false`、`supports_publish=false`。

### 4.2 两层字段模型

```text
PlatformSchema
  platform_id
  category
    leaf_id
    path
    source                # recommendation / existing / manual
    candidate_rank
    status                # ready / pending_category / review_required / unsupported
  fixed_sections[]
  dynamic_sections[]
  endpoints[]
  capture_status

SectionSchema
  key
  label
  order
  visible_when
  fields[]

FieldSchema
  key
  label
  input_type              # input/select/radio/checkbox/upload/table/editor
  value_type
  required
  multiple
  option_source           # inline/api/runtime
  option_count
  dependencies[]
  api_path
  dom_locator_hint
  status
```

没有点击或传入类目时，固定框架照常生成；动态框架标记为 `pending_category`，不能报告为完整。

### 4.3 类目发现流程

1. 调平台推荐类目接口，保留候选路径、叶子 ID、排序及接口状态。
2. 有“点击使用”时，在未保存页面中点击候选，让动态区渲染；平台没有推荐区时，使用已有类目或在类目树选择语义一致的叶子类目。
3. API 返回字段键、类型、必填、候选来源及依赖；DOM 返回区块顺序、标签、控件类型和稳定定位线索。
4. API 与 DOM 交叉核对。只存在于一侧的字段保留并标记差异，不删除、不猜测。
5. 第一推荐明显与商品不符、接口报未开放或结构为空时，记录失败，再尝试下一个语义一致且唯一的候选。
6. 没有唯一可靠候选时标记 `review_required` 并停止动态发现，不自动确认。

推荐类目和候选值都在运行时重新拉取，不写死本次商品的叶子 ID。

### 4.4 API 与 DOM 的职责

- API：字段 ID、类型、必填、可多选、自定义能力、候选接口、规格限制、服务规则。
- DOM：真实显示区块、字段标签、控件形态、显示条件和可操作定位。
- 定位器：优先使用平台页签、区块标题、字段标签与相邻控件关系；不使用一次性的 Playwright `ref` 或动态 class。
- 候选值：报告保存数量、摘要和来源，正式填写时再实时查询，避免规则建立前产生过期大文件。

## 5. 文件与运行结构

建议新增：

```text
platform_registry.py
platform_schema.py
platform_discovery.py
tm_listing.py
pdd_listing.py
wxsph_listing.py
xhs_listing.py
run-tmall.command
run-pdd.command
run-wxsph.command
run-xhs.command
tests/test_platform_registry.py
tests/test_platform_discovery.py
tests/test_tm_listing.py
tests/test_pdd_listing.py
tests/test_wxsph_listing.py
tests/test_xhs_listing.py
```

四个启动器都固定使用：

```text
--platform <platform> --inspect-only --no-save
```

固定参数放在用户附加参数之后，避免被覆盖。发现模式从专用入口打开商品编辑页和目标平台，不经过基础资料填充、图片上传、保存或铺货路径。

每次发现产生脱敏报告：

```text
output/platform-schema/<run-id>/<platform>.json
output/platform-schema/<run-id>/<platform>-dom.json
output/platform-schema/<run-id>/summary.json
```

报告包含字段结构、接口路径、候选数量、API/DOM 差异及异常；不包含认证信息或完整店铺数据。

## 6. 安全门禁

- `inspect-only` 在调度层最高优先级短路，禁止调用基础资料填写和任何上传方法。
- `scaffold` 平台即使用户传入保存参数，也在打开浏览器前拒绝保存。
- `--save-only` 只允许已明确支持且已验收的平台。
- `all` 只运行 `enabled_in_all=true` 的已实现平台。
- 新增平台适配器不暴露 `save()` 或 `publish()`；以后实现规则时再显式增加能力并单独验收。
- 页面关闭或刷新即丢弃本次临时类目选择；发现结束后主动关闭编辑会话。

## 7. 错误处理

- 无类目：固定框架成功，动态框架 `pending_category`。
- 推荐类目路径冲突：记录候选和原因，`review_required`。
- 类目未开放：记录接口错误，不把空结构当成功。
- API 可用、DOM 未显示：标记 `api_only`。
- DOM 已显示、API 无字段：标记 `dom_only`。
- 页面或接口结构变化：保留原始状态摘要并失败退出，不进入填写流程。

## 8. 测试与验收

### 8.1 单元测试

- 注册表只包含本次七个平台且顺序固定。
- 四个平台响应 fixture 可归一化为同一 schema。
- 无类目仍生成固定框架，动态区状态正确。
- 拼多多首选类目不可用时安全降级，不能把错误类目当完成。
- API/DOM 差异被保留并报告。
- 脱敏报告不含 Cookie、请求头和店铺隐私数据。

### 8.2 调度与门禁测试

- `inspect-only` 对基础资料填写、上传、保存和铺货均为零调用。
- 四个 scaffold 平台拒绝保存。
- `all` 排除 preview/scaffold 平台。
- 启动器的固定平台与 `--no-save` 不能被用户参数覆盖。

### 8.3 真实页面验收

1. 每个平台单独打开真实编辑页。
2. 触发推荐类目；无推荐区的平台使用已有或唯一匹配叶子类目。
3. 对照 API schema 与 DOM 字段，生成脱敏报告。
4. 检查页面没有基础资料改写、上传、保存或铺货请求。
5. 关闭页面，确认 ERP 中资料没有被保存。
6. 运行现有完整测试，确保抖音与淘宝行为未回归。

## 9. 后续加规则方式

以后每个平台单独增加 `RuleSet` 和 `apply_rules()`，只消费已确认字段。删除某个平台不需要的架子时，删除对应字段处理器或 section 配置即可，不影响平台注册、发现报告和其他平台。任何保存或铺货能力仍需用户明确授权并完成真实页面复核后才能开启。
