# OpenViking 插件注入预算 — 现状、根因与落地优化方案

日期：2026-09-28（v2，经 kimi-k3:max / doubao-seed-evolving:max 两轮对抗审查修正）
环境：OpenViking server `v0.4.22.dev319` @ `http://127.0.0.1:1933`（本地；`ov.conf`：local vectordb / local agfs / rerank=llm_score / vlm.max_concurrent=3 / embedding.max_concurrent=3）
被分析的配置：`~/.openviking/ovcli.conf` 的 `plugin` 段
被分析的代码：`examples/pi-coding-agent-extension/`（本机实体 `~/.pi/agent/extensions/openviking/`）

> v1 的结论中，经对抗审查被推翻的部分已在正文标注「**v1 错误**」，不删除，以便追溯。

## 1. 问题陈述

`~/.openviking/ovcli.conf` 当前生效：

```json
"plugin": {
    "profileTokenBudget": 3000,
    "recallLimit": 5,
    "recallTokenBudget": 1500,
    "takeoverTokenThreshold": 16000,
    "skillCatalogTokenBudget": 600
}
```

要回答三个问题：这 5 个键在本机 pi + 本地服务端场景下**是否真的生效**、**取值是否合理**、**还有哪些没被利用的杠杆**。

历史包袱：2026-09-27 曾以「默认注入 ≈ 75,200 tokens」为基线压减。该基线算法错误（第 3.4 节）。

## 2. 实测基线与量级

| 注入点 | 触发时机 | 上限（配置决定） | 实测填充 |
|---|---|---|---|
| session-start profile 块 | 每进程一次 | profile 3000 + skills 600 | ≈ 3.6k tokens / ≈ 12 KB |
| 每轮 recall | 每个用户 prompt | recallMaxTokens 1600（服务端默认） | ≈ 1.1k tokens |
| takeover overview | 达阈值时一次 | 3000 | 未实测 |

「上限」与「填充」必须分开读：**上限相加不等于真实注入**（v1 在此处两次犯错，见 3.4）。

session-start 块的实际构成（本会话注入块逐行统计）：profile 4 行 + preferences 44 行 + entities ~160 行（`dropped=156`）+ skills 47 行（`dropped=128`）。

注意：该块在 pi 侧由 `start()` 内构建一次并经 `startPromise` 记忆化（`index.ts:181`）。`isRepeatInjection`（`profile-inject.mjs:478`）**在 pi 扩展内零调用**——v1 写的「digest 去重」不成立。

库体量（`~/.openviking/data/viking/dever-space/user/trae_dever/`）：profile.md 39 KB（服务端读回 4 行）、preferences 51、entities 452、experiences 1115、自有 skills 128（注入目录合计 175）。

量级判断（**数字待证**）：pi 系统提示底噪 = 技能清单 ≈ 18k + 扩展工具 Schema ≈ 12k + AGENTS.md ≈ 10k ≈ 40k，来源为 2026-09-27 的会话实测记录，**本仓库无法复现验证**。对照之下 `plugin` 段总可控量约 5k。

## 3. 根因分析：逐旋钮生效路径

配置解析层次（`config.ts` 注释）：`env(OPENVIKING_*) → workspace 文件 → ovcli.conf:plugin.pi → ovcli.conf:plugin → defaults`。

本机实测：`printenv` 无 `OPENVIKING_*`；`plugin` 为扁平结构（无 `plugin.pi`）；仓库根无 `.openviking/`；`ov.conf` 无 `plugin` 段。**即：只有 `plugin` 与 defaults 两层在起作用。**

### 3.1 `recallTokenBudget` — 默认配置下是死旋钮

pi 召回走服务端 context face：`recall-core.mjs:516` 向 `/api/v1/search/search` POST `mode=context`。该 body 构造（`recall-core.mjs:114-155`）不读该键。它的读取点只有两处：`recall-core.mjs:388`（`formatFallback`）与 `:762`（raw 路径超预算退回）。

可达该路径的条件是 `wantsLocalCompression`（`recall-core.mjs:583-585`），即 `recallCompress` 为 `client/auto`。pi 上该键默认 `"off"`（`config-schema.mjs` recallCompress：harness 覆写表仅 claude_code/codex），且可被 env `OPENVIKING_RECALL_COMPRESS` 绕过 ovcli.conf 打开。

