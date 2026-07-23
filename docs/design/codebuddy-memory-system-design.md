# CodeBuddy 上的 OpenViking 记忆系统设计 spec

- **日期**：2026-07-23
- **状态**：已获用户确认（待实现计划）
- **分支**：`ov-dev-opt`
- **范围**：调参 + 可观测 + 源码同步开发回路（**不改核心召回/捕获算法**）

---

## 1. 背景与现状（调研结论，已实证）

### 1.1 插件形态：一个插件、三个 manifest

OpenViking 的记忆插件是**单一代码体、多 manifest** 结构，位于
`examples/claude-code-memory-plugin/`：

```
.claude-plugin/plugin.json    # Claude Code manifest（含 "mcpServers": "./.mcp.json"）
.codebuddy-plugin/plugin.json # CodeBuddy manifest（无 mcpServers 字段，靠根级 .mcp.json 约定自动发现）
.qoder-plugin/plugin.json     # Qoder manifest
hooks/hooks.json              # 三端共享，9 个 hook 事件，统一用 ${CLAUDE_PLUGIN_ROOT}
scripts/                      # 三端共享（auto-recall / auto-capture / session-start / uri-guard / ov-status ...）
servers/mcp-proxy.mjs         # 三端共享
commands/ov.md                # 三端共享 → /ov 状态命令
```

**关键结论（回应"两个插件 hook 差异"）**：Claude Code 与 CodeBuddy 的 hook **功能上零差异**——
共享同一份 `hooks.json` + `scripts/` + `servers/`。差异仅在 manifest 元数据：
- MCP 声明方式：Claude Code 用 `plugin.json` 的 `mcpServers` 字段显式声明；CodeBuddy 靠根级 `.mcp.json` **约定自动发现**。
- 占位符：统一 `${CLAUDE_PLUGIN_ROOT}`，CodeBuddy 兼容层解析（也认原生 `${CODEBUDDY_PLUGIN_ROOT}`）。

**对设计的意义**：改 hook 行为 = 改共享脚本 → 三端同时受影响。所以本方案不碰核心逻辑，
只做外围三层（调参 / 可观测 / 同步），避免牵动三端。

### 1.2 CodeBuddy 端当前状态：能正常工作

| 检查项 | 结果 |
|---|---|
| 已安装 | ✓ `~/.codebuddy/plugins/cache/openviking-local/` + `~/.codebuddy/marketplaces/openviking-local/` |
| 已启用 | ✓ `~/.codebuddy/settings.json` → `openviking-memory@openviking-local: true` |
| MCP proxy 在跑 | ✓ 进程实证（codebuddy 路径下的 `servers/mcp-proxy.mjs`） |
| OpenViking server | ✓ `127.0.0.1:1933` /health HTTP 200 |

唯一未 100% 铁证的是 hook 端到端触发事件，但脚本共享、占位符已被 superpowers 生产验证，
理论上必然触发；本方案的"可观测"组件会把这点变成可随时一键验证。

### 1.3 三份副本与"快照脱节"风险（回应"老路径残留"疑问）

```
真源        examples/claude-code-memory-plugin/                ← 唯一权威源码
  │  tar 拷贝（install.sh:2739 materialize_codebuddy_marketplace）
  ▼
marketplace ~/.codebuddy/marketplaces/openviking-local/         ← 源码实体拷贝（非符号链接）
  │  codebuddy plugin install 拷贝
  ▼
cache       ~/.codebuddy/plugins/cache/openviking-local/openviking-memory/<ver>/  ← 真正被加载
```

- 当前三份**完全同步**（均 0.4.3，hooks.json / config.mjs / auto-recall.mjs 逐一 diff 一致）。
- 但 marketplace 与 cache 都是**拷贝快照**：`install.sh:2785` 自己印着警告
  "cache 快照，改动源码后需重跑 install.sh"。**改源码后 codebuddy 端不会自动更新**。
- 结论：`openviking-local` 不是"已删除的老版本"，而是"任何时候都离过期只差一次源码改动"。

