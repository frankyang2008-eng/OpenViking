# CodeBuddy 记忆系统优化 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动核心召回/捕获算法的前提下，为 CodeBuddy 端的 OpenViking 记忆插件补齐「可观测、源码同步、参数调优」三件事，使自动召回可见、源码改动可一键同步、token/阈值参数有测量依据。

**Architecture:** 一套插件源码（`examples/claude-code-memory-plugin/`）+ 三份 manifest 共用 `hooks/`、`scripts/`、`servers/`。本计划只动两处：① 运行时状态落盘与状态展示（`scripts/auto-recall.mjs`、`scripts/ov-status.mjs`）；② 共享安装器（`examples/memory-plugin-shared/install.sh`）新增 `--sync` / `--verify` 两个 dev-loop 快路径。配置调优不改代码，直接写 `ov.conf` 的 `claude_code.*`（每次 hook 触发时 `config.mjs` 现读，无需同步）。

**Tech Stack:** Node 18+ ESM 脚本（`.mjs`）、Bash 3.2+ 安装器、OpenViking HTTP `/health` 与检索端点、`jq`-free 的 `node -e` JSON 读取。

**关联设计文档:** `docs/design/codebuddy-memory-system-design.md`（决策 D1–D4）。

## Global Constraints

- **语言：** 全程用中文沟通；代码、命令、路径、标识符保留原文。
- **D1 配置共用：** 所有调参写入 `ov.conf` 的 `claude_code.*` 节，**不新增** `codebuddy.*` 节。配置解析链 `env(OPENVIKING_*) > ovcli.conf > ov.conf(claude_code.*) > 默认`。
- **D4 不动召回逻辑：** 可观测只在「状态落盘」加字段，**不改变**注入给模型的内容、不改召回/排序/阈值代码路径。
- **配置免同步：** `config.mjs` 每次 hook 触发时现读 `ov.conf`，故调参（Task 4）**不需要** `--sync`；只有改 `scripts/`、`hooks/`、`servers/`、`*.json` 源码才需要。
- **Bash 3.2+ 兼容：** `install.sh` 以 macOS 自带 `/bin/bash`（3.2）为目标。禁用关联数组、`mapfile`、`;&`/`;;&`、`${var^^}` 等 bash-4 特性。
- **`set -Eeuo pipefail`（install.sh:44）：** `set -u` 开启，引用未定义变量即中止。新增引用的变量必须在变量初始化块先赋空串；快路径里不调用 `resolve_source_mode`（它引用快路径时尚未赋值的 `SELECTED_HARNESSES` 等）。
- **三端共享：** `scripts/` 被 claude/codebuddy/qoder 三端共用，`install.sh` 被全部 harness 共用。改动一律**纯增量**（加字段、加 flag、加函数），不改既有安装/召回流程。
- **License 头：** 这两个 `.mjs` 与 `install.sh` 均无 AGPL 头，保持现状，不为本计划新增头。
- **分支：** 提交到当前本地优化分支 `ov-dev-opt`（main 为 upstream 镜像，ff-only，不直接提）。
- **gitignore：** 设计/计划文档提交到 `docs/design/`（`docs/superpowers/` 被 gitignore）。
- **commit message：** 结尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## File Structure

| 动作 | 路径 | 职责 |
|------|------|------|
| Modify | `examples/claude-code-memory-plugin/scripts/auto-recall.mjs` | endpoint + 多源两条 ok 路径的 `writeRecallState` 增加 `items` 字段；endpoint 路径 `count`/`top_score` 记真实值（B2） |
| Modify | `examples/claude-code-memory-plugin/scripts/ov-status.mjs` | 渲染 `last-recall.json` 的 `items` 列表（B2） |
| Modify | `examples/memory-plugin-shared/install.sh` | 新增 `--sync`（C1）、`--verify`（C3+B3）、`verify_codebuddy()`、快路径、usage、变量初始化、更新 2785 过期提示 |
| Create | `docs/design/codebuddy-memory-tuning.md` | 测量记录 + 推荐阈值（A，Task 4 产出） |
| Modify | `~/.openviking/ov.conf`（仓库外，用户运行时配置） | 应用调参到 `claude_code.*`（A，Task 4，无 commit） |

任务顺序与依赖：Task 1（B2 可观测）→ Task 2（C1 sync）→ Task 3（C3+B3 verify，依赖 Task 2 的 `--sync`）→ Task 4（A 调参，独立，可与 2/3 并行但需 Task 1 的 `items` 辅助观察）→ Task 5（端到端 + 回归 + 手动 B1）。

---

### Task 1: 召回可观测 — `last-recall.json` 记录命中项 + `/ov` 展示（B2）

**Files:**
- Modify: `examples/claude-code-memory-plugin/scripts/auto-recall.mjs:450-459`
- Modify: `examples/claude-code-memory-plugin/scripts/ov-status.mjs:96-106`

**Interfaces:**
- Consumes: 现有 `writeRecallState(extra)`（auto-recall.mjs:334）、`picked`（433，`{ _sourceType, uri, score }`）、`clampScore`（47）、`readJsonState`（ov-status.mjs）。
- Produces: `~/.openviking/state/last-recall.json` 在两条 ok 路径都多一个**可选**字段 `items: [{ type: string, uri: string, score: number(0..1, 4 位小数) }]`——endpoint 路径从 `res.result.entries` 取（服务端 `RecallEntry` 含 `{uri,score,type,mode,rank}`），多源回退路径从 `picked` 取；endpoint 路径同时把 `count`/`top_score` 从硬编码 `1`/`0` 改为真实值。`/ov`（ov-status.mjs）逐行渲染。Task 4 可读 `items` 观察命中分布。**不改** `approve(...)` 的注入内容（D4）。