**结论：本机当前零行为；但它是「条件死键」，不是无条件死键。**

真正限制召回注入量的是 **`recallMaxTokens`**（默认 1600，`sendOnlyWhenConfigured`）。未配置 → 服务端 `params.py: DEFAULT_MAX_TOKENS = 1600`。

同类条件死键：**`recallMaxContentChars`**（默认 500）。读取点 `recall-core.mjs:101`（legacy body）与 `:382/:391`（fallback content 截断），context-face 路径由服务端负责内容读取，该键不参与。

### 3.2 `recallLimit: 5` — 落在配额塌陷平台上

context 模式下 `recallLimit` 只被换算成 `quotas`（`buildContextSearchBody` → `codingQuotas` → `scaleQuotas`）。

`scaleQuotas()`（`recall-core.mjs:57-84`）：

```js
if (slots < order.length) {          // order = 6 个类目桶
  for (const key of order) quotas[key] = 1;   // 每类目各 1，无视 slots 具体值
  return quotas;
}
```

- `slots ∈ [1,6]` → 全部等价，每类目各 1。
- `slots = 7` 起开始按权重分配（`order` 全部置 1 已用满 6 个，第 7 个给 deficit 最大的 `resources`）。

**v1 错误**：「默认 10 才按权重分配」不成立——权重从 7 起分化，10 没有特殊性。10 的特殊之处只是恰好等于服务端 coding preset（`params.py: PURPOSE_PRESETS["coding"]` = `{events:1, entities:2, preferences:1, experiences:1, resources:3, skills:2}`）。

`recallLimit=5` 的实际代价（库体量 1115 experiences / 452 entities）：

| 类目 | quota(5) | quota(10) |
|---|---|---|
| events / preferences / experiences | 1 | 1 |
| entities | 1 | 2 |
| resources | 1 | 3 |
| skills | 1 | 2 |

### 3.3 预算收紧不截断，只降 tier；但硬约束是 max_tokens，不是 per_entry_cap

`openviking/retrieve/context_assembler/budget.py`：

- 每个候选从类目默认 tier 逐级下探，**放不下就降级，不截断**（源码注释：*"an oversized tier falls back to the previous one instead of being truncated"*）。连 URI 都放不下才 `dropped`。
- 降级阶梯**按类目不同**（`budget.py:_tiers_down_from` + `params.py`）：
  - `events`（默认 `overview`）：`overview → abstract → uri`
  - memory 类目（默认 `abstract`，且 `FULL_BODY_ABSTRACT_CATEGORIES` 里其 abstract 就是全文）：`abstract → overview → uri`（`overview` 是全文的 `# Summary` 子集，作为更便宜的替代插入）
  - `resources`/`skills`：`abstract → uri`（不读正文）
- **唯一硬约束是 `max_tokens`**（`budget.py:111` `used + cost > max_tokens`）。
- `per_entry_cap = max_tokens // 候选数 * 2`（`budget.py:43-45`）是**软约束**：uri tier 豁免（`budget.py:105`），且第三轮 `spare_upgrades = upgrade_pass("full", respect_cap=False)`（`budget.py:171`）显式放弃 cap。

**v1 错误**：写「真正的硬约束是 per_entry_cap」——错，cap 可被绕过；也写错了降级阶梯顺序（`full→overview→abstract→uri` 只对未特判的情形近似成立）。

cap 的真实副作用在 `pipeline.py:110-118`：`cap` 先于内容读取计算，`oversized_abstract_needs_body(c, cap)` 为真的候选会触发正文读取（本地，`READ_CONCURRENCY=8`）。因此**收紧 max_tokens 会增加本地正文读取**（无 Ark 成本，有本地延迟）。

### 3.4 `takeoverTokenThreshold: 16000` — 有效，但理由不是「每次省 10k」

`lib/takeover-core.mjs:252-257`：

```js
this.pendingTokens += estTokens;
if (this.pendingTokens < this.config.takeoverTokenThreshold) return false;
if (this.lastSeenUserTurns <= this.config.takeoverKeepRecentTurns) return false;  // 默认 3
return this.commitAndAdvance();
```

