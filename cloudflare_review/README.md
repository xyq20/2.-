# 快麦审核中心

原生 TypeScript Worker；D1 是审核、候选快照与续跑事件的权威库。网页只确认属性，绝不操作快麦保存/铺货。所有快麦浏览器会话留在本地执行器。

## 本地开发与验证

Node.js 22 或更新版本。以下命令在 `cloudflare_review/` 执行：

```sh
npm ci
npm run types -- --include-runtime=false
npm run db:migrate:local
npm test -- --run
npm run test:bootstrap
npm run check
npm run dev
curl -fsS http://127.0.0.1:8787/health
```

`/health` 返回 `{"ok":true,"service":"kuaimai-review"}`。本地数据库与 R2 只写入忽略的 `.wrangler/`；测试使用独立的 Workers runtime、实际 D1/R2 模拟 binding，每项测试清空自身测试数据。测试与开发从不配置 `remote:true`。

开发如需设备接口，手动建立已被 Git 忽略的 `.dev.vars`，仅放专用于本机测试的 `DEVICE_TOKEN`、`SESSION_SECRET`。不要复制真实平台 Cookie、Chrome Profile 或线上密钥。浏览器登录 Cookie 始终为 Secure；本机浏览器测试可使用 localhost 安全上下文，正式环境必须 HTTPS。

可选浏览器烟测（启动本地服务后）：

```sh
# 已安装 Playwright 时；否则先在你的测试环境安装 playwright 和 Chromium。
REVIEW_LOCAL_URL=http://127.0.0.1:8787 node scripts/browser-smoke.mjs
# 如使用系统 Chrome：PLAYWRIGHT_CHANNEL=chrome
# 如使用已安装的独立 Playwright：PLAYWRIGHT_MODULE=/absolute/path/playwright/index.mjs
```

截图在 `output/playwright/`（忽略目录）。该浏览器测试用合成 API 响应验证桌面/手机、领取、改选原因、确认及切换任务；真实后端鉴权、事务和存储由 Worker 集成测试验证。它不是线上快麦端到端验收。

工具版本由 lockfile 固定。当前 Workers pool 内置 Miniflare 早于兼容日期，因此将 Miniflare 显式覆盖到与 Wrangler 一致的版本；Sharp 覆盖到已修补版本。升级时一并运行所有测试和 `npm audit`，不要盲目 `npm audit fix --force`。

## 首位管理员（仅派生密码材料）

`scripts/hash-password.mjs` 仅从 stdin 读取，拒绝命令行密码或交互式裸输入；输出只有随机 salt 与 PBKDF2-SHA256 hash。要求 12–1024 字符，310,000 次迭代。Worker 使用 `nodejs_compat` 的异步 PBKDF2，因为 Workers Web Crypto 的单次 PBKDF2 迭代上限不足 310,000。

macOS zsh 在本机安全输入：

```sh
read -rs 'bootstrap_password?管理员密码：'
printf '%s' "$bootstrap_password" | node scripts/hash-password.mjs
unset bootstrap_password
```

不要把密码写入 shell 历史、命令参数、日志、SQL 或仓库。把上述派生结果填入只包含 salt/hash 的受保护 SQL 文件（不要提交仓库）：

```sql
INSERT INTO users(id,username,password_salt,password_hash,role,active,created_at)
VALUES('first-admin','admin','<SALT>','<HASH>','admin',1,'<UTC_ISO_TIMESTAMP>');
```

本地导入：`npx wrangler d1 execute kuaimai-review --local --file /absolute/path/bootstrap.sql`。线上导入见下节，必须确认目标环境。创建 operator 同样使用新盐、新 hash，`role='operator'`。用户禁用 `active=0` 后，其所有会话立即失效；也可设置 `sessions.revoked_at`。D1 只保存 32 字节随机会话 token 的 SHA-256，Cookie 为 HttpOnly、Secure、SameSite=Strict、8 小时。浏览器写请求必须同源 Origin。`SESSION_SECRET` 预留给后续会话策略，当前 opaque session 不依赖签名；撤销会话应更新 D1，而非仅轮换此 secret。

