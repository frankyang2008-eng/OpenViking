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

## 测试验证与回归归因（2026-10-08 补全）

- 构建：0.4.24.dev265，make build 全通过（C++ ext + maturin + web-studio + editable）
- 环境：uv pip check 280 packages 全兼容；AST 扫描 1533 文件 0 语法错误，0 坏 import（19 条 openviking_sdk/live_auth 为扫描器误报，实际均可导入）
- ov doctor：9/10 PASS；Embedding FAIL 为宿主 ov.conf 10-07 晚用户自改（local/doubao-embedding-vision），非合并引入
- openclaw 插件 tsc --noEmit：0 error
- rerank 活体验证：doubao-seed-2.0-mini (llm_score)，1.31s，相关文档 0.85/0.5/0.25 全高于噪声 0.0，噪声≤阈值 0.05 → PASS（plans/rerank-mini-verification/live-18th-sync.json）
- rerank 相关单测 54 passed；queue_manager 冲突决议单测 16 passed（含上游新增 loop-scoped client 测试）

### 对账结果（tests/{misc,client,session,server} + retrieve/storage 补充）
| 套件 | 失败 | 新增 | 归因 |
|---|---|---|---|
| misc | 24 | 0 | 全豁免 |
| client | 8 | 0 | 全豁免 |
| session | 32 | 2 | 上游 #5696（详见下） |
| retrieve+storage | 1 | 1 | 上游 stale-mock（已入账） |
| server | 81 | 4 | 上游 #5696（详见下） |
| **合并引入回归** | — | **0** | — |

### 上游 #5696（WM 默认 off）连锁债 —— 5 条，全部铁证归因
1. test_session_commit_race::test_message_added_during_commit_not_lost：**永久挂起**（非失败）。WM=off 时 commit 不再调用 _generate_archive_summary_async，测试 monkeypatch 等 phase1_done 永不触发。铁证：纯 upstream/main 76511554e worktree 挂死 >200s；pre-sync worktree 1.25s PASS。对账需 --deselect。
2-3. test_compressor_v3 factory 2 条：order-dependent（依赖此前测试初始化的 VikingFS 单例），pre-sync worktree standalone 同败。
4-5. server 3 条 memory-policy 断言 + test_sdk_get_session_archive：#5696 改 MemoryPolicy.to_dict 恒写 working_memory 键 + 默认 off，server 测试期望未跟上；纯 main worktree 实测 agent_evolution 1 条同败，SDK 测试空 overview 由 session.py:2038 WM-off 早退直接导致。

**上游债共性**：#5696 改默认值但上游 CI 全部 workflow 不跑 tests/{server,session,misc,client}（17th sync 已定位的 CI 盲区），破损直接合入 main。建议打包提上游 PR：修 5 个测试期望 + commit_race 测试加 working_memory 显式 opt-in。

### 账目
- 债清单：153 条（146 基线 + 7 新入账：1 storage stale-mock + 1 session 挂起 + 2 order-dependent + 3 server WM 断言；SDK 测试与 agent_evolution 重叠计入 server 4 条中的 2 条独立 id）
- 已提交：ddc978f53（merge）、0511fb09a（报告+债账+rerank 证据）
