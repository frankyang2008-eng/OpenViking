# 第 12 次上游同步 — 冲突预分析报告

**日期**: 2026-09-21
**merge-base**: `80c59f13e`（第 11 次同步的基线）
**上游目标**: `172c10507`（upstream/main，含 tag `v0.4.21`）
**本地**: `ov-dev-opt` = `119c1945a`
**规模**: 上游 69 commits / 682 files / +39761 -17202
**干跑结果**: `git merge-tree --write-tree` exit=1 → 15 个文件级冲突（真实 merge 完全复现）

---

## 0. 本地 fork 的结构性不变量（本次冲突的根因）

| 不变量 | 本地 (ov-dev-opt) | 上游 (main) | 影响 |
|---|---|---|---|
| `task_work_index` 位置 | `openviking/storage/queuefs/task_work_index.py` | `openviking/service/task_work_index.py` | 上游新增 11 处引用旧路径 → merge 不报冲突但 ImportError |
| 语义执行器文件名 | `semantic_dag.py` | `semantic_executor.py`（rename +540 行） | rename/add 冲突 |
| 构建脚本位置 | 上游 `scripts/build_support/` | 同上 | 本地无改动 → rename 自动解析 ✅ |
| `agent_evolution_config` | 上游 `openviking_cli/utils/config/` | 同上 | 本地无改动 → 自动解析 ✅ |
| `account_settings.py` | 上游已删除 | — | 本地无改动 → 自动解析 ✅ |

**关键结论**：本地对 `build_support/`、`account_settings.py`、`agent_evolution_config.py` 三个结构变化**零改动**（`git diff --numstat` 为空），上游的 rename/delete 全部干净落地，无本地逻辑丢失。

---

## 1. 解析规则（按优先级）

- **R1 路径修正**：上游内容里凡出现 `openviking.service.task_work_index` → 改写为 `openviking.storage.queuefs.task_work_index`。这是本地 fork 解循环依赖的唯一实质改动，必须保留。
- **R2 丢弃格式化**：本地大部分"改动"是 pi-lens / ruff / prettier autofix 噪音（表格 pipe 加空格、折行重排）。**取上游**。
- **R3 保留本地真特性**：queue_manager 线程/超时/executor、openclaw 插件多 harness 检测、本地回归测试。

---

## 2. 15 个冲突文件 — 逐个判定

### 代码（5 个）

| 文件 | 本地改动 | 上游改动 | 冲突性质 | 解析 |
|---|---|---|---|---|
| `openviking/storage/queuefs/semantic_executor.py` | 无此文件（改的是 `semantic_dag.py`，仅 1 行 import 路径） | rename + 重写 (+540) | rename/add | **取上游新文件 + R1**（line 18 路径改写）；删除本地 `semantic_dag.py` |
| `openviking/storage/queuefs/semantic_processor.py` | +1/-1（import 路径） | +447/-80 | 2 块：①上游新增 3 个 import ②import 位置互换 | ①取上游 3 import，`task_work_index` 走 R1 ②取上游 `semantic_work` import，丢弃 HEAD 重复行 |
| `openviking/storage/queuefs/queue_manager.py` | +105/-20（线程池、init 锁、agfs 调用超时、atexit） | +17/-1（`_embedding_worker_stopped` 事件 + `CancelledError` 处理） | except 子句同位置，**互补不冲突** | **两留**：上游 `CancelledError` 块 + 本地 `TimeoutError` 块 |
| `openviking/utils/embedding_utils.py` | +3/-7（1 行 import + 2 处纯折行） | +142/-40（`action=IndexAction(...)` + `level_overrides` 提取） | 3 块 | 块1 **union**（HEAD 的 `task_work_index` 新路径 + 上游 `resource_rnfv` import）；块2/3 **取上游**（本地是折行噪音） |
| `tests/storage/test_queue_manager.py` | +103/-3（本地回归测试段） | +130/-0（上游新测试） | 2 块：import + 测试体 | **两留**：import union（`SimpleNamespace` + `AsyncMock, MagicMock, patch`）；测试体两段都保留 |