## 正式设置与部署（说明，不由本阶段执行）

本仓库没有真实 account/database ID、凭证、远程资源。以下是授权后由管理员执行的顺序，**本地实现阶段未执行创建、远程迁移或部署**。

```sh
npm ci
npx wrangler d1 create kuaimai-review --binding DB --update-config
npx wrangler r2 bucket create kuaimai-review-assets
# 检查 wrangler.jsonc 中 DB 的实际 database_id 与 ASSETS 桶名，R2 不启用公共访问。
npx wrangler secret put AI_MODEL
npx wrangler secret put MODEL_API_URL
npx wrangler secret put SESSION_SECRET
npx wrangler secret put DEVICE_TOKEN
npx wrangler secret put MODEL_API_KEY
npm run db:migrate:remote
npx wrangler d1 execute kuaimai-review --remote --file /absolute/path/bootstrap.sql
npm run deploy
```

每个 secret 使用交互式输入，不写在参数或 SQL 中。模型名与 endpoint 按部署时选型填写，不在源码预设提供商。部署后访问 Worker HTTPS `/health`，登录测试 admin/operator 权限、设备事件去重、审核确认、设备续跑与私有图片。调度 `0 3 * * *` 是 **UTC 每日 03:00（北京时间 11:00）**，每次最多清理 100 张到期原图；大量积压需额外授权的清理调用或提高调度频率。

## 设备协议

统一 `POST /api/device/events`，头部 `Authorization: Device <token>`、`Content-Type: application/json`。鉴权通过 SHA-256 后恒时比较；请求体最多 256 KiB。信任边界是单个已授权本地执行器部署；如果部署给多个互不信任设备，必须改成逐设备 token 与设备 ID 绑定后再启用，当前共享 token 不隔离设备主体。

统一 envelope：`{"idempotency_key":"稳定且唯一的操作键","event_type":"以下类型","payload":{...}}`。同 key 首次 201，重送 409，均返回同一 `event_id`。事件记录和副作用同事务提交，失败依赖不会占用幂等键。重送须保留原始 envelope；409 对账 `event_id` 后标记已发送，不重复生成新 key。

以下是每类 payload 示例；这里所有值都是示例，不是凭证：

```json
{"event_type":"product.upsert","payload":{"product_version":"pv1","style_code":"K01","title":"休闲裤","category_json":{"leaf":"pants"}}}
{"event_type":"snapshot.created","payload":{"snapshot_version":"sv1","platform_id":"pdd","category_leaf_id":"pants","field_id":"length","field_label":"裤长","schema_version":"schema1","custom_allowed":false,"options":[{"value_id":"long","label":"长裤","position":0}]}}
{"event_type":"review.created","payload":{"id":"review1","run_id":"run1","device_id":"device1","product_version":"pv1","platform_id":"pdd","category_leaf_id":"pants","field_id":"length","field_label":"裤长","canonical_field":"pants_length","snapshot_version":"sv1","suggested_value_id":"long","reason_code":"needs_review","evidence_json":{"summary":"裤脚接近脚踝"}}}
{"event_type":"checkpoint.updated","payload":{"run_id":"run1","product_version":"pv1","device_id":"device1","execution_mode":"all","platform_order":["base","pdd"],"current_index":1,"status":"waiting_review","pending_review_id":"review1","version":1,"image_version":"images1"}}
{"event_type":"stage.completed","payload":{"run_id":"run1","platform_id":"pdd","status":"readback_verified","expected_json":{"length":"long"},"readback_json":{"length":"long"},"verified":true}}
{"event_type":"readback.recorded","payload":{"run_id":"run1","product_version":"pv1","platform_id":"pdd","field_id":"length","snapshot_version":"sv1","actual_value_id":"long","actual_label":"长裤","verified":true,"payload_json":{"source":"api_readback"}}}
{"event_type":"text_facts.created","payload":{"product_version":"pv1","source":"excel","payload_json":{"material":"棉"}}}
```

