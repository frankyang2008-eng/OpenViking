# omp 插件原生能力对齐设计 spec

- **日期**：2026-09-23
- **状态**：设计已获用户确认（实施计划见 `omp-plugin-native-parity-plan.md`）
- **分支**：`ov-dev-opt`
- **范围**：`examples/omp-openviking-extension/` 的工具面 / 配置层 / 守卫 / 文档 + `examples/memory-plugin-shared/install.sh` 的 omp 安装与卸载
- **不在范围**：`memory-plugin-shared/lib` 的算法、pi 侧扩展、服务端 MCP 接口、omp 主程序

---

## 1. 结论摘要

| # | 决策 | 一句话理由 |
|---|------|-----------|
| D1 | 工具面改用 **omp 原生 MCP**：安装时把 OpenViking 写进 `~/.omp/agent/mcp.json`（stdio 代理），删除手写的 7 个 `viking_*` 工具 | omp 自带 MCP 客户端与按需发现机制，扩展不该再实现一遍；服务端 16 个工具自动到位 |
| D2 | 配置层迁到 **shared `config-schema`**，`config.json` 作为 legacy 层传入 | 根治「上游加 knob、omp 手工补」这类漏接（已发生 1 次） |
| D3 | 三个已知缺陷一起修：`bypassPatterns` 生效、`write`/`edit` 指向 `viking://` 拦截、`bash` 含 `viking://` 追加 notice | 前两个是「配置写了但不生效」的静默失败，第三个是缺事件处理 |
| D4 | `DESIGN.md` **外科修正** + 新增「omp 与 pi 差异」章节 | 911 行里大部分（recall/sync/takeover 内部机制）仍然准确，但 `index_builder.ts` 等 6 处引用磁盘上不存在 |
| D5 | 验收对齐 pi 的单测覆盖，现有 56 个测试保持全绿 | 与仓库既有标准一致 |
| D6 | **能力也对齐 pi**（不只配置层）：sync 批量提交、结构化 debug-log、bypass 双 knob、peer 有效解析 | 只对齐配置层会留下「同一个 knob 在两个 harness 里行为不同」的新坑 |
| D7 | 未实现的 knob 启动时警告一次；`uninstall_omp` 只清 mcp.json + 扩展目录 | 不让任何 knob 静默失效；不碰用户改过的 `config.json` |

**净效果**：删约 400 行手写代码（`tools.ts` 266 行 + `client.ts` 中只为工具存在的 REST 方法 + 自定义守卫 hint 表），换 26 行 stdio 代理 + 配置层迁移，工具面从 7 个手写工具变成服务端 16 个工具。

---

## 2. 事实基线（全部实测，非推断）

### 2.1 omp 18.2.10 可用的原生能力

| 能力 | 位置 | 对本设计的意义 |
|------|------|---------------|
| MCP 客户端（stdio / http / sse） | `src/config/mcp-schema.json` `$defs.serverConfig` | 扩展不必自带 MCP client |
| stdio server 字段：`command`(必填) / `args` / `env` / `cwd`，`additionalProperties: false` | 同上 `$defs.stdioServer` | mcp.json 条目不能塞自定义标记字段 |
| http server 字段：`url`(必填) / `headers` | 同上 `$defs.httpServer` | 备选路线（未采用） |
| `headers` / `url` 支持 `${ENV}` 展开 | `src/discovery/mcp-json.ts:113-121` | 备选路线（未采用） |
| MCP 工具名 `mcp__<sanitized_server>_<tool>`，上限 64 字符，超长加 base36 后缀 | `src/mcp/tool-bridge.ts:406-458`、`:440` | 本设计最长名 `mcp__openviking_cancel_watch`(29)，不会截断 |
| `ToolLoadMode = "essential" \| "discoverable"` | `packages/agent/src/types.ts:952` | 决定工具是否进顶层 schema |
| 未声明 `loadMode` 时：内置工具 `essential`，适配器边界（扩展/自定义/MCP）默认 `discoverable` | `src/tools/essential-tools.ts:45` | MCP 工具默认按需 |
| `tools.xdev` 默认 `true`；`tools.xdevDocs` 默认 `"builtins"`（「MCP and extension tools stay on-demand」） | `src/config/settings-schema.ts:4771,4783` | **16 个工具默认零顶层 prompt 成本** |
| `tools.xdevInlineDevices`：glob 数组，可把匹配的设备钉进顶层 | `src/config/settings-schema.ts:4805` | 想钉热工具时的现成开关 |
| ExtensionAPI 事件：`tool_call`、`tool_result`、`session_start` 等 | `src/extensibility/extensions/types.ts` | 守卫与 notice 可挂 |
| ExtensionAPI **没有**「扩展注册 MCP server」的接口 | 同上（无 `registerMcpServer`） | 只能用 mcp.json 或自带 client |