### 前端 / 示例（3 个）

| 文件 | 判定 |
|---|---|
| `examples/claude-code-memory-plugin/scripts/config.mjs` | **保留本地**：`detectHarness` import + `HARNESS` 常量 + `buildPluginConfig(HARNESS, ...)`（多 harness User-Agent 特性）。**移除** `normalizeRewriteMode` import —— 上游已删掉 `recallRewrite` 块，该符号 merge 后零引用 |
| `examples/openclaw-plugin/auto-recall.ts` | **取上游**。`!digest &&` 是上游新增（recallCompress 特性），本地 diff 无此 hunk，冲突纯属相邻折行。本地改动全是 prettier 折行 |
| `examples/openclaw-plugin/config.ts` | **取上游**：新增 `recallCompress` 校验块（`server`/`auto`/`off`）。本地改动为折行噪音 |

### bot 测试（2 个）

| 文件 | 判定 |
|---|---|
| `bot/tests/test_compile.py` | **取上游**。上游 `e1a0d3e0d` 整合测试套件 (-1921)。本地仅 3 处空行/折行。冲突块内的大段测试来自 merge-base，上游已删除 |
| `bot/tests/test_cron_config.py` | **保留本地**：`ToolsConfig(cron=CronConfig(...))` 类型化构造（本地 mypy 优化）。import 行取 union 并**删掉已无引用的 `from vikingbot.config import loader`** |

### 文档（5 个）

| 文件 | 判定 |
|---|---|
| `docs/en/api/01-overview.md`（3 块） | **取上游** |
| `docs/en/guides/01-configuration.md`（2 块） | **取上游**（rerank provider 补 `litellm`/`jev`；openGauss 段落上游已迁移） |
| `docs/zh/api/01-overview.md`（2 块） | **取上游** |
| `docs/zh/api/03-filesystem.md`（1 块） | **取上游**（新增 `-f/--fields` 说明） |
| `docs/zh/guides/01-configuration.md`（2 块） | **取上游** |

**依据**：本地 docs 改动是 markdown 表格 pipe 加空格 + 删空行的格式化噪音；上游本批做了文档体系重写（`#5186`/`#5187`/`#5188`/`#5189`/`#5195`/`#5244`）并加了双语一致性校验。保留本地噪音会破坏上游校验。

---

## 3. merge 检测不到的语义风险：11 处旧路径残留

干跑 merge 后全树扫描 `openviking.service.task_work_index`，得 11 处残留（**上游新增的引用点**，本地迁移覆盖不到）：

**源码 5 处**（必须改，否则 ImportError）
1. `openviking/utils/log_correlation.py:6`（上游新增文件）
2. `openviking/storage/queuefs/semantic_executor.py:18`（rename 新文件）
3. `openviking/storage/queuefs/semantic_processor.py:43`（冲突内，见上）
4. `openviking/service/task_processing_time.py:16`（上游新增文件）
5. `openviking/service/resource_service.py:2447`（上游新增函数内 import）

**测试 6 处**
6. `tests/server/test_skill_update_cancellation.py:13`
7. `tests/storage/test_semantic_processor_permanent_storage_error.py:14`
8. `tests/storage/test_skill_package_cancellation.py:16`
9. `tests/storage/test_context_update_plan.py:2150`
10. `tests/service/test_skill_processing_cancellation.py:14`
11. `tests/service/test_task_processing_time.py:12`

---

## 4. 静默自动合并的 38 个双方共改文件

交集 53 = 15 冲突 + 38 自动合并。逐个核对本地改动量后确认：**除 `queue_manager.py` / 两个 TS/JS 示例 / `setup.py` 外，本地改动全部是 1 行 import 路径迁移（+1/-1）或格式化**。

