# Upstream type debt observed during 11th sync (2026-09-17)

Parked here deliberately. **Not fixes.** These files were NOT edited by the fork.

## Verification (all three confirm upstream ownership)

| Check | Result |
|---|---|
| `git diff --quiet main -- <file>` | identical to `main` |
| `git rev-list --count 2b351a9c5..HEAD -- <file>` | `0` fork commits |
| Introduced by | upstream `ca777889c` — `feat(studio): add VikingBot conversations and Feishu onboarding (#5109)` |
| In a type gate? | **No.** `[tool.mypy]` has no `files=`; CI `mypy` (`pr.yml:262`) is LangChain-only with its own config; `.pre-commit-config.yaml` has no mypy/ruff hook; AGENTS.md scope is `mypy openviking` |

`bot/` is outside every gate, so these errors do not gate this repo's CI, pre-commit, or the fork's workflow.

## The findings (samples, not the full set)

### `bot/tests/test_studio_onboarding.py`
- L372 `[var-annotated]` need type annotation for `value`
- L466, L467 `Object of type "None" is not subscriptable`

### `bot/vikingbot/agent/loop.py`
- L344 `Expression of type "None" cannot be assigned to parameter of type "Config"`
- L561 `send_callback` type mismatch — passes `(msg) -> CoroutineType[...]` where `((OutboundMessage) -> None) | None` is expected

## Verdict: report-only, do not fix

Measured with `.venv/bin/python -m mypy --config-file pyproject.toml` over four bot
files:

**1787 errors in 258 files.**

`bot/` was never type-clean, so there is nothing here to restore. Fixing a
handful of the findings is non-convergent — 5 of 1787 still leaves 1782 — and
nothing in the repo runs the checker over this tree.

Two findings are **behavioural, not cosmetic**: L344 (`Config = None` default)
and L561 (async `publish_outbound` passed into a sync-typed slot). A correct fix
needs upstream's intended design; a wrong one silently changes bot runtime
behaviour or drops outbound messages. Silencing a checker nobody runs is not
worth that risk.

Editing upstream-owned files the fork has never touched would also add a
permanent conflict surface on every future sync, for zero fork benefit.

**Attempted and reverted (2026-09-17).** Four annotation-only edits (`loop.py`,
`tools/factory.py`, `studio/onboarding.py`, the test file) were applied, measured
against the 1787-error baseline, and fully reverted. All four are byte-identical
to `main` again.

## If this ever becomes worth fixing

Fix it **upstream**, as one scoped change with the bot package's own type story
decided first — not in the fork, and not inside or beside a merge commit.

## 追加（2026-09-21，第 12 次同步）

`examples/openclaw-plugin/auto-recall.ts:261` `runtimeFlag(runtimeContext: unknown, key: string): unknown`
—— pi-lens `no-unknown-returns` 报 🔴。已实证该文件与上游 `main` 逐字节相同（`git diff --stat main -- <file>` 为空），
属上游自带债。按约定 report-only，不在 merge commit 中修改。

`benchmark/locomo/openclaw/import_to_ov.py` —— pyright 报 33 项（含 `reportMissingImports`）。
已实证：与上游 `main` 逐字节相同、本地零改动、第 12 次 merge 未触及该文件。上游既有债，report-only。

`openviking/storage/queuefs/semantic_executor.py` —— pyright 报 6 项（L179 `"SemanticProcessor"` 未定义、
L360/L853 `.rstrip` on possibly-None、L1582/1602/1606 `enqueued_levels` possibly unbound）。
已实证：本文件由上游 `semantic_dag.py` rename 而来（上游 `336f2173b`），本次同步中我方唯一改动是 import 路径 1 行，
其余逐字节等于上游 `main`（`git diff main -- <file>` 只显示该 1 行）。L1645 的 `if False:  # pragma: no cover - for type checkers only`
是上游自己的类型检查守卫写法（未用 `TYPE_CHECKING`），故 pyright 无法解析前向引用。上游既有债，report-only。

