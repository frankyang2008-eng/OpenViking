# 第 14 次上游同步 — 冲突分析报告

**日期**: 2026-09-26
**merge-base**: `563313ebe`（第 13 次同步的落点，= 上次对齐的 upstream/main）
**上游目标**: `276dfffc8`（upstream/main，`fix(codex-plugin): exclude host startup context`）
**本地**: `ov-dev-opt` = `261411053`（merge 前，回滚 tag `pre-sync-20260926`）
**规模**: 上游 57 commits / 518 files / +29307 -7524
**重叠**: 双方都改过的文件 75 个 → 真实冲突 **15 个**

---

## 0. 解析规则（沿用第 12 次同步的 R1–R3）

- **R2 丢弃格式化**：本地大量"改动"是 pi-lens / prettier autofix 噪音（折行重排、表格 pipe 加空格、`arrowParens: always`）。
  仓库**无** `.prettierrc` / `.markdownlint` / `.yamllint`，CI 不跑这些检查 → 一律取上游。
- **R3 保留本地真特性**：shared rerank runtime、queuefs executor/超时、openclaw 多 harness 安装器、omp/agy/qoder/codebuddy 接入。
- **取上游**（本地实现被上游完整取代时）：`openviking/models/vlm/token_usage.py`。

---

## 1. 15 个冲突文件 — 逐个判定

### 被上游完整取代（1 个，取上游）

| 文件 | 本地改动 | 上游改动 | 解析 |
|---|---|---|---|
| `openviking/models/vlm/token_usage.py` | 给 `TokenUsageTracker` 加 `RLock` + 手写加锁（4 处，无 decorator） | 同一意图，实现更完整：`@_locked` decorator、`_add_usage()` 合并 `last_updated`、`merge()` 改为对 `to_dict()` 快照迭代（不再迭代活着 model map） | **取上游**。语义是超集，本地版本无独有信息 |

### 本地格式化噪音（5 个，取上游）

| 文件 | 性质 | 证据 |
|---|---|---|
| `docs/en/agent-integrations/17-dsh.md` | 表格分隔符/空行 | base↔本地 tip 去空白后 **token 相同** |
| `docs/zh/agent-integrations/17-dsh.md` | 同上 | 同上 |
| `docs/en/api/03-filesystem.md` | 删空行、表格 | 同上 |
| `examples/agent-hook-plugin/hosts/zcode-capture.mjs` | prettier 折行 | 去 `空白;,` 后 token 相同 |
| `examples/dsh-memory-plugin/index.mjs` | prettier `(session) =>` | 去 `空白;,` 后 token 相同（差异仅 arrow parens） |
| `examples/openclaw-plugin/tests/ut/identity-routing.test.ts` | prettier 折行 | 去 `空白;,` 后 token 相同。**且**上游把 sender 缺失从"抛错"改成"warn + 放宽"，本地那半边的 `toThrow` 断言会直接失败 → 必须取上游 |

### 双方各加功能 — 必须 union（7 个）