实际发送要为每个例子补上 envelope 的 `idempotency_key`。union 与每类允许键定义在 `src/types.ts`、`src/device-events.ts`。未知类型/未知键、嵌套 password/token/cookie/authorization/secret/API key/Chrome profile 字段返回 400。`visual_facts` 由 Worker 内部分析写入，不开放设备伪造模型视觉输出的事件。`snapshot_version` 一经创建不可改变候选；更新候选应产生新版本。checkpoint 更新应显式传递递增 version，不能覆盖更高版本。原始 `verified=false` 回读可以入库，但只允许 `verified=true` 且经过业务门禁的样本参与学习。

## 审核与续跑

浏览器：`GET /api/reviews`；`POST /api/reviews/:id/{claim,renew,skip}` 带 `{"version":N}`；`confirm` 另带 `final_value_id` 与改选时的 `correction_reason`。领取/续租为 10 分钟，每次版本递增。过期、他人持有或版本冲突返回 409；候选在当前 snapshot 中出现次数不为一则 422。确认事务写审核动作、人工决策、唯一 resume_ready 事件，并从 confirmed 转到 resume_ready。skip 变回 pending，不发续跑。网页只在服务器成功返回后切下一项，重复提交按钮禁用。

设备：`GET /api/device/resume?device_id=device1` 返回 `{"events":[{"event_id":"...","payload":{"review_id":"...","run_id":"...","product_version":"...","snapshot_version":"...","final_value_id":"...","version":3}}]}`。本地须重新验证商品、图片、执行模式、当前快麦候选和检查点，先持久化续跑检查点，然后 `POST /api/device/resume/:event_id/ack`：

```json
{"device_id":"device1","checkpoint_id":"local-durable-checkpoint-id","checkpoint_persisted":true}
```

ack 只确认本地已接管恢复任务，不等于保存/铺货成功。后续仍须本地保存回读。服务无法直接验证本机磁盘，依赖已鉴权执行器如实报告，缺失上述确认则拒绝 ack。重复相同 ack 幂等；不同 checkpoint 标识返回冲突。

管理员 API：`GET /api/admin/status`；`POST /api/admin/reviews/:id/release` 释放锁；`POST /api/admin/reviews/:id/invalidate` 撤销尚未 consumed 的审核，均需 version。operator 对整个 `/api/admin/*` 前缀返回 403。

## 图片与数据删除

设备 `PUT /api/device/assets/:sha256` 带 `x-product-version`、`x-asset-kind`（original/learning_thumbnail）、Content-Type（image/jpeg、image/png、image/webp）。最多 10 MiB，服务器重新计算 SHA-256。对象键为 `products/<product_version>/<sha256>/<kind>`，同图重复上传返回相同 asset_id。商品版本必须已入库。

浏览器 `GET /api/assets/:id` 必须有效会话与当前有效任务领取权限，或管理员身份；不暴露 R2 公共 URL，响应 private/no-store。原图保留 30 天；学习缩略图长期保留，只有明确管理员删除才删除。`cleanupExpiredOriginals` 每调用最多 100 项，R2 删除成功后才移除 D1 元数据。

管理员 `DELETE /api/admin/products/:product_version`：先 tombstone 阻止并发写入，再批量删 R2，最后用 FK CASCADE 删除商品、事实、人工决策、审核、关联事件、检查点与回读；删除失败可重试。受该商品影响的聚合规则保守停用并清零计数，必须重新学习。跨商品候选结构快照保留。R2 与 D1 没有跨服务事务；tombstone/重试让失败可恢复，不声称跨服务原子删除。

## 向前迁移与回滚

已上线 migration 不改写、不破坏性降级；增加新编号迁移，在独立测试环境验证后按前向兼容顺序发布。上线前记录 Worker version 与 D1 恢复时间点。Worker 故障可以 `npx wrangler rollback <VERSION_ID>` 回到兼容当前 schema 的版本，**不要反向执行 DROP/重建 SQL 作为回滚**。数据恢复使用 Cloudflare D1 Time Travel（以账户当前可用能力/保留期为准）；R2 已删除对象无法由 D1 恢复，需另行备份策略。阶段二只交付本地代码与模拟测试，线上资源和真实快麦续跑尚待单独授权验收。