### 2.2 pi 与 omp 差距矩阵

| 面 | pi | omp 现状 |
|----|----|---------|
| 工具 | 服务端 MCP 动态注册 16 个（`openviking_*`，含 write/edit/grep/glob/tree/add_skill/health/watches） | 手写 7 个 `viking_*`，是 MCP 面的真子集 |
| 配置 | shared `config-schema`（env → 工作区文件 → ovcli `plugin.pi` → 默认） | 手写 44 字段映射（上游每加 knob 就漏一次） |
| bypass | `isBypassed()` 在 `start()` 生效（跳过健康检查/session/profile） | 配置解析了但**从不使用**（全目录 0 引用） |
| 守卫 | write/edit 拦截 + bash notice + edit 输入窄化 | 只覆盖 read/grep/find/ls/bash，无 tool_result handler |
| 互斥标记 | `globalThis.__OPENVIKING_PI_EXTENSION__` | 无（omp 侧无同类风险，不做） |
| 依赖 | `package.json` + 安装时 `npm ci` | 无依赖（tar 拷贝） |

### 2.3 三个决定性事实

1. **omp 默认就让 MCP 工具按需发现**：`tools.xdev: true` + `tools.xdevDocs: "builtins"`。`read xd://` 列出设备，`write xd://<tool>` 调用。所以挂 16 个工具不会撑爆系统提示。
2. **仓库已有 stdio→HTTP MCP 代理**：`examples/*/servers/mcp-proxy.mjs`（26 行，零依赖，运行时经插件自己的 `loadConfig()` 读 `ovcli.conf`）。claude-code / opencode / agent-hook / dsh / agy / codex 都在用。omp 复用即可，**凭据不落 mcp.json、不随 token 轮换失效**。
3. **`sync.mjs` 按「插件代码的直接 import 闭包」同步 shared 文件**（`TARGETS` 里 omp 已登记，`committed: true`）。omp 缺的 8 个 shared 文件不是遗漏，而是 omp 还没 import 它们；改完 import 重跑生成器即可补齐。

### 2.4 服务端 MCP 工具面（权威清单，`openviking/server/mcp_endpoint.py:1897`）

16 个：`find, search, read, write, edit, list, tree, remember, add_resource, add_skill, list_watches, cancel_watch, grep, glob, forget, health`

→ omp 侧名称：`mcp__openviking_<tool>`。**注意 `add_skill` 在列**：这推翻了 `DESIGN.md:24`「omp 无 add_skill」的旧结论——迁移后 omp 继承服务端全部工具，共享守卫表里「skill URI 改写 → 用 add_skill」的分支对 omp 也成立了。

---

## 3. 目标与非目标

**目标**

1. omp 的工具面 = 服务端 MCP 面，服务端加工具时 omp 无需改代码。
2. 配置层与其它 harness 同源，knob 不再手工移植。
3. 三个静默失败缺陷修掉。
4. 文档与代码一致（`DESIGN.md`、`README.md`）。

**非目标**

1. 不追 pi 的 `__OPENVIKING_PI_EXTENSION__` 互斥标记（omp 无第二个扩展竞争）。
2. 不实现 pi 的 `mcpEnabled` 开关语义之外的新配置面（见 §4.2 inert knob 策略）。
3. 不改 omp 主程序，不改服务端。
4. 不动 `memory-plugin-shared/lib` 的算法与默认值。

---

## 4. 设计

### 4.1 工具面：MCP 直挂

