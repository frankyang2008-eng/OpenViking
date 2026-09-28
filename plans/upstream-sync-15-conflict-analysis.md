# 第 15 次上游同步 — 冲突分析报告

**日期**: 2026-09-28
**merge-base / 上次落点**: `276dfffc8`（第 14 次同步的 upstream 目标，= 本次合并前的 main）
**上游目标**: `f708ecfff`（upstream/main）
**本地**: `ov-dev-opt` = `eee6a4da6`（merge 前，回滚 tag `pre-sync-20260928`）
**merge 提交**: `adda17e8e`（parents: `eee6a4da6` + `f708ecfff`）
**规模**: 上游 14 commits / 138 files / +7244 −556；双方都改过的文件 14 个 → git 报真实冲突 **3 个**

---

## 0. 本次的关键教训：真正的风险不在 git 报的冲突里

3 个文本冲突全部是机械性的（一行表达式 / import 行）。**会炸的是 git 看不见的东西**：
本地 `e665e41ac fix(queuefs): break storage<->service circular import at withbot startup`
把 `openviking/service/task_work_index.py` 搬到了 `openviking/storage/queuefs/task_work_index.py`。
上游本次新增的文件仍然 import 老路径 → 合并后**运行期 ImportError**，git 不报任何冲突。

**新增的验证步骤（以后每次同步都做）**：对合并后的树做全量 `openviking.*` import 存在性扫描。
脚本：AST 遍历所有 `.py`，把 `from openviking.X` / `import openviking.X` 解析成路径，
比对 `X.py` 或 `X/__init__.py` 是否存在。本次扫出 3 处（见 §2），其余 2 处命中
（`openviking.pyagfs.binding_client`、`openviking.models.vlm.backends`）是 namespace package
与 conftest 的已知兜底，非缺陷。

---

## 1. git 报出的 3 个冲突

### 1.1 `crates/ragfs/src/core/mountable.rs` — 取并集

| 侧 | 改动 |
|---|---|
| base | `.map_or(true, \|name\| !is_hidden_internal_name(name))` |
| ov-dev-opt | clippy autofix：`map_or(true, f)` → `is_none_or(f)`（纯风格） |
| upstream | 新增 `directories_only` 过滤：`&& (!directories_only \| e.info.is_dir)` |

**解析**：保留上游的谓词 + 保留本地的 clippy 风格。
`directories_only` 已在函数签名与 trait 调用链上贯通，`cargo test -p ragfs` 449 passed。

### 1.2 `tests/service/test_task_processing_time.py` — 拆开两侧各取所需

本地侧 import `storage.queuefs.task_work_index` + **过期的** `semantic_dag`；
上游侧 import `service.task_work_index`（本仓库不存在的路径）+ `semantic_executor` 新名字。

**解析**：路径取本地（模块真实位置），类名/模块名取上游
（`SemanticDagExecutor → SemanticTreeExecutor`、`SemanticNodeScheduler → SemanticTreeScheduler`，
上游 `336f2173b` 已 rename）。即：

```python
from openviking.storage.queuefs.semantic_executor import (
    SemanticTreeExecutor,
    SemanticTreeScheduler,
)
from openviking.storage.queuefs.task_work_index import TaskWorkIndex, bind_task_context
```

### 1.3 `tests/storage/test_semantic_processor_permanent_storage_error.py` — 同上

**解析**：`TaskWorkQueueMiddleware` 取上游新增的 import（文件体 L160 已经在用），
`TaskWorkIndex` 路径取本地。

---

## 2. git 不报、但合并后必然炸的语义漂移（本次已修）

| 文件 | 性质 | 处理 |
|---|---|---|
| `openviking/service/memory_compile.py` | 上游**新增**源文件，import `service.task_work_index` | 改为 `storage.queuefs.task_work_index` |
| `tests/session/memory/test_consolidation_compile.py` | 上游**新增**测试，同上 | 同上 |
| `openviking/session/train/components/rollout_executor.py` | **两边都有**的潜在缺陷：函数内 lazy import 老路径，没有测试覆盖到 | 一并修掉（1 行） |

---

## 3. 自动合并、需要人工确认语义的「双改文件」结论

全部通过：本地特性完整存活，上游改动是叠加而非替换。