### 1.4 可复用的已有资产

- **`uri-guard.mjs`**（`PreToolUse: Read|Glob|Grep`）：检测到 `viking://` URI 即 **deny** 原生文件读取，
  强制改走 OpenViking MCP 的 `read/glob/grep`。"防止模型把记忆当文件乱读再脑补编造"的护栏**已存在**。
- **`ov-status.mjs`**（`/ov` 命令）：已能打印 server 健康、上次注入（大小/时间/audit 路径）、
  上次召回（**条数 / top 相似度 / token 用量 vs 预算 / reason**）、三路网关开关、认证来源。
  状态文件 `~/.openviking/state/last-recall.json`、`~/.openviking/last_inject.md` 由共享脚本写，与触发端无关。
- 其他：`debug-recall.mjs`、`statusline.mjs`、`logRankingDetails` 开关。

### 1.5 现有调参旋钮（`scripts/config.mjs`，本方案的起点）

解析链：`env(OPENVIKING_*) > ovcli.conf > ov.conf > 默认`。ov.conf 里这些字段的 section 名是
**`claude_code.*`**（历史遗留），CodeBuddy 的共享脚本同样读它。

| 旋钮 | 默认 | 作用 |
|---|---|---|
| `recallLimit` | 6 | 每次 prompt 召回条数 |
| `scoreThreshold` | 0.35 | 相似度阈值，低于此不注入（**精准/防臆想核心**） |
| `recallMaxContentChars` | 500 | 单条记忆截断 |
| `recallTokenBudget` | 2000 | 每次召回 token 预算 |
| `recallPreferAbstract` | true | 注入 L0 摘要而非全文（**省 token 核心**） |
| `recallPeerScope` | all | 召回范围 all/actor |
| `minQueryLength` | 3 | 短于此不召回 |
| `profileTokenBudget` | 10000 | session-start 注入预算 |
| `noAutoInject` | false | 关闭会话注入 |
| `captureMode` | semantic | 捕获模式 semantic/keyword |
| `captureMaxLength` | 24000 | 捕获内容上限 |
| `captureAssistantTurns` | true | 是否捕获 assistant 侧 |
| `commitTokenThreshold` | 20000 | 提交阈值 |
| `writePathAsync` | true | 写路径异步 |

---

## 2. 目标与非目标

### 目标

让 CodeBuddy 上的 OpenViking 记忆管线做到：
1. **明确的自动召回** —— 召回可靠发生，且"召回了哪几条、各多少分、花了多少 token"可见。
2. **创建记忆信息** —— 捕获链路正常工作，能从对话沉淀记忆。
3. **节省 token** —— 在保证质量前提下，优化注入/召回的 token 分配。
4. **精准召回、避免臆想编造** —— 提高相关性阈值、只注入干净摘要、强制记忆走正确工具。
5. **源码不脱同步** —— 改源码后 codebuddy 端能便捷更新，且脱节可被一眼发现。

### 非目标（YAGNI）

- 不重构召回/捕获算法（那是"方向 3"，拆为后续独立 spec）。
- 不改 `uri-guard` 行为，不动 hook 事件挂载。
- 不引入 `codebuddy.*` 配置 section（见决策 D1）。
- 不动 Claude Code / Qoder 端的任何行为。

---

## 3. 设计

### 组件 A · CodeBuddy 专属调参

**A1. 配置方式**：直接共用 `ov.conf` 的 `claude_code.*` section（见决策 D1），
出一份针对 CodeBuddy 所用模型（`settings.json` 中 `model: hy3`）的推荐值。

**A2. 测量先行**：不拍脑袋定值。先让 CodeBuddy 真实跑一段，用 `/ov` + `debug-recall.mjs`
观察召回分数分布与 token 用量，再定阈值。