**mcp.json 条目**（写入 `$(resolve_omp_agent_dir)/mcp.json`，即 `~/.omp/agent/mcp.json` 或 profile 目录）：

```json
{
  "mcpServers": {
    "openviking": {
      "type": "stdio",
      "command": "node",
      "args": ["<安装目录>/extensions/openviking/servers/mcp-proxy.mjs"]
    }
  }
}
```

- **合并语义**：读现有 JSON → 只增改 `mcpServers.openviking` → 保留其它 server（本机现有 `codebase-memory-mcp`）→ 原子写（临时文件 + rename，沿用 `install.sh` 现有 node 片段写法）。
- **managed marker**：`args[0]` 里的 `extensions/openviking/servers/mcp-proxy.mjs` 路径即标记（与 `install.sh` 里 cursor/trae 的 `grep -q 'mcp-proxy.mjs'` 检查同源）。卸载只删这个键，且仅当路径匹配。
- **卸载**：`install.sh` 目前**没有** `uninstall_omp`，需新增并从卸载派发处调用（删 mcp.json 条目 + 删扩展目录）。
- **字段面**：`stdioServer` 允许 `command`(必填) / `args` / `env` / `cwd`，外加 serverBase 的 `enabled` / `timeout` / `requestIdFormat` / `auth` / `oauth`（`mcp-schema.json` 的 `$defs.stdioServer` + serverBase）；两处都是 `additionalProperties:false`，**不能塞自定义标记字段**——所以 managed marker 只能靠 `args[0]` 路径。`enabled:false` 可当软开关（用户临时禁用不必删条目）。
- **多源冲突（未核实，必须实测）**：omp 同时读 `~/.omp/agent/mcp.json`、项目级 `.omp/mcp.json`、`mcp.json`、`.mcp.json`（`mcp-schema.json:4` 的描述）。若某个项目级文件也定义了 `openviking` server，omp 的合并/覆盖策略未知 → Phase 3 验收必须在含项目级 mcp.json 的目录里实测一次。

**代理入口** `servers/mcp-proxy.mjs`（新文件，~26 行，镜像 `opencode-plugin/servers/mcp-proxy.mjs`）：

```js
import { loadConfigForProxy } from "../lib/omp-config.mjs";
import { createLogger } from "../shared/debug-log.mjs";
import { createOpenVikingMcpProxy } from "../shared/mcp-proxy-core.mjs";
```

**工具呈现**：不改 omp 设置，全部 `discoverable`（omp 默认）。扩展在 session-start 注入块里加一行提示（条件：扩展自身健康检查通过 **且** mcp.json 里存在 openviking 条目 **且** `disabledServers` 未列 `openviking`——denylist 优先级最高，见 `mcp-schema.json:21`；用户禁用后不该再提示工具可用），告知可用工具与 `read xd://` 的发现方式。README 记录 `tools.xdevInlineDevices` 的自助钉法。

**工具删除**：`tools.ts` 整个删除；`client.ts` 中只为工具存在的 REST 方法（`overview()` 等）删除；`viking_archive_expand` 的语义由 `mcp__openviking_read` 读 `viking://session/<sid>` 覆盖（原实现就是 `client.overview("viking://session/<sid>")` + `/history` 回落）。

**OV 未运行时的降级（评审新增，P1）**

代理把 `initialize` 直转上游（`mcp-proxy-core.mjs:337` 在 `postToMcp` 里处理 `initialize`；本地拦截只覆盖 `tools/call`，见 `:207`）。因此 omp 启动时 OpenViking 未运行 → 握手失败 → 16 个工具**整体缺席**；而现状是 7 个手写工具始终注册、只在调用时报错。这是相对自身的行为倒退。

- **本期做法（omp 侧可见性兜底）**：`start()` 的健康检查失败时 `ctx.ui.notify` 明确提示「OpenViking 未运行 → viking 工具不可用，启动服务后重开会话」；install 完成时打印同一句；README 写明「OV 需常驻」。
- **不做的方案（记录为后续）**：让 `mcp-proxy-core.mjs` 本地应答 `initialize`。它是 shared 行为变更，影响所有走该代理的 harness；且 `tools/list` 仍需上游，要真正补齐得内置静态工具清单（有漂移风险），收益不足以承担这个风险。
- 判断依据：该契约（OV 常驻）对现有 6 个走 MCP 代理的 harness 同样成立，omp 并非新增独有缺陷，只是相对它自己的过去更敏感。