命中后：flush → commit → `pollOverview()` → 用一条 `buildOverviewMessage(..., takeoverOverviewBudget=3000)` 替换掉已提交历史。overview 未就绪时 `pendingTokens = 0` 且不推进边界，下轮重试。

**v1 错误（两条）**：

1. **「净收益 ≈ −10k tokens/次」是假账。** 每次 commit 的替换率是 `1 - overview/chunk`：阈值越小，chunk 越小，替换率越**差**（16000 时 ≈ 77%，30000 时 ≈ 89%）。阈值改变的是**峰值上下文与提交频率**，不是单位对话的节省率。选 16000 的正确理由只有一个：pi 每轮全量重发历史，压低峰值 = 压低每轮 prompt。代价是提交频率 `30000/16000 = 1.875×`（v1 写的「×2」不准确）。
2. **「默认叠加 75,200」不成立。** `takeoverTokenThreshold` 是**触发阈值不是预算**；`resumeArchiveInject` / `resumeArchiveTokenBudget` / `resumeArchiveMaxChars` 在 pi 无消费者；`resumeContextBudget` 只在 `takeoverEnabled=false` 分支（`index.ts:189-192`、`:378-393`）被读取。默认 `takeoverEnabled=true` → 三者全不参与。**v1 的「pi 真实默认注入 ≈ 12.8k」同样不严谨**：那是「每会话一次的 10k + 每轮 ≤1.6k」的上限混合体，不是填充值。

轮询窗口：`takeoverOverviewPollMs × takeoverOverviewPollMax` = 15 次轮询 = **14 个间隔 ≈ 28 s**（v1 写 30 s 略偏）。

提交是否与 Ark 抢并发槽：commit → `POST /sessions/{id}/commit`（`client.ts:118-127`）→ `session.py` 归档 + 抽取入 QueueFS；`ov.conf` 侧 `vlm.max_concurrent=3`、`embedding.max_concurrent=3`、`memory.extraction_enabled=true`。路径存在，量级未实测。

**服务端 auto-commit 不会独立放大这件事**：`session/auto_commit_policy.py` 明确 *"Sessions without a stored policy keep automatic commits disabled"*，本机无存储策略 → 服务端自动提交关闭（可经 session config API 打开，届时阈值默认 `DEFAULT_PENDING_TOKEN_THRESHOLD = 150000`）。

### 3.5 惰性键清单（写了不生效）

| 键 | pi 扩展中的消费者 |
|---|---|
| `noAutoInject` | 无（仅 `config-schema.mjs` 声明）。pi 关召回要用 `autoRecall: false`（`isRecallEnabled`） |
| `repoContext` / `repoContextCacheTtlMs` | 无 |
| `skillExperience` / `skillExperienceLimit` | 无 |
| `resumeArchiveInject` / `resumeArchiveTokenBudget` / `resumeArchiveMaxChars` | 无（整个扩展 grep 不到读取点） |
| `resumeContextBudget` | 仅在 `takeoverEnabled=false` 分支（`index.ts:189-192`、`:378-393`） |
| `commitTokenThreshold` | 仅 `!takeoverEnabled` 时（`sync.ts:163-165` 守卫 → `:202-208`） |
| `commitTurnThreshold` | **任何分支都没有**（pi 扩展内只有 `config-schema.mjs:142` 一处声明） |
| `recallTokenBudget` / `recallMaxContentChars` | 仅 context face 不可用或 `recallCompress≠off` 时（见 3.1） |
| `sessionStartMaxBytes` | pi 无 harness 覆写（仅 claude_code/codex/zcode）→ `capTokens = ∞` |

### 3.6 `profileTokenBudget` / `skillCatalogTokenBudget` 的分配算法

`shared/profile-inject.mjs:371-430`：

- `memoryBudget = min(profileTokenBudget, capTokens - skills.used)`，pi 下 `capTokens = ∞`
- profile 最多占 `memoryBudget / 2`，其余给 preferences / entities 列表（`formatListing` 逐行累计，放不下即丢并记 `dropped`）
- `<available-skills>` 走独立预算；先试带 description，超预算退化为「只列名字」，再超退化为一行为一行计数