**前置条件：** OpenViking server 在跑（`curl -fsS -m 5 http://127.0.0.1:1933/health` 通），否则 auto-recall 走 `reason:"offline"` 分支不产生 `items`，测试会因错误原因失败。

- [ ] **Step 1: 写失败检查（red）**

先确认当前 `last-recall.json` 没有 `items` 字段。跑一次真实召回（直接喂 stdin，无需 CodeBuddy），再断言无 `items`：

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking/examples/claude-code-memory-plugin
curl -fsS -m 5 http://127.0.0.1:1933/health >/dev/null || { echo "SERVER DOWN — 先启动 openviking-server"; exit 1; }
printf '%s' '{"prompt":"我喜欢什么编程语言","session_id":"plan-t1","cwd":"'"$PWD"'"}' | node scripts/auto-recall.mjs >/dev/null 2>&1
node -e 'const j=require(require("node:os").homedir()+"/.openviking/state/last-recall.json"); if(!Array.isArray(j.items)){console.error("NO items field (expected before fix)");process.exit(1)} console.log("items:",j.items.length)'
```

Expected: 输出 `NO items field (expected before fix)`，退出码 1（红）。

- [ ] **Step 2: 改 auto-recall.mjs（多源 ok 路径加 `items`）**

在 auto-recall.mjs 的多源召回成功分支（450-459）的 `top_score: topScore,` 与 `cc_session_id: sessionId,` 之间插入 `items`。用 Edit，old_string 为：

```js
    tokens_budget: cfg.recallTokenBudget,
    top_score: topScore,
    cc_session_id: sessionId,
    reason: "ok",
  });
  approve(built?.block);
```

new_string 为：

```js
    tokens_budget: cfg.recallTokenBudget,
    top_score: topScore,
    items: picked.map((it) => ({
      type: it._sourceType,
      uri: it.uri,
      score: Number(clampScore(it.score).toFixed(4)),
    })),
    cc_session_id: sessionId,
    reason: "ok",
  });
  approve(built?.block);
```

注意：此 old_string 在该文件**唯一**（endpoint 路径的 `writeRecallState` 没有 `top_score: topScore` 且字段不同，不会误匹配）。endpoint 路径的 `items` 由下一步 Step 2b 补上。

- [ ] **Step 2b: 改 auto-recall.mjs（endpoint 路径也记 `items` + 真实 `count`/`top_score`）**

已实测 `/api/v1/search/recall` 响应 `result` 除 `rendered` 外还有 `entries[]`（服务端 `openviking/retrieve/type_quota_recall.py` 的 `RecallResult.to_dict()`，每条 `RecallEntry` 含 `{uri,score,type,mode,rank}`；`rendered` 即由这些 entry 拼接，故 `rendered` 非空 ⇒ `entries` 非空）。endpoint 路径是生产默认路径（直连 server 即命中，无需代理），此步让 B2 在真实使用中真正可见。分两处 Edit。

Edit 1 — `recallViaTypeQuotaEndpoint` 改为返回 `{ block, entries }`（不再丢 `entries`）。old_string：

```js
  const rendered = String(res.result?.rendered || "").trim();
  if (!rendered) return "";
  return [
    "<openviking-context>",
    "Relevant memory from OpenViking. Use the recall/read MCP tools to expand URIs.",
    rendered,
    "</openviking-context>",
  ].join("\n");
}
```

new_string：

```js
  const rendered = String(res.result?.rendered || "").trim();
  if (!rendered) return { block: "", entries: [] };
  const entries = Array.isArray(res.result?.entries) ? res.result.entries : [];
  const block = [
    "<openviking-context>",
    "Relevant memory from OpenViking. Use the recall/read MCP tools to expand URIs.",
    rendered,
    "</openviking-context>",
  ].join("\n");
  return { block, entries };
}
```

Edit 2 — endpoint 调用方记 `items` 与真实 `count`/`top_score`。old_string：

```js
  const endpointBlock = await recallViaTypeQuotaEndpoint(userPrompt, effectivePeer.peerId);
  if (endpointBlock !== null) {
    if (!endpointBlock) {
      log("skip", { reason: "recall_endpoint_no_results" });
      writeRecallState({ count: 0, reason: "no_results", cc_session_id: sessionId });
      approve();
      return;
    }
    writeRecallState({
      count: 1,
      content_items: 1,
      hint_items: 0,
      tokens_used: estimateTokens(endpointBlock),
      tokens_budget: cfg.recallTokenBudget,
      top_score: 0,
      cc_session_id: sessionId,
      reason: "ok",
    });
    approve(endpointBlock);
    return;
  }
```

new_string：

```js
  const endpointResult = await recallViaTypeQuotaEndpoint(userPrompt, effectivePeer.peerId);
  if (endpointResult !== null) {
    if (!endpointResult.block) {
      log("skip", { reason: "recall_endpoint_no_results" });
      writeRecallState({ count: 0, reason: "no_results", cc_session_id: sessionId });
      approve();
      return;
    }
    const endpointItems = endpointResult.entries.map((e) => ({
      type: e.type,
      uri: e.uri,
      score: Number(clampScore(e.score).toFixed(4)),
    }));
    const hintItems = endpointResult.entries.filter((e) => e.mode === "uri").length;
    writeRecallState({
      count: endpointItems.length,
      content_items: endpointItems.length - hintItems,
      hint_items: hintItems,
      tokens_used: estimateTokens(endpointResult.block),
      tokens_budget: cfg.recallTokenBudget,
      top_score: endpointResult.entries.reduce((m, e) => Math.max(m, clampScore(e.score)), 0),
      items: endpointItems,
      cc_session_id: sessionId,
      reason: "ok",
    });
    approve(endpointResult.block);
    return;
  }