### 4.2 配置层：迁到 shared config-schema

**单一实现放 JS**（TS 的 `config.ts` 委托，代理也走同一份）：

```text
lib/omp-config.mjs          ← 新：loadConfig(cwd) / loadConfigForProxy(env)
config.ts                   ← 改：薄委托（保留 EXTENSION_VERSION / detectHarness 导出）
```

**目标形状**：与 pi 的实际做法一致（`pi-coding-agent-extension/config.ts` 103 行）——把 schema 结果整体铺开，只留少数几行 shim：

```ts
const config = buildPluginConfig("omp", {
  cwd, version: EXTENSION_VERSION,
  // plugin-config.mjs:210 是 `legacy || ovConfSection(ov, "omp")`：传了非空 legacy 就
  // 顶掉 ov.conf 的 plugin.omp 段。故显式合并两层，ov.conf 优先（与 §4.2 分层一致）。
  legacy: { ...readOmpConfigJson(), ...ovConfSectionFor("omp") },
  deriveEffectivePeer: true,
});
return {
  ...config,                                        // schema 78 个 knob 全量铺开
  bypassPatterns: config.bypassSessionPatterns,     // 旧名回指
  debugLogPath: config.debugLogPath || String(process.env.OV_DEBUG_LOG || "").trim(),
  peerId: config.effectivePeer.peerId,              // 有效 peer：旧 memory 仍可达
  ...ompAliases(config),                            // takeover.* 展开 + 旧名回指
} as OVConfig;
```

**分层结果**（白拿，与 pi/其它 harness 一致）：`env` → 工作区文件（`.openviking/…` + 注册表） → `ovcli.conf` 的 `plugin.omp` 段 → schema 默认值。

**omp 现有 44 字段 → schema knob 映射**：

| 类别 | omp 现有 | schema knob | 处理 |
|------|---------|------------|------|
| 直通同名 | `enabled` `recallLimit` `scoreThreshold` `minQueryLength` `recallTokenBudget` `recallMaxContentChars` `recallPreferAbstract` `recallPeerScope` `recallQueryExpansion` `recallLedger` `workspacePeer` `profileTokenBudget` `resumeContextBudget` `commitTokenThreshold` `commitKeepRecentCount` `skillCatalog` `skillCatalogTokenBudget` `captureToolResults` `captureMode` `captureMaxLength` `captureToolMaxChars` `captureAssistantTurns` `takeoverEnabled` `takeoverTokenThreshold` `takeoverKeepRecentTurns` `takeoverOverviewBudget` `takeoverOverviewPollMs` `takeoverOverviewPollMax` `logLevel` | 同名 | 直接对齐 |
| 别名（已内置，无需适配层） | `recallBudget` `profileBudget` `recallScoreThreshold` `recallMinQueryLength` `bypassPatterns` `syncTurns` | 同名 knob | **schema 已注册为别名**：`config-schema.mjs:80,81,82,122,147,187`；`resolveKnobs` 按别名回填 `configured`/`sources`，旧 `config.json` 继续生效 |
| 嵌套展开 | `takeover: {enabled, tokenThreshold, keepRecentTurns, overviewBudget, overviewPollMs, overviewPollMax}` | 扁平 6 个 `takeover*` | 适配层展开（schema 无嵌套形式，这才是适配层的真正职责） |
| 旧名回指 | `bypassPatterns` | `bypassSessionPatterns` | 输出侧回指（`config.ts` 内部旧读取点仍用旧名）；输入侧由内置别名覆盖 |
| 身份 | `endpoint` `apiKey` `account` `user` `peerId` `userAgent` | `apiKey` `accountId` `userId` `peerId` … | 以 `shared/credentials.mjs` 的 `resolveConnection()` 为准（omp 已在用），字段名随 schema |
| 已核实 | `syncTurns`（每 N 轮同步） | `autoCapture` 的注册别名 | **已是别名**（`config-schema.mjs:115-122`，注释写明 `syncTurns` 是 dsh/pi 的拼法），语义一致，**不需要** omp 私有键 |
| 内部 | `recallQueryExpansionConfigured` `recallLimitConfigured` | — | omp 内部状态位，迁移后由 schema 的 `configured`/`sources` 取代 |
| 新增语义 | `mcpEnabled` | `mcpEnabled` | 迁移后工具归 harness 管，该 knob 只能控制**工具提示行**是否注入，README 需写明（否则又是一个死键） |