**排序偏差（v1 写错）**：`lsDir`（`profile-inject.mjs:126-146`）请求 `recursive=true&abs_limit=512&node_limit=512`，**取了 abstract**（doubao 第一轮称 `abstracts=0` 是伪证，其第二轮已自纠），最后 `.sort((a,b) => a.name.localeCompare(b.name))` —— **纯字母序，无 score**。技能清单同理（`own` 组按 `byName`）。

因此 600 tokens 下 175 个技能只展示 47 个、452 个 entities 只展示 ~160 个，被隐藏的是**字母序后排**，不是「最不相关」的。v1 写的「按 score 顺序」不成立。

## 4. 实测验证

### 4.1 session-start 块

见第 2 节。技能目录 `dropped=128` 表明 600 预算被正确执行。

### 4.2 召回注入量（同一 query，coding 10 条配额）

| max_tokens | 结果 |
|---|---|
| 1600 | 前 9 条带正文 + 1 条 `detail="uri"`；第 10 条 = `overview` |
| 1200 | 与 1600 逐条一致，仅第 10 条由 `overview` 降到 `uri` |

**该方法论的已知缺陷**（对抗审查提出，部分接受）：

- **接受**：单次观测、未留 HTTP 响应或 `stats.used_tokens` 原始记录（服务端确实透出 `used_tokens`，`budget.py:178-186` 经 `pipeline.py:188`）；「填充 ≈1.1k」是目测。
- **接受**：v1「换不到 token 节省」与「第 10 条降级」自相矛盾——降级本身就是节省（数百 token）。正确表述是：**节省存在但被未填满的余量吸收；收益未证。**
- **不接受**：审查者怀疑 `dedup_turns=5` 污染了两次测量。核实：dedup 只在 `body.session_id` 存在时才下发（`recall-core.mjs:141-144`），这两次调用**未传 session_id**，服务端 `dedup_turns` 取默认 0（`search.py` Field(default=0)），两次候选集同源，可比。

**cap 副作用已证**：1600/10 候选 → cap 320；1200/10 → cap 240。cap 收紧使 240-320 token 区间的 memory 文件更易触发 `oversized_abstract_needs_body` 的正文读取。

→ **结论保留但换理由**：不显式设 `recallMaxTokens`。理由是「收益未证、副作用已证」，不是「换不到节省」。

### 4.3 降级而非截断的行为证据

实测输出混合 tier：`detail="overview"`（events）、`detail="abstract"`（entities/preferences/skills）、`detail="uri"`（一条 resources）。无半句截断。

### 4.4 被审查证伪的一项 v1 遗留假设

v1 的开放式问题 Q1 提出「用 `detail_by_category` 把 resources/skills 钉成 abstract 来换回 `per_entry_cap`」。**该杠杆是 no-op**：`tier_window` 对 pin 直接返回 `(start, start)`，而 resources/skills 的默认 tier 本就是 `abstract`（`params.py:52-63`），且 `per_entry_cap` 只依赖 `max_tokens` 与候选数，与 detail 无关。钉位只能往**深**钉（更费 token），与目标相反。

## 5. 落地优化方案

### 5.1 配置变更

```diff
 "plugin": {
     "profileTokenBudget": 3000,
-    "recallLimit": 5,
-    "recallTokenBudget": 1500,
+    "recallLimit": 10,
+    "recallQueryExpansion": "off",
     "takeoverTokenThreshold": 16000,
-    "skillCatalogTokenBudget": 600
+    "skillCatalogTokenBudget": 600,
+    "recallCompress": "off"
 }
```