```

D4：`approve(endpointResult.block)` 的注入内容与之前完全一致，只多记了状态字段。`count` 语义从「1 个聚合块」改为「命中条目数」，与多源路径 `count: picked.length` 对齐，`/ov` 的「N items」与下方列表条数一致。

- [ ] **Step 3: 改 ov-status.mjs（渲染 `items`）**

在 ov-status.mjs 的 `if (recall)` 块内、`console.log(...)` 之后、`} else {` 之前插入逐行渲染。用 Edit，old_string 为：

```js
      `top ${top}, ${used}/${budget} tokens (${recall.reason || "ok"})`,
    );
  } else {
    console.log("Last auto-recall: (none yet)");
  }
```

new_string 为：

```js
      `top ${top}, ${used}/${budget} tokens (${recall.reason || "ok"})`,
    );
    if (Array.isArray(recall.items) && recall.items.length > 0) {
      for (const it of recall.items) {
        const pct = typeof it.score === "number" ? Math.round(it.score * 100) : "?";
        console.log(`    - [${it.type} ${pct}%] ${it.uri}`);
      }
    }
  } else {
    console.log("Last auto-recall: (none yet)");
  }
```

`Array.isArray` 守卫保证旧版 `last-recall.json`（无 `items`）不报错（三端向后兼容）。

- [ ] **Step 4: 语法自检 + 跑测试转绿（green）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking/examples/claude-code-memory-plugin
node --check scripts/auto-recall.mjs && node --check scripts/ov-status.mjs && echo "SYNTAX OK"
printf '%s' '{"prompt":"我喜欢什么编程语言","session_id":"plan-t1","cwd":"'"$PWD"'"}' | node scripts/auto-recall.mjs >/dev/null 2>&1
node -e 'const j=require(require("node:os").homedir()+"/.openviking/state/last-recall.json"); if(!Array.isArray(j.items)){console.error("STILL NO items");process.exit(1)} console.log("items:",j.items.length); j.items.forEach(i=>console.log("  -",i.type,i.score,i.uri))'
node scripts/ov-status.mjs
```

Expected: `SYNTAX OK`；`items: <N>`（N≥1，若该 query 有命中）；逐行 `  - <type> <score> <uri>`；`ov-status.mjs` 输出末尾 `Last auto-recall:` 行下出现 `    - [<type> <pct>%] <uri>` 列表。直连 server（默认走 endpoint 路径）即可看到 `items`，无需代理；此时 `count` 等于命中数、`top_score` 为真实最高分。

- [ ] **Step 5: Commit**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
git add examples/claude-code-memory-plugin/scripts/auto-recall.mjs examples/claude-code-memory-plugin/scripts/ov-status.mjs
git commit -m "$(cat <<'EOF'
feat(memory-plugin): record recalled items in last-recall.json and show in /ov

可观测增强（B2）：多源召回 ok 路径把逐条命中项 {type,uri,score} 落盘到
last-recall.json，/ov 逐行渲染。仅加状态字段，不改注入内容（设计 D4）。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 源码一键同步 — `install.sh --sync codebuddy`（C1）

**Files:**
- Modify: `examples/memory-plugin-shared/install.sh`（变量初始化 109-111；usage 175-176；arg-parse 194-195；主流程 3066-3067；hint 2785）

**Interfaces:**
- Consumes: 现有 `install_codebuddy`（2765）、`resolve_self_checkout`（1158/3066 调用）、`plugin_dir_on_disk`（1226）、`ensure_checkout`（1191）。
- Produces: 新 flag `--sync codebuddy`。dev 模式下跳过向导/选 harness/连接配置/校验，直接重跑 `install_codebuddy` 后退出。被 Task 3 的 `--verify` 提示语引用；被 Task 4 之外的源码改动流程使用。

**安全约束回顾：** `--sync` 在 arg-parse 里同时设 `YES=1`，使 `INTERACTIVE=0`（204-208 条件 `[ "$YES" -ne 1 ]` 不满足），从而 `select_language` 不提示（304-321 仅 INTERACTIVE=1 才 tui_menu）。快路径在 `resolve_self_checkout`（3066，已设 `CHECKOUT_DIR`）之后、`select_harnesses`（3067）之前插入，**不调用** `resolve_source_mode`（避开 set -u 引用未初始化变量）。

- [ ] **Step 1: 写失败检查（red）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
bash examples/memory-plugin-shared/install.sh --sync codebuddy; echo "exit=$?"
```

Expected: `Unknown argument: --sync`，打印 usage，`exit=2`（红）。

- [ ] **Step 2: 变量初始化块加两个变量**

Edit，old_string（install.sh 109-111）：

```bash
YES=0
UNINSTALL=0
NODE_BIN=""
```

new_string：

```bash
YES=0
UNINSTALL=0
SYNC_TARGET=""
VERIFY_TARGET=""
NODE_BIN=""
```

- [ ] **Step 3: usage 加两行**

Edit，old_string（175-176）：

```bash
  --uninstall        Remove Cursor/TRAE/Qoder/CodeBuddy OpenViking integration files and config.
  --yes, -y          Use defaults for prompts when possible.
```

new_string：

```bash
  --uninstall        Remove Cursor/TRAE/Qoder/CodeBuddy OpenViking integration files and config.
  --sync codebuddy   Dev loop: re-materialize + reinstall the CodeBuddy plugin from this checkout, skip wizard.
  --verify codebuddy Check CodeBuddy plugin health + source/installed sync; exit 0 if OK.
  --yes, -y          Use defaults for prompts when possible.