**inert knob 策略**（见 §8 未决项 Q1）：schema 有 78 个 knob，omp 目前消费 44 个。采用 schema 后其余 knob 可被用户设置却不生效——这正是 `bypassPatterns` 那类 bug 的温床。设计默认做法：启动时把「用户显式配置但 omp 未消费」的 knob 收集起来，用 `ctx.ui.notify` 报一次（沿用 schema 层已有的 `configured`/`sources`/`warnings` 返回值），不静默吞掉。

### 4.3 守卫：保留最小 hint 表，但文案给全名

共享 `shared/uri-guard.mjs` 的 `DEFAULT_TOOL_HINTS` 覆盖 `read/glob/grep/edit/write/bash/runcommand/shell`，并对 skill URI 自动切 `add_skill` 分支；但它的示例文案是**裸名**（`uri-guard.mjs:122-125`：`read(uris="…")`），而 omp 切 MCP 后工具全名是 `mcp__openviking_read`。照抄裸名会让模型去调内置 `read`——恰好是被守卫拦下的那个工具。所以**不能整个删除**自定义表；pi 也没删（`pi-coding-agent-extension/lib/uri-guard-adapter.mjs:9-44` 保留了自己的表，因为 pi 的桥接工具叫 `openviking_*`）。

做法：

- 保留 `VIKING_URI_TOOL_HINTS`，`tool`/`example` 文案改成**全名**（`mcp__openviking_read` / `_grep` / `_glob` / `_list` / `_write` / `_edit` / `_search`）。
- 补齐当前缺的条目：`write`、`edit`（现状只有 read/grep/find/ls/bash）。
- `write`/`edit` 条目**必须保留 skill 分支**（`isSkillUri(uri) ? "mcp__openviking_add_skill" : …`），否则覆盖共享表会丢掉 `add_skill` 提示（`uri-guard.mjs:103-120` 的 `isSkillUri`/`addSkillExample`）。
- 调用时合并两层，omp 的精确名覆盖共享默认：`evaluateUriGuard(toolName, input, { hints: { ...DEFAULT_TOOL_HINTS, ...VIKING_URI_TOOL_HINTS } })`。
- `guardVikingUriToolCall` → 返回 `{ block: true, reason }`；**新增** `noticeVikingUriToolResult`（pi 的同名函数在 `lib/uri-guard-adapter.mjs:71-78`，**不在 shared**）→ `evaluateUriNotice(...)`，把 notice 追加到 `tool_result` 的 content 之后（原 content 必须保留）。
- 保留 pi 的 `narrowGuardInput`：omp 的 `edit` 输入形状与 pi 同源，`{path, edits:[{oldText,newText}]}` 的替换文本会被通用扫描误判为位置，只放行 `path`（**实现时按 omp 内置 edit 的实际入参核对**）。

这一处同时修掉 D3 的两个守卫缺陷：`write`/`edit` 进入守卫表（拦截），`bash` 走 notice 分支。

### 4.4 bypass

`index.ts` 的 `start()` 在健康检查前加：

```ts
if (isBypassed(config, { cwd: process.cwd() })) { bypassed = true; started = true; return; }
```

与 pi 一致：bypass 命中则不做健康检查、不建 session、不注入 profile、不同步。注意 bypass **不影响** MCP 工具可用性（工具归 harness 管）——这是 D1 已接受的代价，README 需写明。

### 4.5 文档

- `DESIGN.md`：删掉 `index_builder.ts` 的 6 处引用与「6 文件架构 / 7 工具」等失实描述；文件清单与工具表按现状改；**新增「omp 与 pi 差异」章节**（工具面、配置层、xdev 呈现、无互斥标记、依赖差异）。
- `README.md`：工具表换成 16 个 `mcp__openviking_*`；安装章节加 mcp.json 说明与 `read xd://` 用法；加 `tools.xdevInlineDevices` 自助钉法；加「旧 `viking_*` 名字 → 新工具」映射表。