| 变更 | 依据 | 预期收益 | 预期成本 |
|---|---|---|---|
| 删 `recallTokenBudget` | 条件死键（3.1） | 消除误判来源 | 0 |
| `recallLimit` 5 → 10 | `[1,6]` 等价，5 白亏广度（3.2） | 恢复 resources 3 / entities 2 / skills 2 | +≈400 tokens/轮；cap 533→320 → 中型 memory 文件更易触发本地正文读 |
| **新增** `recallQueryExpansion: "off"` | 见 5.3 —— 唯一能砍掉**每轮一次 Ark VLM 调用**的旋钮 | 每轮省一次 query planner 调用（`ov.conf:query_planner = doubao-seed-2.0-mini`），并缩短召回关键路径 | 含糊 query 的召回质量下降，删一行即回滚 |
| **不新增** `recallMaxTokens` | 4.2 | 避免无收益地收紧 cap | — |
| 保留 `profileTokenBudget: 3000` | 2 | 已达 12.8k 上限 → 3.6k 填充 | 字母序后排的 entities/skills 被动不可见 |
| 保留 `takeoverTokenThreshold: 16000` | 3.4 | 压低峰值上下文（pi 每轮全量重发） | 提交频率 1.875×，争抢 vlm/embedding 并发槽 |
| 保留 `skillCatalogTokenBudget: 600` | 3.6 | — | 加水位不能解决字母序截断 |
| **新增** `recallCompress: "off"`（显式声明） | 3.1 | 把当前隐式默认变为显式，防止 env/未来默认漂移重新激活 `recallTokenBudget` | 0 |
| **不改** `takeoverOverviewPollMs` | 5.2 | 见下 | — |

### 5.2 被否决的改动：`takeoverOverviewPollMs: 1000`

第一轮双方一度都同意写 1000（把 28 s 等待窗口腰斩）。第二轮 kimi 反对，机制成立：**`takeoverOverviewPollMax` 固定 15**，把 `pollMs` 从 2000 降到 1000 等价于把**总等待窗口**从 28 s 砍到 14 s，而不是「更早拿到 overview」。若 overview 生成耗时落在 14–28 s 区间，改 1000 会让 `pollOverview` 超时 → `pendingTokens = 0`、边界不推进 → 白提交一次。**本机无 overview 生成延迟数据，任何取值都缺依据。**

→ 不改。若确实感知到卡顿，正确组合是 `pollMs: 1000 + pollMax: 30`（提高轮询频率的同时保住总窗口），且需先测 overview 延迟。

### 5.3 本轮新增的最大杠杆：`recallQueryExpansion`

- 默认 `"auto"`（`config-schema.mjs`，`sendOnlyWhenConfigured`）→ 未配置时不下发 → 服务端 `search.py` 默认 `query_expansion: "auto"`。
- 客户端判定（`recall-core.mjs:187-190`）：`sessionId` 存在 且 `query_expansion !== "off"` → 走 expansion 保险丝（客户端 deadline 15 s，覆盖服务端 `retrieval.recall_intent_timeout_s` 5 s）。
- 服务端展开走 `query_planner` VLM（`ov.conf`：`doubao-seed-2.0-mini`，thinking=false，timeout=60）。
- 即：**每个带 session 的用户 prompt 都会多一次 Ark 调用，且在召回关键路径上。**

这是 `plugin` 段里唯一能把「每轮一次 Ark 调用」直接砍掉的旋钮，与用户的核心痛点（Ark 过载 / TTFT）同向。审查双方的分歧是「默认关」还是「仅当仍过载时关」；本方案取**默认关**（一行、可逆、方向与痛点一致）。

### 5.4 未采纳 / 已证伪的杠杆

| 杠杆 | 处置 | 依据 |
|---|---|---|
| `detail` / `detail_by_category` 钉 resources/skills 到 abstract | **证伪**，不动 | 4.4：no-op |
| 关闭 `rerank.enabled` | **证伪**，不动 | 召回路径 `VikingFS.find` 用 `RetrieverMode.QUICK`（`_semantic.py:314`），rerank 仅在 `THINKING` 模式下触发（`hierarchical_retriever.py:345,381,808`）；pi 召回不经过 rerank |
| 关闭服务端 auto-commit | **不需要** | 3.4：无存储策略时服务端自动提交本就关闭 |
| `scoreThreshold` 上调 | 暂不动 | 实测命中集中在 0.68~0.82，本地小库上收效有限 |
| `recallDedupTurns` | 已生效，不动 | 默认 5 |

### 5.5 真正的量级大头（非本配置项）

按 2 节的量级判断（数字待证）：技能清单 ≈18k + 扩展工具 Schema ≈12k + AGENTS.md ≈10k ≈ 40k，远超 `plugin` 段可控的 5k。若 TTFT 仍是问题，优先级高于继续调 `plugin`：