```

- [ ] **Step 4: arg-parse 加两个 case**

Edit，old_string（194-195）：

```bash
    --uninstall) UNINSTALL=1; shift ;;
    --yes|-y) YES=1; shift ;;
```

new_string：

```bash
    --uninstall) UNINSTALL=1; shift ;;
    --sync) SYNC_TARGET="${2:-}"; YES=1; shift 2 ;;
    --verify) VERIFY_TARGET="${2:-}"; YES=1; shift 2 ;;
    --yes|-y) YES=1; shift ;;
```

- [ ] **Step 5: 主流程插快路径 dispatch**

Edit，old_string（3066-3067，列 0 无缩进）：

```bash
resolve_self_checkout
select_harnesses
```

new_string：

```bash
resolve_self_checkout

# Fast paths: `--sync codebuddy` / `--verify codebuddy` run without the wizard,
# harness selection, connection config, or install validation. Dev-loop only —
# they rely on resolve_self_checkout having located the plugin sources in this
# checkout. `--sync`/`--verify` set YES=1 at parse time so nothing prompts.
if [ -n "$SYNC_TARGET" ]; then
  [ "$SYNC_TARGET" = "codebuddy" ] || { err "Unsupported --sync target: $SYNC_TARGET (expected: codebuddy)"; exit 2; }
  install_codebuddy
  exit 0
fi
if [ -n "$VERIFY_TARGET" ]; then
  [ "$VERIFY_TARGET" = "codebuddy" ] || { err "Unsupported --verify target: $VERIFY_TARGET (expected: codebuddy)"; exit 2; }
  if verify_codebuddy; then exit 0; else exit 1; fi
fi

select_harnesses
```

说明：`verify_codebuddy` 的函数体在 Task 3 才加入；本步先放 dispatch（`--verify` 暂时会报 `verify_codebuddy: command not found`）。若想让 Task 2 独立可提交，可先在 Task 3 Step 2 一起落地函数体——两 Task 改同一文件，**建议按 Task 2 → Task 3 连续做完再分别 commit**，或合并为一次提交。这里按计划顺序：Task 2 只提交 `--sync` 可用（dispatch 里 `--verify` 分支随 Task 3 生效）。若严格要求 Task 2 单独可运行，把 dispatch 的 `--verify` 分支留到 Task 3 再加。

> **执行提示（给实现者）：** 为让 Task 2 单独成立，本步 dispatch **先只加 `--sync` 分支**，`--verify` 分支在 Task 3 Step 3 补。即本步 new_string 用下面精简版：
>
> ```bash
> resolve_self_checkout
>
> # Fast path: `--sync codebuddy` re-materializes + reinstalls the plugin from
> # this checkout, skipping the wizard. `--sync` sets YES=1 so nothing prompts.
> if [ -n "$SYNC_TARGET" ]; then
>   [ "$SYNC_TARGET" = "codebuddy" ] || { err "Unsupported --sync target: $SYNC_TARGET (expected: codebuddy)"; exit 2; }
>   install_codebuddy
>   exit 0
> fi
>
> select_harnesses
> ```

- [ ] **Step 6: 更新 2785 过期提示**

Edit，old_string：

```bash
  info "$(t 'CodeBuddy plugin installed and enabled:' 'CodeBuddy 插件已安装并启用：') $CODEBUDDY_PLUGIN_ID ($(t 'cache snapshot; rerun install.sh after changing the source' 'cache 快照，改动源码后需重跑 install.sh'))"
```

new_string：

```bash
  info "$(t 'CodeBuddy plugin installed and enabled:' 'CodeBuddy 插件已安装并启用：') $CODEBUDDY_PLUGIN_ID ($(t 'cache snapshot; after editing sources run: bash install.sh --sync codebuddy' 'cache 快照，改动源码后执行：bash install.sh --sync codebuddy'))"
```

- [ ] **Step 7: 跑测试转绿（green）— 标记传播**

证明 `--sync` 真把源码内容同步到 marketplace（双向：加标记能传、清标记也能传）。**注意：这会重装你本机的 CodeBuddy 插件（幂等，dev-loop 可接受）。**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
MKT=~/.codebuddy/marketplaces/openviking-local/plugins/openviking-memory
printf '\n// plan-t2-sync-marker\n' >> examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --sync codebuddy; echo "sync exit=$?"
grep -q "plan-t2-sync-marker" "$MKT/scripts/config.mjs" && echo "PROPAGATED" || echo "NOT PROPAGATED (bad)"
git restore examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --sync codebuddy
grep -q "plan-t2-sync-marker" "$MKT/scripts/config.mjs" && echo "STILL THERE (bad)" || echo "CLEAN"
```

Expected: `sync exit=0`、`PROPAGATED`、第二次 sync 后 `CLEAN`。全程无交互提示、无 `Unknown variable` 类 set -u 报错。

- [ ] **Step 8: Commit**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
git add examples/memory-plugin-shared/install.sh
git commit -m "$(cat <<'EOF'
feat(install): add --sync codebuddy fast-path for source->plugin dev loop

C1：一键把本 checkout 的插件源码重新物化 + 重装到 CodeBuddy（复用
install_codebuddy），跳过向导/选 harness/连接配置/校验。解决三副本脱节里
「改源码后忘记重装」的手工负担。arg-parse 设 YES=1 保证非交互。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 健康 + 脱节检测 — `install.sh --verify codebuddy`（C3+B3）

**Files:**
- Modify: `examples/memory-plugin-shared/install.sh`（新增 `verify_codebuddy()` 于 2800-2802 之间；主流程 dispatch 补 `--verify` 分支）