已实证安全：
- `service/deletion.py`、`service/reindex_executor.py`、`service/task_tracker.py`、`storage/queuefs/add_resource_processor.py`、`storage/viking_fs/_snapshot.py`、`tests/parse/test_feishu_parser_api.py`、`tests/service/test_resource_service_watch.py`、`tests/service/test_task_queue_middleware.py`、`tests/test_task_tracker.py` — 本地均为 +1/-1 import 迁移，干跑后**无旧路径残留** ✅
- `openviking/resource/watch_scheduler.py`(+2/-5)、`openviking/storage/collection_schemas.py`(+1/-5)、`openviking/models/embedder/openai_embedders.py`(+6/-2)、`openviking/storage/viking_fs/_grep.py`(+3/-1)、`tests/session/test_session_retention_integration.py`(+1/-3) — 逐个看 diff，**全是格式化** ✅
- `examples/openclaw-plugin/client.ts`(+170/-76) — 格式化；上游 +2 行（`recallCompress` 类型与透传）落点独立 ✅
- `setup.py`(+3/-7) — 上游 rename 到 `scripts.build_support.*` 已正确解析；本地 `CMAKE_OSX_DEPLOYMENT_TARGET=11.0` 保留 ✅

---

## 5. 验证清单（merge 后必做）

1. `git diff main -- <每个解决过的冲突文件>` → 只应剩 R1 路径修正 + R3 本地特性
2. 全树 `grep -rn "openviking\.service\.task_work_index"` → 必须为 **0**
3. `git diff --stat main` → 与预期的 fork delta 规模一致
4. `python -c "import openviking"` → 通过（验证 `.so` + 无 ImportError）
5. 跑受影响测试：`tests/storage/test_queue_manager.py`、`tests/storage/test_semantic_executor_*.py`、`bot/tests/test_cron_config.py`
6. 上游既有失败基线对比（勿当回归）：`tests/parse/test_feishu_unified_import.py::test_downloaded_file_routes_by_extension_but_normalized_markdown_does_not`

---

## 6. 冲突之外的注意事项

- **merge 纯净性约定**：pi-lens autofix 会在 merge 期间反复重写工作区文件。冲突解决后、merge commit 前必须 `git restore` 回 index 状态，保证 merge 只含真实合并内容。
- **上游类型债不修**：`bot/` 全树 1787 个既有 mypy 错误不在任何门禁内，report-only（见 `plans/upstream-type-debt-20260917.md`）。

---

## 7. merge 后验证结果（2026-09-21 实测）

**merge commit**: `280418b54`（parents `119c1945a` + `172c10507`）

### 环境一致性
| 项 | 结果 |
|---|---|
| 版本 | `0.4.21.dev124` → **`0.4.22.dev184`** |
| `import openviking` | OK |
| 9 个关键模块导入 | 全 OK（含 R1 修正的 `storage.queuefs.task_work_index`） |
| 3 个旧模块移除 | OK（`service.task_work_index` / `semantic_dag` / `server.account_settings`） |
| 全树旧路径残留 | **0**（预测 11 处，冲突内修 2 + 冲突外修 9） |

### 4 类产物全量重建
| 产物 | 状态 |
|---|---|
| `openviking/bin/ov` (Rust CLI) | ✅ 重建 |
| `openviking/lib/ragfs_python.abi3.so` | ✅ 重建（maturin release, 7m06s） |
| `openviking/storage/vectordb/engine/_native.abi3.so` | ✅ 重建（上游未改 `src/`，复用 CMake 缓存正确） |
| `openviking/web_studio/dist` | ⚠️ **需手动重建** —— `make build` 的 `build-studio` 只要 `dist/index.html` 存在就跳过，而上游本批改了 45 个 web-studio 文件。已手动 `npm ci && npm run build -- --base=/studio/` + 复制 |

### 测试回归对比（`tests/{storage,service,utils,unit}`，对照纯净 upstream/main worktree）
| | 合并态 | 上游基线 |
|---|---|---|
| passed | **3136** | 3035 |
| failed | **55** | 59 |
| errors | **1** | 85 |

- **合并态失败但基线通过 = 0 个 → 零回归**
- 基线失败但合并态通过 = 88 个（fork 修掉了上游 mock_agfs/account_settings 问题）
- 两处上游既有失败已在基线复现：`tests/storage/test_viking_fs_grep.py` 2 项