## 上游测试引用已重命名模块（2026-09-21 第 12 次同步实证）

上游 `336f2173b`（#5175）把 `openviking/storage/queuefs/semantic_dag.py` 重命名为 `semantic_executor.py`，
但漏改两个测试文件的 import，导致 pytest 收集期即报错：

- `tests/service/test_task_processing_time.py:13` — `from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor, SemanticNodeScheduler`（另 L51/L54 的 monkeypatch 字符串同样失效）
- `tests/storage/test_semantic_processor_permanent_storage_error.py:17` — `from openviking.storage.queuefs.semantic_dag import DagStats`

实证：在**纯净 upstream/main worktree**（`/tmp/ov-upstream-baseline`，HEAD=172c10507）上跑同两文件，
得到完全相同的 `ModuleNotFoundError: No module named 'openviking.storage.queuefs.semantic_dag'`。
合并版这两行与上游 `main` 逐字节相同（`git diff main -- <file>` 为空）→ 上游既有缺陷，非 merge 回归。

按 merge 纯净性约定 report-only，不在 merge 中改上游测试文件（上游会自行修复重命名遗漏）。

## 上游既有失败：tests/storage/test_viking_fs_grep.py 2 项（2026-09-21 第 12 次同步实证）

- `test_grep_with_agfs_denies_acl_restricted_content_without_grant`
- `test_grep_with_agfs_allows_acl_granted_content`

实证：纯净 upstream/main worktree 上同样 2 failed → 上游既有缺陷（来自 #5133 ACL-aware grep batch check），非 merge 回归。
fork 对该文件的改动为纯格式化（Prettier/ruff 折行），无功能变化。

## 追加（2026-09-23，第 13 次同步）

`.github/workflows/pr.yml` —— yamllint 报 15 项（`[brackets]` 括号内空格、`[line-length]` 91/93/92/111/121 > 80、
`[trailing-spaces]` L154/157/160）。已实证属上游自带债，按约定 report-only：

- `git diff --stat 172c10507..ov-dev-opt -- .github/workflows/pr.yml` **为空** → 本地从未改过该文件
- `git diff --stat 172c10507..main -- .github/workflows/pr.yml` = `1 +` → 上游本次新增 1 行
- `git diff --quiet main:.github/workflows/pr.yml :.github/workflows/pr.yml` 干净 → 与上游 `main` 逐字节相同
- 本仓库**无 `.yamllint` 配置**，pre-commit 无 yamllint；且 `pyproject.toml` 的 ruff 配置
  `line-length = 100` 并把 `E501`（line too long）列入 `ignore` → 本仓库显式关闭行长规则。
  故「line too long (91 > 80)」与本仓库自身规则直接冲突，80 列是 yamllint 的默认值而非本项目约定。

`benchmark/aml/eval/evaluate.py` —— 3 项（L538 identity operators with literals、L108 call without try/except、
L269 `compile()` on dynamic input）。已实证属上游自带债，按约定 report-only：

- `git diff --stat 172c10507..ov-dev-opt -- benchmark/aml/eval/evaluate.py` **为空** → 本地从未改过该文件
- `git diff --stat 172c10507..main -- benchmark/aml/eval/evaluate.py` = `597 +` → 上游本次整文件新增（#5276 AML adapter）
- `git diff --quiet main:benchmark/aml/eval/evaluate.py :benchmark/aml/eval/evaluate.py` 干净 → 与上游 `main` 逐字节相同
- L269 的 `compile()` 是该 benchmark 评测脚本的设计（编译模型生成代码以评测），非本 merge 引入

结论：两者均 report-only。在此处修改上游文件会让 fork 偏离上游基线，并在每次同步重复冲突。
正确路径是提上游 PR（scoped change），而非在 merge 中或 merge 旁修改。