**Interfaces:**
- Consumes: Task 2 的 `VERIFY_TARGET` 变量与 `--verify` arg-parse case；`json_get`（418）、`plugin_dir_on_disk`（1226）、常量 `CODEBUDDY_DIR`/`CODEBUDDY_MKT_DIR`/`CODEBUDDY_PLUGIN_ID`/`OVCLI_CONF`（56）/`OV_HOME`；`heading`/`info`/`warn`/`err`/`t`。
- Produces: `verify_codebuddy()` 返回 0（全过）/1（有问题）；`install.sh --verify codebuddy` 退出码随之。5 项检查：① settings.json 已启用 ② mcp-proxy 进程 ③ server `/health` ④ 召回/注入活动（仅信息，不硬失败）⑤ 源码 vs 已装副本脱节（C3）。

**已核实的真实环境（2026-07-23 实测）：**
- settings.json 有 `"openviking-memory@openviking-local": true` → grep 模式命中。
- mcp-proxy 进程 3 个：source(examples/) + qoder cache + codebuddy marketplace（codebuddy 跑的是 **marketplace 路径** `~/.codebuddy/marketplaces/.../servers/mcp-proxy.mjs`，非 cache）。codebuddy 专属模式 `codebuddy.*mcp-proxy.mjs` 可命中。
- marketplace 副本 `$CODEBUDDY_MKT_DIR/plugins/openviking-memory/` 含 `.claude-plugin/`、`.codebuddy-plugin/`、`hooks/`、`scripts/`、`servers/`、`.mcp.json`（tar 全量拷贝，排除 node_modules/.git/.omc/package-lock）。
- cmp 行为清单文件均存在于两侧：`hooks/hooks.json`、`scripts/{auto-recall,auto-capture,session-start,config}.mjs`、`servers/mcp-proxy.mjs`、`.codebuddy-plugin/plugin.json`。

- [ ] **Step 1: 写失败检查（red）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "exit=$?"
```

Expected（Task 2 已落 `--verify` arg-parse 但未落函数体与 dispatch 分支时）：走 `*)` 之前会被 arg-parse 接受，但主流程无 dispatch → 继续走正常安装向导，或直接报 `verify_codebuddy: command not found`。**判定红的标准：** 没有「逐项 ✓/✗ 健康输出 + 退出码反映结果」。只要还没实现就算红。

- [ ] **Step 2: 新增 verify_codebuddy() 函数**

Edit，old_string（2800-2804，uninstall_codebuddy 收尾 + Validation 注释头）：

```bash
  info "$(t 'Removed the CodeBuddy OpenViking plugin.' '已移除 CodeBuddy OpenViking 插件。')"
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
```

new_string：

```bash
  info "$(t 'Removed the CodeBuddy OpenViking plugin.' '已移除 CodeBuddy OpenViking 插件。')"
}

# verify_codebuddy: read-only health + sync check for the CodeBuddy plugin.
# Prints one ✓/✗ line per check; returns 0 when all hard checks pass, 1 otherwise.
# Check 4 (recall activity) is informational only — a fresh install legitimately
# has no recall state yet. Check 5 (desync) compares checkout sources against the
# materialized marketplace copy over the files that drive runtime behavior.
verify_codebuddy() {
  heading "$(t 'CodeBuddy verify' 'CodeBuddy 校验')"
  local fail=0 f desync src_ver mkt_ver base src_dir
  local settings="$CODEBUDDY_DIR/settings.json"
  local mkt_plugin="$CODEBUDDY_MKT_DIR/plugins/openviking-memory"
  local recall_state="$OV_HOME/state/last-recall.json"
  src_dir="$(plugin_dir_on_disk claude-code-memory-plugin 2>/dev/null)" || src_dir=""

  # 1. Enabled in settings.json
  if [ -f "$settings" ] && grep -q "\"$CODEBUDDY_PLUGIN_ID\"[[:space:]]*:[[:space:]]*true" "$settings"; then
    info "✓ enabled: $CODEBUDDY_PLUGIN_ID"
  else
    warn "✗ not enabled in $settings"; fail=1
  fi

  # 2. MCP proxy process alive (codebuddy-scoped first, then any)
  if pgrep -f "codebuddy.*mcp-proxy.mjs" >/dev/null 2>&1; then
    info "✓ mcp-proxy running (codebuddy)"
  elif pgrep -f "mcp-proxy.mjs" >/dev/null 2>&1; then
    info "✓ mcp-proxy running (shared/other harness)"
  else
    warn "✗ mcp-proxy not running"; fail=1
  fi

  # 3. Server /health
  base="$(json_get "$OVCLI_CONF" url)"; [ -n "$base" ] || base="http://127.0.0.1:1933"
  if curl -fsS -m 5 "$base/health" >/dev/null 2>&1; then
    info "✓ server healthy: $base"
  else
    warn "✗ server unreachable: $base/health"; fail=1
  fi

  # 4. Recall pipeline has fired (informational — absence only means "no prompt yet")
  if [ -f "$recall_state" ]; then
    info "✓ recall state present: $recall_state"
  else
    warn "· no recall recorded yet (send a prompt in CodeBuddy to exercise auto-recall)"
  fi

  # 5. Source vs installed copy sync (C3)
  if [ -z "$src_dir" ]; then
    warn "✗ plugin sources not found (run from a repo checkout)"; fail=1
  elif [ ! -d "$mkt_plugin" ]; then
    warn "✗ marketplace not materialized — run: bash install.sh --sync codebuddy"; fail=1
  else
    desync=0
    for f in hooks/hooks.json scripts/auto-recall.mjs scripts/auto-capture.mjs \
             scripts/session-start.mjs scripts/config.mjs servers/mcp-proxy.mjs \
             .codebuddy-plugin/plugin.json; do
      if ! cmp -s "$src_dir/$f" "$mkt_plugin/$f" 2>/dev/null; then
        warn "  differs: $f"; desync=1
      fi
    done
    src_ver="$(json_get "$src_dir/.claude-plugin/plugin.json" version)"
    mkt_ver="$(json_get "$mkt_plugin/.claude-plugin/plugin.json" version)"
    if [ "$src_ver" != "$mkt_ver" ]; then
      warn "  version: src=$src_ver installed=$mkt_ver"; desync=1
    fi
    if [ "$desync" -eq 0 ]; then
      info "✓ source in sync with installed copy (v$src_ver)"
    else
      warn "✗ source differs from installed copy — run: bash install.sh --sync codebuddy"; fail=1
    fi
  fi

  if [ "$fail" -eq 0 ]; then
    info "$(t 'CodeBuddy plugin OK' 'CodeBuddy 插件正常')"
  else
    warn "$(t 'CodeBuddy plugin has issues (see above)' 'CodeBuddy 插件存在问题（见上）')"
  fi
  return $fail
}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
```

说明（ponytail）：脱节检测用**定点 cmp 行为文件 + 版本比对**的启发式，而非全树 diff——dev-loop 里版本 bump 少，定点比对已能抓住「改了源码忘了 sync」。`# ponytail: 定点 cmp 7 个行为文件；若要全量保证可换 diff -r --exclude`。