1. 清理 pi 侧技能清单（本机 loaded skills 近百个，全部进 system prompt；其中 OpenViking 侧 175 个里自有 128 个是会话抽取产物，需人工判留）
2. 卸载不用的 pi 扩展（每个扩展贡献工具 Schema；`mcpEnabled: false` 可单独关掉 OpenViking 自己的工具面，`index.ts:39`）
3. `~/.pi/agent/settings.json` 的技能过滤

**未验证的前提**：Ark 侧 prompt caching 是否命中，决定了「每轮全量重发历史」是否真的线性付费；本仓库无法验证。若缓存高命中，则 5.5 与 `takeoverTokenThreshold` 的收益都要重估。

## 6. 对抗审查结论（Q1–Q6 裁决）

两轮制（第一轮独立攻击，第二轮交叉质证），审查方 `reviewer`，模型 `volcengine-agent-plan/kimi-k3:max` 与 `volcengine-agent-plan/doubao-seed-evolving:max`。原始记录：`/tmp/ov-debate/r{1,2}-{kimi,doubao}.md`。

合并裁定：**OK with notes** —— 配置可落地，v1 文档的量化论证需按第 3、4 节的标注修正（本 v2 已修正）。

| 问题 | 裁决 |
|---|---|
| Q1 recallLimit 5→10 的 +400 tokens 是否值得 / 能否用 detail 钉位换回 cap | 值得（5 是塌陷平台）；detail 钉位是 no-op，**该备选作废** |
| Q2 16000 让提交频率 1.875× 是否值得 | 值得，但理由是压峰值不是「每次省 10k」；auto-commit 关闭 → 无叠加放大 |
| Q3 recallMaxTokens 显式 1600 vs 不设 | 不设。收益未证、cap 收紧副作用已证 |
| Q4 entities 字母序截尾能否改用更聪明的筛选 | 当前实现确实只按 `localeCompare`；改进需改扩展（`lsDir` 排序）或服务端支持排序参数，本轮不动 |
| Q5 175 个技能是否抽取噪声 / 600 预算是否在给该清的清单打补丁 | 倾向「该清不清」；清理由 5.5 承担，不加水位 |
| Q6 是否有未识别的配置层会推翻结论 | 已实测排除：无 `OPENVIKING_*` env、无 `plugin.pi`、无 workspace 配置、`ov.conf` 无 `plugin` 段。**但**审查补充了三条被 v1 遗漏的代码路径：`recallQueryExpansion`（5.3）、rerank 的 QUICK 门（5.4）、auto-commit policy（3.4） |

### 双方一致确认的 v1 硬伤

1. 3.6 排序依据写错（字母序 ≠ score）
2. 3.3 把 `per_entry_cap` 当唯一硬约束（忽略 uri 豁免与 spare pass）
3. 5.1 的「−10k/次」是假账
4. Q1 的 `detail` 备选是 no-op
5. 3.4 的轮询窗口 30 s（实为 28 s）
6. 3.2 的「10 才按权重分配」（实为 7 起）
7. 3.4 的「12.8k 真实注入」把上限当填充
8. 3.3 的降级阶梯顺序未按类目分别写

### 残余不确定性

- pi 底噪 18k/12k/10k 无本仓库出处，无法验证
- Ark prompt caching 命中率未知 → C9/TTFT 的因果链悬置
- overview 生成的实际延迟未知 → `takeoverOverviewPollMs/pollMax` 任何取值缺依据
- 运行中 server 构建版本与仓库 checkout 是否一致未验证
- 提交侧（VLM/embedding 并发槽争抢）无实测耗时

## 7. 验证方法

1. 改配置后新开终端跑 `pi`，确认状态栏 `OV ✓ · ctx 0 · ~0/16000`
2. 发一轮带记忆的问题，与 4.2 的 6 条基线对比，确认召回条目升至 ~10 条
3. 观察注入块 tier 混合比例与 `dropped` 计数是否符合 4.2 预期
4. 记 3 次冷启动 TTFT 与改造前对比。**若 TTFT 无变化，即证实 5.5 的「大头在底噪」判断**，后续优化应转向技能清单与扩展裁剪
5. 观察 2~3 天内是否出现 `takeover overview not ready` 日志（`takeover-core.mjs` 有对应 log 分支）——若出现，才轮到调 `pollMs/pollMax`
6. 若召回质量（尤其含糊 query）明显下降，回滚 `recallQueryExpansion`