### 4.6 能力对齐清单（pi 有、omp 没有）

实测对比（排除 `shared/`、`tests/`）：omp **已经**有 `lib/capture-adapter.mjs`（88 行，与 pi 同）和 `lib/takeover-core.mjs`（406 行，与 pi 同），这两块不是缺口。真正的差距是 4 项：

| # | 能力 | pi 的实现 | omp 现状 | 处理 |
|---|------|----------|---------|------|
| A1 | **批量提交 / 批量重放** | `syncBranch` 进 `sendPayloads`（`shared/batch-send.mjs`），`flushForTakeover` 进有界的 `drainSessionBacklog`（同 `BATCH_LIMIT`）；只有启动时的 `replayPending()` 仍是一请求一条 | `syncBranch` 逐条 `for (const payload of extracted.payloads)` 发；`flushForTakeover` 调共享 `replayPending()`，一条一个请求 | 两条批量路径都接：`syncBranch` → `sendPayloads`，`flushForTakeover` → `drainSessionBacklog`；启动路径的 `replayPending()` 保持共享实现不动 |
| A2 | **结构化 debug 日志** | `shared/debug-log.mjs` 的 `createLogger("pi", {debug, debugLogPath})`，knob `debug`/`debugLogPath` + `OV_DEBUG_LOG` 旧名 | 内联 6 行 `debugLog` 闭包，无日志文件 | 换成 `createLogger("omp", …)` |
| A3 | **bypass 双 knob** | `bypassSession`(bool) + `bypassSessionPatterns`(array)，`isBypassed()` 在 `start()` 生效 | 只有 `bypassPatterns`；手写的 `matchBypass()` 是「裸路径 = 前缀」语义 | 改用 `isBypassed()` 并删掉 `matchBypass()`；**语义变化**：模式是锚定 glob，`/tmp/work` 不再覆盖 `/tmp/work/sub`，要写 `/tmp/work/**`（见 §6） |
| A4 | **有效 peer 解析** | `deriveEffectivePeer: true` → `peerId = config.effectivePeer.peerId` | `resolveEffectivePeerId()` 自行解析 | 改用 schema 的有效 peer（旧 memory 在 `actor` scope 下仍可达） |

**明确不做**（不属对齐目标）：

- `lib/mcp-bridge.mjs` / `lib/mcp-result.mjs`：omp 走原生 MCP，不需要进程内 bridge。
- `globalThis.__OPENVIKING_PI_EXTENSION__` 互斥标记：omp 无第二个扩展竞争。
- `package.json` / `npm ci`：omp 无依赖。
- 其它 harness 专属的 knob 消费者（`recallCompress*` 的压缩器接线、`resumeArchive*`、`repoContext`、`sessionStartMaxBytes` 等）：pi 侧同样没有实现，属共享库能力，不是 omp 的差距。

---

## 5. 文件级改造清单

**新增**

| 文件 | 内容 |
|------|------|
| `examples/omp-openviking-extension/lib/omp-config.mjs` | JS 配置加载器（`buildPluginConfig` + legacy 两层合并 + `takeover.*` 展开 + inert 清单） |
| `examples/omp-openviking-extension/servers/mcp-proxy.mjs` | stdio→HTTP 代理入口（~26 行） |
| `examples/omp-openviking-extension/tests/config-migration.test.mjs` | 旧 config.json 键仍生效、别名、`plugin.omp` 段、inert 警告 |
| `examples/omp-openviking-extension/tests/uri-guard.test.mjs` | 更新既有：write/edit 拦截、bash notice |
| `examples/memory-plugin-shared/install-mcp-json.test.mjs` | mcp.json 合并/保留他人条目/卸载只删自己的条目 |

**修改**