### 本轮发现并修复的自引入回归（重要教训）
冲突解决时用「锚点行 + 冲突块」做替换，**锚点行只写在 oldText 里、忘了写进 newText**，导致静默丢行：

1. `openviking/storage/queuefs/semantic_processor.py` — 丢了 `from openviking.storage.queuefs.semantic_queue import is_semantic_msg_stale`（438/1053 行在用）→ SEMANTIC worker 运行期 NameError → `test_skill_shutdown_releases_lock_after_embedding_worker_exits` 超时失败
2. `openviking/utils/embedding_utils.py` — 丢了 `from openviking.storage.queuefs.embedding_msg_converter import EmbeddingMsgConverter`（461/522/722 行在用）

**两者都不是上游既有问题**（纯净 main 上对应测试通过），是本地解决引入的。

**后续同步必做**：冲突解决后执行「删除行审计」——
```bash
while read -r f; do git diff main -- "$f" | grep "^-" | grep -v "^---" | grep -v "task_work_index" | grep -v "^-\s*$"; done < <冲突文件清单>
```
任何输出都必须逐行确认是有意的 fork 改动。本次仅靠「AST 解析通过 + 模块可导入」无法发现（缺的是函数内引用，模块级不报错）。

---

## 8. 清理结果（2026-09-21）

**原则**：只清理本项目环境的产物；有价值的构建缓存保留。

### 已清理
| 项 | 体积 | 理由 |
|---|---|---|
| `target/debug/` | 1.5G | 陈旧：无任何可执行二进制（`target/debug/ov` 不存在），`make build` 走 `cargo build --release` 用 `target/release/` |
| `__pycache__` × 125 | — | 可再生 |
| `.pytest_cache` / `.ruff_cache` / `.vitest/` | — | 工具缓存，可再生 |
| `openviking.egg-info/` | 492K | `uv pip install` 会重建 |
| `build/lib.*/openviking/lib/ragfs_python.abi3.so` | — | setuptools staging 陈旧副本（正式产物在 `openviking/lib/`） |
| 临时 worktree `/tmp/ov-bisect`、`/tmp/ov-upstream-baseline` | — | 本次诊断创建 |
| `/tmp/ov-*.log`、`/tmp/merge-msg-12.txt` 等 | — | 本次临时文件 |

**仓库总计 7.9G → 6.4G**（释放 1.5G）

### 保留（附理由）
| 项 | 体积 | 保留理由 |
|---|---|---|
| `target/release/` | 1.7G | `openviking/bin/ov` 的来源（10,435,984 bytes 与 `target/release/ov` 一致）；删则下次 `make build` 多 ~7min Rust 重建 |
| `.venv/` | 1.3G | 运行环境本体 |
| `web-studio/node_modules/` | 630M | SPA 构建依赖；删则下次需 `npm ci` |
| `.mypy_cache/` | 281M | mypy 增量缓存 |
| `build/` | 42M | CMake 缓存；上游未改 `src/` 时命中缓存使 C++ 阶段秒过 |

### 未跟踪项处置
| 项 | 处置 |
|---|---|
| `plans/` | **保留** —— 同步决策与类型债记录 |
| `skills/ov-memory-session-investigator/` | **保留** —— OpenViking 记忆诊断 skill |
| `.pi-lens.json` | **保留** —— pi-lens 工具配置 |
| `viking_remember` | 保留待定 —— 950B JSON，疑似 `viking_remember` 误写文件（2026-09-08 会话笔记） |
| `.claude-flow/` | 保留待定 —— claude-flow MCP 的 session 状态（188K），非本项目产物 |

### 清理后完好性验证
- 4 类产物齐全（`ov` / `ragfs_python.abi3.so` / `_native.abi3.so` / `web_studio/dist`）
- `./openviking/bin/ov --version` → `openviking 0.4.22.dev184` ✅
- `import openviking` → `0.4.22.dev184` ✅