**A3. 调参方向**（具体数值以 A2 测量结果为准）：
- 省 token：`profileTokenBudget` 10000 → 下调（候选 ~4000）；`recallPreferAbstract` 保持 `true`（L0-only）；`recallTokenBudget` 2000 保持或微调。
- 精准/防臆想：`scoreThreshold` 0.35 → 上调（候选 0.45–0.5）滤掉低相关噪音；`recallMaxContentChars` 500 保持。
- 捕获：`captureMode semantic`、`captureAssistantTurns true` 保持。

### 组件 B · 可观测（看得见召回 + 一键确认在工作）

**B1. 验证 `/ov` 在 CodeBuddy 可用**：`commands/ov.md` 三端共享，CodeBuddy 按约定自动发现命令；
理论已可用，需实测确认 —— 这就是"确认在工作"的一键入口。

**B2. 召回可见性**：确认 `last-recall.json` 已含条数/top 分数/token；若信息不足，让 `auto-recall.mjs`
把"召回的 URI 列表 + 各自分数"**追加落盘**供 `/ov` 展示。**只加状态记录，不改召回逻辑**。

**B3. CodeBuddy 验证脚本**：自动检查以下各项并输出"正常/异常"结论：
- 插件已启用（settings.json enabledPlugins）
- mcp-proxy 进程在跑
- server /health 健康
- 最近确有 recall / inject 事件（状态文件 mtime 新鲜）
- **源码版本 vs cache 版本一致**（见组件 C 的脱节检测）

### 组件 C · 源码同步开发回路

**C1. 一键同步（推荐，务实）**：新增 `install.sh --sync codebuddy`（或 `make sync-codebuddy`），
一步完成"重新物化 marketplace + 重新 plugin install"，把 dev 回路压缩为
"改源码 → 跑一条命令 → 重启 IDE"。

**C2. 更自动（可选研究项，不承诺）**：调研 CodeBuddy 是否在 `/reload-plugins` 或 `plugin update`
时从 marketplace 刷新 cache；若 marketplace 改为指向源码的符号链接，cache 能否自动跟上。
结论成立再采用，否则维持 C1。

**C3. 脱节检测**：并入 B3 的验证脚本 —— 比对"源码 plugin.json 版本 vs cache 拷贝版本/内容"，
发现脱节立即提示重跑同步。直接回应"老路径残留"担忧：**即使忘了同步也能一眼看穿**。

---

## 4. 关键决策

- **D1 · 配置共用 `claude_code.*`，不新增 `codebuddy.*`**：引入新 section 需改共享 `config.mjs`，
  违背"不动核心"约束；代价仅是语义别扭，用文档说明即可。
- **D2 · 源码同步走"一键 sync + 脱节检测"，而非"符号链接全自动"**：cache 快照由 codebuddy 的
  install 语义决定，符号链接只能保证 marketplace 新鲜、保证不了 cache；"全自动"留作研究项 C2，
  先用一键 sync 把回路变短、用脱节检测兜底。
- **D3 · 调参值测量先行**：阈值/预算的具体数值以真实测量为准，不在 spec 里写死。
- **D4 · 可观测只加状态落盘，不改召回逻辑**：遵守"不动共享核心算法"的硬约束。

---

## 5. 验证与测试

- **CodeBuddy 实测**：`/ov` 命令、自动召回、自动捕获、uri-guard 拦截、一键同步回路、脱节检测。
- **回归**：不动共享逻辑 → Claude Code / Qoder 理论零影响；但 `install.sh` 加了 sync，需回归安装/卸载流程。
- **三端冒烟**：确认改动不破坏另两端。

---

## 6. 开放问题 / 后续

- C2 的 CodeBuddy cache 刷新行为（`/reload-plugins` / `plugin update` 是否重读 marketplace）待实测。
- 若多端（Claude Code + CodeBuddy）同时在线，状态文件（`last-recall.json` 等）为共享最后写胜出，
  `/ov` 显示的是最近一次触发端的数据 —— 可接受，暂不做 per-harness 状态隔离。
- "方向 3"（重构召回/捕获管线：L0-only 注入、显式召回标记带分数、捕获去重、防臆想强化）
  拆为后续独立 spec，按需推进。