| 文件 | 改动 |
|------|------|
| `examples/omp-openviking-extension/sync.ts` | 提交与重放路径改用 `shared/batch-send.mjs`（A1） |
| `examples/omp-openviking-extension/index.ts` | 加 bypass（含 `bypassSession`）；删工具注册；加 `tool_result` handler；加工具提示行；换 `createLogger`（A2）；inert knob 警告 |
| `examples/omp-openviking-extension/config.ts` | 薄委托到 `lib/omp-config.mjs`；`peerId` 改用有效 peer（A4） |
| `examples/omp-openviking-extension/lib/uri-guard-adapter.mjs` | hint 表文案改**全名**（`mcp__openviking_*`）+ 补 `write`/`edit`（含 skill 分支）；加 `noticeVikingUriToolResult`；加 edit 窄化 |
| `examples/omp-openviking-extension/client.ts` | 删工具专用 REST 方法 |
| `examples/omp-openviking-extension/shared/*` | 由 `sync.mjs` 生成（新增约 8 个文件：`config-schema` `plugin-config` `workspace-config` `workspace-registry` `mcp-proxy-config` `mcp-proxy-core` `debug-log` 及类型声明） |
| `examples/memory-plugin-shared/install.sh` | `install_omp` 加 mcp.json 合并；新增 `uninstall_omp`；接进卸载派发 |
| `examples/memory-plugin-shared/lib/config-schema.mjs` | `HARNESS_KEYS` 加 `omp: "omp"`（1 行）→ `HARNESS_CONFIG_KEYS` 自动派生（`config-schema.mjs:259`）；否则 doctor 把 `plugin.omp` 段当未知键误报（`doctor-core.mjs:351,1166`） |
| `examples/memory-plugin-shared/lib/plugin-config.mjs` | 导出 `ovConfSection`（1 行，`plugin-config.mjs:161` 现为私有），供 omp 显式合并 legacy 层 |
| `examples/omp-openviking-extension/README.md`、`DESIGN.md` | 见 §4.5 |

**shared 改动边界**：本设计对 `memory-plugin-shared/lib` 只做两处**注册表 / 可见性**改动（`HARNESS_KEYS` 加一行、`ovConfSection` 加 export），不碰算法与默认值。改后必须重跑 `sync.mjs`，并确认其它 harness 的 `shared/` 只多不少（`git status` 里不应出现其它 harness 的 diff）。

**删除**

| 文件 | 行数 |
|------|------|
| `examples/omp-openviking-extension/tools.ts` | ~266 |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 工具名从 `viking_*` 变成 `mcp__openviking_*`，用户既有 prompt/skill 引用失效 | README 加旧→新映射表；守卫文案直接给新名字 |
| bypass 模式语义变化：裸路径不再当前缀 | 手写 `matchBypass()` 把 `/tmp/work` 当目录前缀，共享 matcher 是锚定 glob；`/tmp/work` 现只匹配自身。README 写明用 `/tmp/work/**`，旧 `config.json` 里写裸目录的用户需加 `**` |
| MCP 工具默认按需，模型可能不主动 `read xd://` 发现 | session-start 提示行（仅在健康检查通过且条目存在时注入）；README 给 `tools.xdevInlineDevices` 钉法 |
| 安装器写用户全局 `mcp.json` | 只增改 `mcpServers.openviking`、原子写、路径即标记、提供 `uninstall_omp` 精确回滚 |
| `node` 不在 omp 运行环境的 PATH 上 | 安装时探测并写绝对路径（若已有 `NODE_BIN` 变量则复用） |
| 配置迁移后旧 `config.json` 失效 | `config.json` 作为 legacy 层传入 + 别名后置映射 + 专门的迁移测试 |
| 语义不清的 knob（如 `syncTurns`）被错误映射 | 标为「实现时核实」，不确定就保留为 omp 私有键，不硬映射 |
| 迁移后引入未实现的 knob 变成新的静默失败 | inert knob 警告（§4.2） |
| **OV 未运行时 16 个工具整体缺席**（比现状的 7 个常驻工具倒退） | omp 侧提示兜底（§4.1）；README 写明 OV 需常驻；根治（改 shared 代理本地应答 `initialize`）列为后续，不在本期 |
| 项目级 `mcp.json` 也定义了 `openviking` → 与全局条目冲突（**未核实**） | Phase 3 必须在含项目级 mcp.json 的目录里实测；README 说明多源优先级 |
| 用户用 `disabledServers` 禁用后仍被提示「工具可用」 | 提示行条件加 `disabledServers` 检查（denylist 优先级最高） |
| 回滚 / 卸载后 `mcp.json` 残留条目指向已删的 `mcp-proxy.mjs` → 每次 omp 启动 spawn 报错 | Phase 3 回滚步骤必须跑 `uninstall_omp` 清条目（或恢复 Phase 0 备份）；`uninstall_omp` 仅按 `args` 路径匹配才删 |
| `config.json.bak.<ts>` 若写在扩展目录内会随目录一起被删 | 备份写到扩展目录**之外**（如 `$(resolve_omp_agent_dir)/config.json.bak.<ts>`） |