| 文件 | 本地 | 上游 | 解析 |
|---|---|---|---|
| `openviking/service/core.py` | `_init_shared_rerank_runtime(config)`、进程级 rerank client + executor、close 时先排空 executor 再关 client | `_init_runtime_config_manager()` + `_vlm_resolver` 校验（删除进程级 embedder 校验，embedding 改为按账号懒建）；`debug_service` 依赖新增 `embedding_provider` / `vlm_resolver`；close 时关 `vlm_resolver` | 三处均取并集；**embedder 校验按上游删除**（`self._embedder` 在上游已不再赋值，保留会 100% 抛错）。`_init_shared_rerank_runtime` 放在 `_init_runtime_config_manager()` 之后，与上游用同一个 `config` 引用传给 `init_viking_fs(rerank_config=...)`，保持一致 |
| `openviking/service/debug_service.py` | `rerank_client` 参数穿透 `DebugService → ObserverService`；`models` 状态复用 service 级共享 client（避免每次 health 检查泄漏 HTTP+worker pool） | 新增 `embedding_provider` / `vlm_resolver` 参数与账号级观测、`format` 参数、scope 字段 | 构造/`set_dependencies` 同时接纳 3 个参数（位置顺序 `rerank_client, embedding_provider, vlm_resolver`）；rerank 改为「共享 client 优先，否则回退自建」；**本地那段 `if self._config.embedding: embedding_instance = ...get_embedder()` 丢弃**（上游已用 `_get_token_tracker()` namespace 取代，保留会把被删的进程级 embedder 拉回来） |
| `openviking/storage/queuefs/queue_manager.py` | `ThreadPoolExecutor("qfs-agfs")` 共享执行器 + `_agfs_call_timeout=30` | `_vlm_resolver: Optional["VLMResolver"]` + `TYPE_CHECKING` 导入 | 纯并集 |
| `openviking/session/memory/patch_merge_context_provider.py` | `consumer="patch_merge"`、`Sequence` 导入 | `TYPE_CHECKING` | 纯并集 |
| `examples/pi-coding-agent-extension/index.ts` | omp fork 的 `getBranch()` 回退 + `WeakMap` 身份映射（自动合并段，未冲突） | `takeover.commitAndAdvance(() => …getBranch())`、注释 | 冲突仅格式化 → 取上游；本地 omp 回退段已自动合并保留 |
| `examples/openclaw-plugin/commands/setup.ts` | 本地安装器逻辑（自动合并） | 新增 `normalizePeerRoleInput()`：**setup 输入不再接受 `person`**，仅老配置里兼容；错误文案与 `--peer-role` 描述同步 | 取上游语义。本地半边缺 `normalizePeerRoleInput` 定义却在别处调用 → 若取本地侧会 ReferenceError |
| `examples/openclaw-plugin/setup-helper/install.js` | 本地安装器逻辑（自动合并，+1225） | 同上 person→sender | 5 处冲突全部取上游语义；本地 `--peer-role` 调用点统一改 `normalizePeerRoleInput` |

### 双方各加 harness（2 个，union + 本地一处真 bug 修复）

| 文件 | 本地新增 harness | 上游新增 harness | 解析 |
|---|---|---|---|
| `examples/memory-plugin-shared/install.sh` | `qoder`、`codebuddy`、`omp`、`agy`（+665 行） | `kimicode`（Kimi Code 原生 bundle，+51 处引用） | 20 处冲突全部 union，统一顺序 `zcode → kimicode → qoder → codebuddy → omp → agy` |
| `examples/openclaw-plugin/setup-helper/install.js` | 同上安装器 | 同上 | — |

**install.sh 顺带修掉一个本地 bug（非上游带来）**：`tui_item_at()` 里 `zcode` 与 `qoder` 之间缺 `i=$((i + 1))`，
导致 qoder 行永远取不到自己的索引（TUI 里 qoder 不可点、`add` 行出现两次）。
TUI 可选项数因此 11 → 12，与 `tui_selectable_count()` 的常量一致。

**TUI 自检**（`tui_item_at` 条目数 vs `tui_selectable_count` 常量）：
```
fixed entries = opencode, pi, dsh, cursor, trae, trae-cn, zcode, kimicode, qoder, codebuddy, omp, agy  (12)
tui_selectable_count 常量 = 12   ✅
index 自增次数 = 11 + 2(bins 循环)  ✅
```

---

## 2. 校验

| 检查 | 结果 |
|---|---|
| `git grep '^<<<<<<<'` 全仓 | 0 |
| `bash -n install.sh` / `node --check install.js` | 通过 |
| `python -c "ast.parse(...)"`（改动的 py 文件） | 通过 |
| 取上游的 6 个文件「本地是否真没丢东西」 | 去空白 token 比对：格式噪音确认（见上表证据列） |
| 本地特性存活（`git diff --stat main -- <file>`） | core.py +49、debug_service.py 14、queue_manager.py 125、install.sh +682、install.js 1588、setup.ts 530、pi extension 95 — 均在 |

## 3. Not touched（上游自带问题，report-only）

本次 merge 未修改任何"与 `upstream/main` 逐字节相同"的文件。若后续 pi-lens / mypy 报出告警落在
`git diff --quiet main:<path> :<path>` 干净的文件上，按第 12/13 次同步的书面约定继续 report-only
（详见 `plans/upstream-type-debt-20260917.md`），改上游文件会让 fork 在每次同步重复冲突。
