# 快麦 ERP 自动铺货

该程序读取产品文件夹中的 `产品信息.xlsx`、1:1 主图、3:4 主图、详情页图和 SKU 图，自动编辑快麦商品中心的基础资料。

当前默认产品：

`smb://gongxiang/共享文件/谭/products/绿巨人+NGBL-10588/产品信息.xlsx`

执行顺序：

1. 读取 Excel 第二行商品名称、货号和“吊牌价/价格/基本售价”。
2. 打开 `https://erp.superboss.cc/index.html#/index/`，通过 ERP 首页“快麦通”入口完成单点登录。
3. 优先调用快麦 `/item/base/page` 同源 API 确认目标商品，DOM 查询作为兜底。
4. 打开“编辑 -> 基础资料”，替换商品名称和所有指定图片。
5. 将基本售价批量设置到全部 SKU。
6. 保存前复核款式编码、商品名称、图片数量、售价和页面校验错误。
7. 最后点击“保存”，并通过 `/item/base/edit` 响应或“保存成功”提示确认结果。

## 运行

首先在 Finder 中确认 `smb://gongxiang/共享文件` 已挂载。

双击 `run.command`，或在终端运行：

```bash
./run.command
```

首次会打开一个自动化专用的 Google Chrome，入口是快麦 ERP 首页 `https://erp.superboss.cc/index.html#/index/`。请在 20 分钟内完成 ERP 登录；程序会等待 ERP 单点登录完整结束，再进入商品中心。

登录状态同时保存在 `output/kuaimai/chrome-profile/` 和 `output/kuaimai/auth-state.json`。后者专门保留 Chrome 正常关闭时会清理的会话 Cookie，后续运行会自动恢复。该文件包含本机登录凭证，请勿发送给他人。

针对快麦页面加载较慢的情况，默认等待时间已调整为：页面和保存 300 秒、每组图片上传 600 秒。编辑抽屉会等待加载遮罩消失并确认款式编码出现后，才开始填写。

> 根据本次需求，程序默认会点击保存。

只检查 Excel 和图片目录：

```bash
./run.command --dry-run
```

完整填写并校验，但不保存：

```bash
./run.command --no-save
```

默认情况下，如果出现“同步至 ERP 系统资料”确认框，程序选择“跳过同步”，仅保存快麦通资料。需要同步时：

```bash
./run.command --sync-erp
```

使用其他产品：

```bash
./run.command --excel-url 'smb://gongxiang/共享文件/谭/products/其他产品/产品信息.xlsx'
```

## 安全机制

- 查询不到唯一款式编码时停止。
- SKU 图数量与第一规格值数量不一致时停止，防止颜色错配。
- 图片未全部上传或页面存在校验错误时不保存。
- 出现编码重复并要求创建新编码时停止，不自动确认。
- ERP/快麦通认证链未完成时停止，不在 SSO 中间页强制跳转。
- 每次运行的日志、保存前/后截图和结果保存在 `output/kuaimai/runs/`。