---

## 7. 验收矩阵

| 面 | 验收 |
|----|------|
| 工具面 | 装完后 `~/.omp/agent/mcp.json` 含 openviking 条目且其它条目不变；omp 会话里 `read xd://` 能列出 16 个 `mcp__openviking_*`；`node <dest>/servers/mcp-proxy.mjs` 能起并完成 MCP 握手 |
| 配置层 | `config.json` 旧键（含 `recallBudget`、`takeover.*`、`bypassPatterns`）仍生效；`OPENVIKING_*` 环境变量生效；`ovcli.conf` 的 `plugin.omp` 段生效；优先级顺序有测试覆盖；用户设了 omp 未消费的 knob 时启动报一次警告 |
| 能力对齐 | 提交 20+ 条消息时按 `BATCH_LIMIT` 分请求（用 fake fetch 断言请求数）；`debugLogPath` 生效后日志文件出现；`peerId` 取自有效 peer |
| 守卫 | `write`/`edit` 指向 `viking://` 被拦；`bash` 含 `viking://` 结果后追加 notice 且原内容保留；skill URI 提示指向 `add_skill` |
| bypass | 命中 `bypassPatterns` 时不健康检查、不建 session、不注入 profile |
| 卸载 | `uninstall_omp` 后 mcp.json 只剩他人条目，扩展目录删除 |
| 回归 | 现有 56 个测试全绿；`node --test tests/*.test.mjs` 与 `sync.mjs` 幂等（`git status` 不变） |
| OV 未起 | 停掉 OpenViking 再起 omp → 出现明确提示，**不静默无工具** |
| 回滚 | revert + `uninstall_omp` 后：mcp.json 无残留 openviking 条目、扩展目录消失、omp 不再尝试 spawn 已删代理 |
| doctor | `plugin.omp` 段不再被报成未知键（`HARNESS_KEYS` 改动生效） |
| 多源 | 含项目级 `mcp.json` 的目录里启动，确认 server 解析结果与预期一致（未核实项定案） |

---

## 8. 已决项记录（原未决项）

1. **inert knob 策略** → 启动时把「用户显式配置但 omp 未消费」的 knob 收集起来，`ctx.ui.notify` 报一次（沿用 schema 层的 `configured`/`sources`/`warnings`）。
2. **范围** → **全接**（见 §4.6）：配置层对齐 + A1~A4 四项能力对齐。
3. **文档位置** → `docs/design/`，design + plan 成对（本文件 + `omp-plugin-native-parity-plan.md`）。
4. **卸载范围** → `uninstall_omp` 只清 `mcp.json` 的 openviking 条目（路径匹配才删）+ 删扩展目录；`config.json` 保留。

### 评审后新增（kimi-k3 + glm-5.3 双轮，2026-09-23）

- **OV 未运行降级** → 本期只做 omp 侧提示兜底；改 `mcp-proxy-core.mjs` 本地应答 `initialize` 列为后续（见 §4.1）。
- **shared 改动边界** → 只允许「注册表 / 可见性」类改动（`HARNESS_KEYS` 一行 + `ovConfSection` export），不改算法与默认值。
- **守卫 hint 文案** → 保留自定义表但用全名 `mcp__openviking_*`，不采用共享表的裸名（会让模型照抄到内置工具）。
- **`syncTurns`** → 撤回「语义待核」：已是 `autoCapture` 的注册别名，无需私有键。