- [ ] **Step 3: 主流程 dispatch 补 `--verify` 分支**

若 Task 2 用了精简版 dispatch（只 `--sync`），Edit 把它扩成完整版。old_string：

```bash
if [ -n "$SYNC_TARGET" ]; then
  [ "$SYNC_TARGET" = "codebuddy" ] || { err "Unsupported --sync target: $SYNC_TARGET (expected: codebuddy)"; exit 2; }
  install_codebuddy
  exit 0
fi

select_harnesses
```

new_string：

```bash
if [ -n "$SYNC_TARGET" ]; then
  [ "$SYNC_TARGET" = "codebuddy" ] || { err "Unsupported --sync target: $SYNC_TARGET (expected: codebuddy)"; exit 2; }
  install_codebuddy
  exit 0
fi
if [ -n "$VERIFY_TARGET" ]; then
  [ "$VERIFY_TARGET" = "codebuddy" ] || { err "Unsupported --verify target: $VERIFY_TARGET (expected: codebuddy)"; exit 2; }
  if verify_codebuddy; then exit 0; else exit 1; fi
fi

select_harnesses
```

- [ ] **Step 4: 跑测试转绿（green）— 脱节检测全环**

前置：server 在跑、mcp-proxy 在跑（否则 ②③ 失败会掩盖脱节信号；判读时看 `differs:`/`source differs` 行而非只看退出码）。**只动源码、不污染 marketplace**——用 `git restore` 还原，保证零副作用。

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "baseline exit=$?"   # 期望 0（已同步）
printf '\n// plan-t3-desync\n' >> examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "desync exit=$?"     # 期望 1，输出含 "differs: scripts/config.mjs"
git restore examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "restored exit=$?"   # 期望 0
```

Expected: baseline `exit=0`；desync `exit=1` 且有 `differs: scripts/config.mjs` 与 `✗ source differs from installed copy — run: bash install.sh --sync codebuddy`；restored `exit=0`。

- [ ] **Step 5: 验证 `--sync` 能消除真实脱节（C1+C3 闭环）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
printf '\n// plan-t3-loop\n' >> examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --sync codebuddy                              # 把标记同步进 marketplace
bash examples/memory-plugin-shared/install.sh --verify codebuddy | grep -c "differs: scripts/config.mjs"   # 期望 0（sync 后源码==已装）
git restore examples/claude-code-memory-plugin/scripts/config.mjs
bash examples/memory-plugin-shared/install.sh --sync codebuddy                              # 清标记
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "final exit=$?"      # 期望 0
```

Expected: sync 后 `differs:` 计数 0；最终 `final exit=0`。

- [ ] **Step 6: Commit**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
git add examples/memory-plugin-shared/install.sh
git commit -m "$(cat <<'EOF'
feat(install): add --verify codebuddy health + source/installed desync check

C3+B3：只读逐项校验 CodeBuddy 插件——settings 启用、mcp-proxy 进程、server
/health、召回活动（信息项）、源码 vs 已装副本脱节。退出码 0/1 反映结果，
脱节时提示用 --sync codebuddy 修复。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 参数调优（测量驱动）— 阈值与 token 预算（A）

**Files:**
- Create: `docs/design/codebuddy-memory-tuning.md`
- Modify: `~/.openviking/ov.conf`（仓库外，用户运行时配置，无 commit）

**Interfaces:**
- Consumes: `scripts/debug-recall.mjs`（独立测量工具，直连 server，无需 CodeBuddy）；Task 1 的 `items`（辅助观察命中分布）；`config.mjs` 的配置键与 env 覆盖名。
- Produces: 测量记录文档 + 应用到 `ov.conf` 的 `claude_code.*` 推荐值。**不改代码**；配置现读免同步（D1/Global Constraint）。

**配置键速查（config.mjs，env 覆盖名 / 默认）：**
`scoreThreshold`(OPENVIKING_SCORE_THRESHOLD, 0.35)、`recallLimit`(OPENVIKING_RECALL_LIMIT, 6)、`recallTokenBudget`(OPENVIKING_RECALL_TOKEN_BUDGET, 2000)、`recallMaxContentChars`(OPENVIKING_RECALL_MAX_CONTENT_CHARS, 500)、`recallPreferAbstract`(OPENVIKING_RECALL_PREFER_ABSTRACT, true)、`profileTokenBudget`(OPENVIKING_PROFILE_TOKEN_BUDGET, 10000)、`minQueryLength`(OPENVIKING_MIN_QUERY_LENGTH, 3)、`timeoutMs`(OPENVIKING_TIMEOUT_MS, 15000)、`captureMode`(OPENVIKING_CAPTURE_MODE, semantic)、`commitTokenThreshold`(OPENVIKING_COMMIT_TOKEN_THRESHOLD, 20000)。