| 文件 | 本地 | 上游 | 结论 |
|---|---|---|---|
| `openviking/service/core.py` | 进程级共享 rerank client + `ThreadPoolExecutor` 准入闸、close 时先排空再关 client | 新增 `_compile_service.configure_memory_runner(...)` | 并集。`_init_shared_rerank_runtime(config)` 放在 `_init_runtime_config_manager()` 之后，与上游共用同一 `config` 引用；`init_viking_fs(rerank_client=, rerank_executor=)` 签名匹配 |
| `openviking/service/reindex_executor.py` | — | 新增 namespace + `recursive=false` 拒绝 | 无关，直接接受 |
| `openviking/session/memory/tools.py` | — | `MemoryLsTool` 新增 `recursive=true`、目录带 `/`、超限截断 | 直接接受 |
| `examples/pi-coding-agent-extension/config.ts` / `README.md` | omp fork | `recallQueryFilters` / `recallExcludeUris` 两个新旋钮 | 并集 |
| `bot/vikingbot/cli/commands.py`、`crates/ov_cli/*`、`crates/ragfs/src/git/service.rs`、`sdk/typescript/*` | 本地改动 | 上游改动 | 无冲突，逐文件 diff 确认互不覆盖 |

`node examples/memory-plugin-shared/sync.mjs` 本次**无产出变化**（`git status` 干净）——
上游这批没动 `examples/memory-plugin-shared/lib`，与第 14 次不同。

---

## 4. 构建与验证（环境一致性 / rerank / 插件）

| 项 | 结果 |
|---|---|
| `make build`（.venv 激活后）| 通过。C++ abi3 + `openviking/lib/ragfs_python.abi3.so` + editable install |
| `ov` CLI | `0.4.22.dev339`。**注意**：`make build-cli` 产 51MB debug 且覆盖能力；setup.py `build_ext` 产的是 release `ov`，`cargo build --release` 后拷 `target/release/ov` 才是正规产物 |
| 独立端口起服务（1935，临时 workspace）| `health ok`，启动日志 0 error，`Shared rerank client initialized (provider=llm_score, concurrency=8)`。`/metrics` 中 `openviking_model_usage_available{model_type="rerank",valid="1"} 1.0` |
| rerank 真实调用 | `LlmScoreRerankClient`，Ark 1.18s 返回 `[1.0, 0.0, 0.0]`，排序正确 |
| Python 定向测试 | 1505 passed / 1 failed（见 §5） |
| `cargo test -p ragfs` | 449 passed, 0 failed |
| `cargo test`（Go SDK）| pass |
| TypeScript SDK | 57 tests + `tsc --noEmit` 干净 |
| node 插件测试（118 文件）| 除 §5 的 10 个既有失败外全绿 |
| openclaw vitest | 42 files / 719 tests passed |
| `bash -n` 6 个安装脚本 | 全 OK |
| hermes 插件测试 | **无法本地跑**：需要 checkout `NousResearch/hermes-agent@58d146e9`（CI-only） |

---

## 5. 同步前就存在的失败（非本次引入，已用 `pre-sync-20260928` worktree 复现）

| 失败 | 根因 | 修法 |
|---|---|---|
| `tests/session/memory/test_graph_view.py::test_render_graph_html_edge_details_include_source_and_target_uris` | `graph_view.py` 模板是 raw string + `.format()`，输出的是 JS 转义 `\n`（**运行期正确**）；测试期望的是真换行 | 测试侧把那 3 处 `\n` 写成 `\\n` 或把期望串改成 raw string |
| `examples/omp-openviking-extension/tests/config.test.mjs` 9 个 | `skillCatalogTokenBudget` 期望 1200（shared schema 默认值）实际 600；`takeover` 相关亦不过 | 未定位，需单独立项 |
| `examples/agy-memory-plugin/tests/agy-plugin.test.mjs::evaluateAgyUriGuard denies viking:// in run_command` | adapter 把 `{CommandLine: ...}` 直接交给 shared `evaluateUriGuard`，键名未被识别 | 未定位 |

另外：`tests/session/memory/test_patch_merge_context_provider.py` 3 个失败**不是缺陷**，
是 `~/.openviking/ov.conf` 里的 `output_language_override: "zh-CN"` 造成的环境效应；
去掉该键后 14 passed。

---

## 6. 清理

删：`build/`(61M)、`target/`(4.2G)、`.pytest_cache`、`sdk/typescript/node_modules`（本次 `npm ci` 产生的临时物）、全树 `__pycache__`/`*.pyc`。
保留：`.venv`、`openviking/lib/*.so`、`openviking/bin/ov`、`openviking/storage/vectordb/engine/*.so`、`openviking/web_studio/dist`、`openviking.egg-info`、`graphify-out`。
仓库体积 **9.1G → 4.7G**。

**不要跑 `make clean`** —— 它的 `CLEAN_DIRS` 会把 `openviking/bin/`、`openviking/lib/`、`web_studio/dist/` 这些已交付产物一起删掉。
