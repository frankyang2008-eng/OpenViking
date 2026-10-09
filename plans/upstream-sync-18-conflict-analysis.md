# 第 18 次上游同步冲突分析报告（2026-10-08）

- 回滚 tag：`pre-sync-20261008`（ov-dev-opt @ 0d9c0b5f5）
- 上游目标：upstream/main @ 76511554e（main 领先 26 commits）
- main 处理：ff-only 快进至 76511554e（纯镜像，无本地提交）
- merge commit：ddc978f53（parents 0d9c0b5f5 + 76511554e）

## git 冲突（4 文件）

### 1. openviking/storage/queuefs/queue_manager.py
- 冲突点：TaskWorkIndex import 路径。
- 本地：15th sync 把 task_work_index 从 service/ 迁到 queuefs/，import 已改 `openviking.storage.queuefs.task_work_index`。
- 上游：仍在老路径 `openviking.service.task_work_index`，并新增 `LoopScopedAsyncClientCache` import + worker loop 关闭前清空 loop-scoped async client（#5675）。
- 决议：**保留本地路径 + 采纳上游新增 import 与 shutdown 逻辑**（close_current_loop_clients 已自动并入函数体，验证在 line 377）。
- 语义漂移检查：这 26 个 commit 里上游**未改动** task_work_index.py 本身 → 无需回迁内容。`service/task_work_index.py` 在本地不存在，上游引用方仅 queue_manager（已修）。全仓 grep `openviking.service.task_work_index` 仅剩 graphify-out 缓存 JSON，无活代码。

### 2. tests/storage/test_queue_manager.py
- 冲突点：import 块 + 测试插入区。
- 本地：worker lifecycle 测试组（thread leak / re-init / stop）+ bind_task_context 本地路径。
- 上游：新增 `test_queue_worker_closes_loop_scoped_clients_before_closing_its_loop`。
- 决议：**并集**——本地测试组与上游新测试共存，import 双方都保留。AST 语法校验过。

### 3. tests/utils/mock_agfs.py
- 冲突点：`AGFSNotFoundError` import 风格。
- 本地：`from openviking.pyagfs.exceptions import ...`（更轻，避免拉起整个 pyagfs/__init__）。
- 上游：`from openviking.pyagfs import ...`。
- 决议：**保留本地**。两者等价（__init__.py:31 re-export exceptions.AGFSNotFoundError），本地为刻意轻量化选择。

### 4. examples/openclaw-plugin/services/context-lifecycle-service.ts（最深的一处）
- 上游 #5676 重写了 auto-recall 装配流：
  - 新增 `resolveAssembleRouting` + `readProfileBlock`（读 `viking://~/memories/profile.md` 及 actor peer profile，8000 字符截断，Promise.allSettled 容错）
  - `recallForAssemble` 改为 4 参签名 `(params, recallQuery, client, routing)`，client/routing 外提
  - 主装配：Promise.all 并行拉 profile + recall，token 预算降级链 [profile+recall] → [profile] → [recall]
- 决议：**三个 hunk 全取上游**。理由：
  1. 本地 2 参旧版 recallForAssemble 的能力（queryConfigStore、traceRecorder、dedupTurns）上游新版签名已全部继承；
  2. 第二个调用点（line 750，resume 路径）上游已改为 4 参，若保留本地 2 参定义会产生类型断裂；
  3. 上游去掉 `!params.prompt` 早退是**有意为之**——profile 注入不应依赖 prompt 存在（recall 仍由 query.length<5 门控）；
  4. 上游预算降级链严格强于本地单次超预算即放弃。
- 本地独有改动（hasAutoRecallBlock / prependRecallToLatestUserMessage 等）在冲突区外，均已保留。
- 验证：`examples/openclaw-plugin` 本地 tsc --noEmit **0 error**；`client.read(uri, actorPeerId, timeoutMs)` 3 参签名在本地 client.ts:59 存在，readProfileBlock 调用兼容。

## 本地未提交改动（merge 前处理）
- `config-schema.mjs`（omp 插件）：resumeArchiveInject / takeoverEnabled 默认 true→false，为本地刻意配置。已在 merge 前以独立 commit 固化在 ov-dev-opt，避免被 merge 吞掉。

## Not touched（上游债，逐字节保持）
- 上游自带 lint/type/测试债本次不在 merge 中修改，待测试对账（tasks/check-regressions.sh）归类。

## 待验证（见构建与测试步骤）
- AST 全量 import 扫描
- tests/{server,misc,client,session} + 对账脚本
- doubao-seed-2.0-mini 活体 rerank
- ov doctor