**背景（token 节省抓手）：** 最大的 token 来源是 session-start 的 profile 注入（`profileTokenBudget` 默认 10000）。召回侧已被 `recallTokenBudget`(2000) + `recallPreferAbstract`(true) + `recallMaxContentChars`(500) 约束。阈值 `scoreThreshold`(0.35) 决定「放多少进来」——太低放进弱相关=浪费 token+易臆想，太高漏召回。

- [ ] **Step 1: 跑测量电池（red = 无数据，无法定阈值）**

前置：server 在跑。对一组代表性 query 各跑一次 `debug-recall.mjs`，记录每条：config summary 里的 `scoreThreshold`/`recallLimit`、raw 结果分数分布、final picks、生成 system message 字节数。

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking/examples/claude-code-memory-plugin
for q in \
  "我喜欢什么编程语言" \
  "上次升级 OpenClaw 遇到了哪些坑" \
  "vikingdb 和 vikingfs 是什么关系" \
  "install.sh 支持哪些 harness" \
  "我的 Homebrew 镜像偏好是什么" \
  "量子引力波怎么做红烧肉" \
  "帮我写一个完全没接触过的全新项目" \
; do
  echo "===== QUERY: $q ====="
  node scripts/debug-recall.mjs "$q" 2>&1 | grep -Ei "scoreThreshold|recallLimit|finalScore|top|picked|bytes|score=|abstract|category" | head -25
done
```

预期：前 5 条（真实记忆相关）应有若干 `finalScore` 明显高于后 2 条（nonsense/全新项目，应低分或无命中）。**记录每条 query 的 top finalScore**——这是定 `scoreThreshold` 的依据。此步「红」的含义：在跑之前没有任何分布数据，阈值只能是猜的。

- [ ] **Step 2: 决策阈值与预算（写决策规则，不写死）**

按实测分布应用规则：
- **`scoreThreshold`：** 取一个高于 nonsense/全新项目 top 分、低于真实命中有效分的值。候选 `0.45–0.5`。若分布无清晰分界，保守取 `0.4`（在默认 0.35 上小幅抬升）。
- **`profileTokenBudget`：** `10000 → 4000`（候选）。这是 token 节省主抓手；改后观察 `~/.openviking/state/last_inject.md` 大小变化。
- **其余保持默认：** `recallTokenBudget`=2000、`recallPreferAbstract`=true、`recallMaxContentChars`=500、`recallLimit`=6、`captureMode`=semantic。

- [ ] **Step 3: 写测量文档 `docs/design/codebuddy-memory-tuning.md`**

内容结构：① 测量方法（debug-recall.mjs 直连）② 电池 query 清单 + 各自 top finalScore + 生成 message 字节数 ③ 选定的 `scoreThreshold`/`profileTokenBudget` 及理由 ④ 其余保持默认的键清单 ⑤ 应用方式（ov.conf `claude_code.*` / env 覆盖）⑥ 免同步说明（config.mjs 现读）。**数值来自 Step 1 实测，不臆造。** 用真实测到的分数填表。

- [ ] **Step 4: 应用到 `~/.openviking/ov.conf` 的 `claude_code.*`**

读取现有 `~/.openviking/ov.conf`，在 `claude_code` 对象内合并（保留已有键）：

```json
"claude_code": {
  "scoreThreshold": <Step2 实测选定值>,
  "profileTokenBudget": 4000
}
```

用 `node -e` 做安全合并（避免手改 JSON 出错），改前先备份：

```bash
cp ~/.openviking/ov.conf ~/.openviking/ov.conf.bak.$(date +%Y%m%d%H%M%S)
node -e '
const fs=require("fs");const p=require("node:os").homedir()+"/.openviking/ov.conf";
const j=JSON.parse(fs.readFileSync(p,"utf8"));
j.claude_code=Object.assign({},j.claude_code,{scoreThreshold:<Step2值>,profileTokenBudget:4000});
fs.writeFileSync(p,JSON.stringify(j,null,2));
console.log("applied:",JSON.stringify(j.claude_code));
'
```

（也可用 env 覆盖做不改文件的快速实验：`OPENVIKING_SCORE_THRESHOLD=0.48 OPENVIKING_PROFILE_TOKEN_BUDGET=4000`。）

- [ ] **Step 5: 验证配置生效（green）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking/examples/claude-code-memory-plugin
node scripts/debug-recall.mjs "我喜欢什么编程语言" 2>&1 | grep -Ei "scoreThreshold|profileTokenBudget|recallLimit"
```

Expected: config summary 打印出**新的** `scoreThreshold`（=Step2 值）与 `profileTokenBudget`=4000，证明 `ov.conf` 的 `claude_code.*` 被现读生效（无需重启、无需 sync）。

- [ ] **Step 6: Commit（仅文档；ov.conf 在仓库外不提交）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
git add docs/design/codebuddy-memory-tuning.md
git commit -m "$(cat <<'EOF'
docs(design): codebuddy memory tuning — measured score threshold + token budget

A：用 debug-recall.mjs 电池实测分数分布，据此定 scoreThreshold，并把
profileTokenBudget 10000→4000 作为主 token 节省抓手。配置写 ov.conf
claude_code.*（现读免同步，设计 D1）。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 端到端 + 回归 + 手动 B1 验证

**Files:** 无新代码；纯验证。

**Interfaces:**
- Consumes: Task 1–4 全部产出。
- Produces: 验证结论（自动化部分留证据，手动部分给用户清单）。

**自动化部分（可无人值守）：**

- [ ] **Step 1: 三端共享脚本语法 + 向后兼容（B2 增量不破坏 claude/qoder）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking/examples/claude-code-memory-plugin
node --check scripts/auto-recall.mjs && node --check scripts/ov-status.mjs && echo "SYNTAX OK"
# 旧版 last-recall.json（无 items）不应让 /ov 崩溃：
node -e 'const fs=require("fs"),os=require("os"),p=os.homedir()+"/.openviking/state/last-recall.json";const bak=fs.existsSync(p)?fs.readFileSync(p):null;fs.writeFileSync(p,JSON.stringify({count:2,top_score:0.5,tokens_used:10,tokens_budget:2000,reason:"ok",ts:Date.now()}));'
node scripts/ov-status.mjs >/dev/null && echo "BACKWARD-COMPAT OK (no items field tolerated)"
```

Expected: `SYNTAX OK`、`BACKWARD-COMPAT OK`（`Array.isArray` 守卫生效，无 `items` 不报错）。

- [ ] **Step 2: install.sh 正常路径回归（fast-path 不破坏既有流程）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
bash examples/memory-plugin-shared/install.sh --help | grep -E "\-\-sync|\-\-verify" && echo "USAGE OK"
# 不带 --sync/--verify 时 SYNC_TARGET/VERIFY_TARGET 为空 → 快路径跳过，走正常向导。
# 重全量安装属重操作，列为可选手动项；此处至少确认 --help 正常、arg 解析无误。
```

Expected: `--help` 输出含 `--sync codebuddy` 与 `--verify codebuddy`，`USAGE OK`，退出码 0。

- [ ] **Step 3: `--verify` 最终健康（自动化可判）**

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking
bash examples/memory-plugin-shared/install.sh --verify codebuddy; echo "verify exit=$?"
```

Expected: `verify exit=0`（server + mcp-proxy 在跑、源码已同步时）。

**手动部分（需 CodeBuddy IDE，无法无头驱动）— 给用户的验收清单：**

- [ ] **B1 状态可视：** 在 CodeBuddy 里运行 `/ov`，应显示 server 健康 + 最近一次 auto-recall，且命中项带 Task 1 新增的逐行 `[<type> <pct>%] <uri>` 列表。
- [ ] **召回闭环：** 在 CodeBuddy 发一条与既有记忆相关的 prompt（如「我喜欢什么编程语言」），auto-recall 触发；再 `/ov` 应见 `Last auto-recall` 更新 + 命中 URI。
- [ ] **uri-guard 反臆想：** 在 CodeBuddy 里让它原生 Read 一个 `viking://` URI，应被 uri-guard 拦截并重定向到 MCP（而非臆造内容）。
- [ ] **auto-capture 捕获：** 进行一段含偏好/事实的对话，确认记忆被捕获（`ov-status` 或 server 侧可见新增 memory）。
- [ ] **调参生效（Task 4）：** 新开会话，session-start 注入体积应因 `profileTokenBudget=4000` 而明显小于此前；弱相关 prompt 注入应因抬升的 `scoreThreshold` 而更克制。

---

## Self-Review 记录

- **Spec 覆盖：** 设计 §3 三组件全覆盖——组件 A 调参→Task 4；组件 B 可观测（B1 状态可视/B2 召回可见/B3 活动检测）→Task 1（B2）+Task 3（B3 并入 verify 检查④）+Task 5 手动（B1）；组件 C 源码同步（C1/C2/C3）→Task 2（C1）+Task 3（C3）；C2 符号链接全自动在设计 D2 已定为「留研究项」，本计划不含。决策 D1（配置共用 claude_code.*）→Task 4；D2→Task 2 走一键 sync 非符号链接；D3（测量先行）→Task 4 Step 1-2；D4（不动召回逻辑）→Task 1 仅加状态字段、Global Constraint 明确。
- **Placeholder 扫描：** 各 Task 的代码均为完整可落地片段（auto-recall/ov-status 的 Edit 前后串、install.sh 的 6 处 Edit、verify_codebuddy 全函数、ov.conf 合并命令）。唯一「运行时才定」的是 Task 4 的 `scoreThreshold` 数值——这是设计 D3 明确要求的测量驱动，非占位符，且给出了决策规则与候选区间。
- **类型一致：** `items` 元素 `{type, uri, score}` 在 auto-recall.mjs 产出与 ov-status.mjs 消费两处字段名一致；`verify_codebuddy` 引用的常量/函数（`CODEBUDDY_PLUGIN_ID`/`CODEBUDDY_MKT_DIR`/`OVCLI_CONF`/`json_get`/`plugin_dir_on_disk`/`cmp`/`pgrep`/`curl`）均已核实存在于 install.sh 或实测环境。
- **风险与备注：** ① Task 2/3 测试会重装本机 CodeBuddy 插件（幂等，dev-loop 可接受）并已用 `git restore` 保证源码零残留。② `--verify` 退出码是整体健康（含 server/proxy 是否存活），判读脱节时看 `differs:` 行而非仅退出码。③ endpoint 召回路径（auto-recall.mjs 398-418）与多源回退路径都记 `items`——已实测 `/api/v1/search/recall` 响应 `result.entries[]` 带逐条 `{uri,score,type,mode,rank}`（`rendered` 由其拼接），故 endpoint 路径同样可观测，且 `count`/`top_score` 记真实值。
