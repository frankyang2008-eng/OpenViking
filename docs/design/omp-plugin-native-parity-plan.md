# omp 插件原生能力对齐实施计划

- **日期**：2026-09-23
- **状态**：Phase 1–5 已完成（本地提交），Phase 6 真机验证待执行；逐阶段状态见各 Phase 标题下（设计见 `omp-plugin-native-parity-design.md`）
- **分支**：`ov-dev-opt`（本地提交，不 push）
- **总工期估算**：12–18 小时（6 个阶段，每阶段独立可验证）

---

## 0. 执行约定

| 项 | 约定 |
|----|------|
| 测试 | `cd examples/omp-openviking-extension && node --test tests/*.test.mjs`（当前基线：56 pass / 0 fail） |
| shared 同步 | `node examples/memory-plugin-shared/sync.mjs`（改完 import 后必须跑；跑完 `git status` 不应再有变化 = 幂等） |
| 安装 | `bash examples/memory-plugin-shared/install.sh --harness omp --lang zh` |
| 卸载 | `bash examples/memory-plugin-shared/install.sh --harness omp --uninstall` |
| 生成物纪律 | `shared/` 下带 `// GENERATED FROM …` 首行的文件**禁止手改**，只能改 `memory-plugin-shared/lib` 后重跑 sync |
| shared 改动边界 | 本期只允许两处**注册表 / 可见性**改动（`config-schema.mjs` 的 `HARNESS_KEYS` 加 `omp` 一行、`plugin-config.mjs` 导出 `ovConfSection`）；改后重跑 sync，确认**其它 harness 的 `shared/` 无 diff** |
| 提交 | 每阶段一个 commit（`feat/fix/docs/chore(omp): …`），本地提交不 push |
| 高风险点 | Phase 3（工具面切换）——它是唯一会改变用户可见行为的阶段，前两阶段纯内部 |

**Phase 0 前置检查（15 分钟）**

1. `git status` 干净，`git log -1 --oneline` 记录起点。
2. `diff -rq --exclude=node_modules --exclude=config.json --exclude=.omc ~/.omp/agent/extensions/openviking examples/omp-openviking-extension` → 应无差异。
3. 备份 `~/.omp/agent/mcp.json` → `mcp.json.bak.$(date +%Y%m%d-%H%M%S)`。
4. 开一个新 omp 会话，确认 session-start 注入块里出现 `<available-skills>`（上一轮的遗留验证项，属现状确认）。

---

## Phase 1 — 配置层迁移（D2 + A2 + A4）

> **状态**：✅ 已完成（`c783eba05` `47a2e8494` `f4740f5b2`）

**目标**：omp 的配置来源与其它 harness 同源；不再手工移植 knob。

**改动**

1. 新建 `examples/omp-openviking-extension/lib/omp-config.mjs`（JS，代理与扩展共用）：
   - `readOmpConfigJson()`：读扩展目录旁的 `config.json`（保留现状：`logLevel` 等）
   - `loadConfig(cwd)`：`buildPluginConfig("omp", {cwd, version, legacy, deriveEffectivePeer: true})` + §4.2 的 shim（`bypassPatterns` 回指、`debugLogPath` 回落 `OV_DEBUG_LOG`、`peerId = config.effectivePeer.peerId`）+ 适配层（**只做 `takeover.*` 嵌套展开**；`recallBudget`/`profileBudget`/`bypassPatterns`/`recallScoreThreshold`/`recallMinQueryLength`/`syncTurns` 都已是 schema 内置别名，**不要重复搬运**）
   - **legacy 必须显式合并两层**：`{ ...readOmpConfigJson(), ...ovConfSection(ovFile, "omp") }`。`plugin-config.mjs:210` 是 `legacy || ovConfSection(...)`，只传 `config.json` 会**顶掉** `ov.conf` 的 `plugin.omp` 段。
   - `collectInertKnobs(config)`：返回「用户显式配置但 omp 未消费」的 knob 列表（用 schema 的 `configured`/`sources`）
   - `loadConfigForProxy(env)`：供 stdio 代理使用（Phase 3 接）
2. shared 两处小改（注册表 / 可见性，不改算法）：
   - `memory-plugin-shared/lib/config-schema.mjs`：`HARNESS_KEYS` 加 `omp: "omp"`（`HARNESS_CONFIG_KEYS` 自动派生，`config-schema.mjs:259`）——否则 doctor 把 `plugin.omp` 段当未知键误报（`doctor-core.mjs:351,1166`）。
   - `memory-plugin-shared/lib/plugin-config.mjs`：导出 `ovConfSection`（`plugin-config.mjs:161` 现为私有函数）。
3. `config.ts` 改成薄委托（保留 `EXTENSION_VERSION` / `detectHarness` / `DEFAULT_CONFIG` 的导出面，测试不改引用路径）。
4. `index.ts`：内联 `debugLog` 换成 `createLogger("omp", {debug, debugLogPath})`；启动时 `collectInertKnobs()` 非空则 `ctx.ui.notify` 报一次。
5. 跑 `node examples/memory-plugin-shared/sync.mjs` → 生成 `shared/config-schema.mjs`、`shared/plugin-config.mjs`、`shared/workspace-config.mjs`、`shared/workspace-registry.mjs`、`shared/debug-log.mjs` 及 `.d.mts`。

**验收**

- `node --test tests/*.test.mjs` 全绿（含新增 `tests/config-migration.test.mjs`）。
- 新增测试覆盖：旧 `config.json` 键生效；`recallBudget`→`recallTokenBudget`；`takeover.*` 嵌套展开；`OPENVIKING_RECALL_LIMIT` 生效；`ovcli.conf` 的 `plugin.omp` 段生效；优先级 env > 工作区 > `plugin.omp` > `config.json` > 默认；inert knob 被识别。
- **双源同时生效（专守 `plugin-config.mjs:210` 的 `legacy ||` 陷阱）**：`config.json` 与 `ov.conf` 的 `plugin.omp` 段同时存在时，`plugin.omp` 的值胜出，且 `config.json` 里独有的键仍生效。
- doctor 不再把 `plugin.omp` 段报成未知键（`HARNESS_KEYS` 改动生效）。
- 手动：`OPENVIKING_RECALL_LIMIT=3` 起 omp，`/viking` 状态行显示 3。

**回滚**：单 commit revert；`config.json` 未被改动，旧代码可继续读它。

---

## Phase 2 — 能力对齐（A1 批量提交 + A3 bypass）

> **状态**：✅ 已完成（`1aa374a22`）

**目标**：同一个 knob 在两个 harness 里行为一致。

**改动**

1. `sync.ts`：接 `shared/batch-send.mjs`（`BATCH_LIMIT` + `sendSessionMessages`）——`syncBranch` 走 `sendPayloads`，`flushForTakeover` 走有界的 `drainSessionBacklog`；启动时的 `replayPending()` 保持共享实现（一请求一条）不动。同文件把自写的 `debugLog` 闭包换成 `createLogger("omp", …)`。
2. `index.ts`：`start()` 里把 bypass 的 for 循环换成 `isBypassed(config, {cwd})`，删掉手写的 `matchBypass()`（语义从「裸路径 = 前缀」变成锚定 glob，见设计 §6）。
3. 跑 sync（`shared/batch-send.mjs` 应被拉进来）。

**验收**

- 新增测试：一轮多消息只发 **1** 个请求（旧实现一条一个请求）；超过 `BATCH_LIMIT` 时按批拆（断言首批 = `BATCH_LIMIT` 且各批之和 = `added`）；`bypassSession` 与 glob 模式命中/不命中；裸路径不再覆盖子目录（记录语义变化）。
- 受影响的旧测试：两个 commit 日志测试改为传 `debugLogPath`（logger 读解析后的配置，不再直读 `OV_DEBUG_LOG`）；`restoreWatermark` 测试改为断言批量请求体（不再数 `addMessagePayload`）。
- 手动：在 `bypassSessionPatterns` 里写当前仓库路径，起 omp → 无 OpenViking 会话创建。

**回滚**：单 commit revert。

---

## Phase 3 — 工具面切 MCP（D1 + D3 的两项守卫）

> **状态**：✅ 已完成（`60c353fd3`）

**目标**：omp 的工具面 = 服务端 16 个 MCP 工具，手写工具下线。

**改动**

1. 新建 `servers/mcp-proxy.mjs`（~26 行，镜像 `examples/opencode-plugin/servers/mcp-proxy.mjs`），依赖 `lib/omp-config.mjs` 的 `loadConfigForProxy`。
2. 跑 sync → 生成 `shared/mcp-proxy-core.mjs`、`shared/mcp-proxy-config.mjs`。
3. 删除 `tools.ts`；删除 `client.ts` 中只为工具存在的 REST 方法（`overview()` 等，删前逐个确认无其它调用方）。
4. `index.ts`：删工具注册调用；加 session-start 工具提示行（条件：健康检查通过 **且** `mcp.json` 里存在 openviking 条目 **且** `disabledServers` 未列 `openviking` **且** `mcpEnabled !== false`）；健康检查失败时 `ctx.ui.notify` 明确提示「OpenViking 未运行 → viking 工具不可用」（§4.1）。
5. `lib/uri-guard-adapter.mjs`：**保留** hint 表但文案改全名 `mcp__openviking_*`，补 `write`/`edit`（含 skill→`add_skill` 分支）；调用时 `hints: { ...DEFAULT_TOOL_HINTS, ...VIKING_URI_TOOL_HINTS }`；新增 `noticeVikingUriToolResult`（原 content 必须保留）；保留 edit 输入窄化（**先核对 omp 内置 edit 的实际入参形状**）。
6. `index.ts`：接 `tool_result` 事件调 `noticeVikingUriToolResult`。

**验收**

- `node <repo>/examples/omp-openviking-extension/servers/mcp-proxy.mjs` 能启动并完成 MCP 握手（用一个 fake 请求或直接连真服务端）。
- 新 omp 会话：`read xd://` 列出 16 个 `mcp__openviking_*`；`write xd://mcp__openviking_health` 能调通。
- 守卫测试：`write`/`edit` 指向 `viking://` 被拦；`bash` 含 `viking://` 结果后追加 notice 且原内容保留；skill URI 提示指向 `add_skill`。
- `node --test tests/*.test.mjs` 全绿（守卫测试已更新）。
- 停掉 OpenViking 再起 omp → 出现明确提示，不静默无工具。
- 在含项目级 `mcp.json` 的目录里启动一次，确认 server 解析结果（未核实项定案）。

**回滚**：revert commit；`tools.ts` 从 git 恢复；**再跑一次 `bash examples/memory-plugin-shared/install.sh --harness omp --uninstall` 清掉 `mcp.json` 条目**（否则残留条目指向已删的 `servers/mcp-proxy.mjs`，omp 每次启动 spawn 报错），然后重跑 install 把旧代码装回 `~/.omp`。

---

## Phase 4 — 安装 / 卸载（D1 + D7）

> **状态**：✅ 已完成（`f7c6100e0` + 收尾提交）

**目标**：装完即用，卸完干净。

**改动**

1. `install_omp()`：tar 拷贝后合并 `$(resolve_omp_agent_dir)/mcp.json`（读 → 只增改 `mcpServers.openviking` → 保留其它 server → 临时文件 + rename 原子写）。`command` 优先写探测到的绝对 node 路径（复用现有 `NODE_BIN`），否则写 `node`。
2. 新增 `uninstall_omp()`：仅当 `args` 路径匹配 `extensions/openviking/servers/mcp-proxy.mjs` 时删该条目；删扩展目录；**保留**用户改过的 `config.json` —— 备份到扩展目录**之外**的 `$(resolve_omp_agent_dir)/config.json.bak.<ts>`（写在目录内会随目录一起被删）。
3. 把 `uninstall_omp` 接进 `uninstall_agent_integrations()`（`--uninstall` 分支）。
4. 新增 `examples/memory-plugin-shared/install-mcp-json.test.mjs`。

**验收**

- 测试：合并保留他人条目（含本机现有 `codebase-memory-mcp`）；JSON 非法时报错不破坏原文件；卸载只删自己的条目。
- 手动：装 → `cat ~/.omp/agent/mcp.json` 正确；卸 → 只剩 `codebase-memory-mcp`，扩展目录消失。

**回滚**：revert commit + 从备份恢复 mcp.json。

---

## Phase 5 — 文档（D4）

> **状态**：✅ 已完成（收尾提交）

**改动**

1. `examples/omp-openviking-extension/DESIGN.md`：删 `index_builder.ts` 的 6 处引用与「6 文件架构 / 7 工具」失实描述；文件清单与工具表按现状改；新增「omp 与 pi 差异」章节（工具面、配置层、xdev 呈现、无互斥标记、无依赖）。
2. `examples/omp-openviking-extension/README.md`：工具表换成 16 个 `mcp__openviking_*`；安装章节加 mcp.json 说明与 `read xd://` 用法；加 `tools.xdevInlineDevices` 自助钉法；加「旧 `viking_*` → 新工具」映射表（**7 个旧工具全覆盖**：`viking_browse` → `mcp__openviking_list`，`viking_archive_expand` → `mcp__openviking_read`，其余同理）；写明 `mcpEnabled` 在 omp 的新语义（只控制提示行）。

**验收**：文档内不再出现磁盘上不存在的文件名；README 的工具表与 §2.4 权威清单一致（16 个）。

---

## Phase 6 — 端到端验证与提交

> **状态**：⏳ 待真机验证

1. `node --test tests/*.test.mjs` 全绿；`node examples/memory-plugin-shared/sync.mjs` 后 `git status` 不变（幂等）。
2. `diff -rq` 安装目录与仓库一致。
3. 真机：新 omp 会话 → session-start 注入块含 `<available-skills>`；`read xd://` 见 16 个工具；一次 recall + 一次 capture + 一次 commit 正常；`/viking` 状态行正确。
4. bypass 目录里起 omp → 无会话创建、无注入。
5. 按阶段拆 commit 提交（本地，不 push）；提交信息用 `feat(omp):` / `fix(omp):` / `docs(omp):`。

---

## 7. 阶段依赖与顺序

```text
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6
             └──────────┴───────────┘  (1、2 可并行；3 依赖 1 的 lib/omp-config.mjs)
```

- Phase 3 依赖 Phase 1 的 `loadConfigForProxy`。
- Phase 4 依赖 Phase 3 的 `servers/mcp-proxy.mjs` 路径存在。
- Phase 5 无代码依赖，可提前做。

---

## 8. 执行中可能踩的坑

| 坑 | 处理 |
|----|------|
| `sync.mjs` 拉进来的 `.d.mts` 与实际导出不符 | 只信 `.mjs`；类型声明是生成物，若报错则同步上游 lib 修复后再生成 |
| omp 的 `edit` 入参形状与 pi 不同 → 守卫误拦 | 先读 omp 内置 edit 工具源码确认，再定 `GUARD_INPUT_KEYS_BY_TOOL` |
| 以为 `syncTurns` 需要保留为私有键 | 它已是 `autoCapture` 的注册别名（`config-schema.mjs:122`），直接对齐 |
| 只传 `config.json` 当 legacy，顶掉 `ov.conf` 的 `plugin.omp` 段 | 显式合并 `{ ...config.json, ...ovConfSection(ov, "omp") }`（`plugin-config.mjs:210` 是 `legacy \|\| ovConfSection(...)`） |
| 项目级 `mcp.json` 也定义 `openviking` → 多源冲突（未核实） | Phase 3 实测；README 写明优先级 |
| 卸载 / 回滚后 mcp.json 残留死条目 | 回滚必须跑 `uninstall_omp`；条目仅在 `args` 路径匹配时删 |
| 守卫文案用共享表的裸名 `read(uris=…)` | 模型会去调内置 `read`（正是被拦的工具）——文案必须给全名 `mcp__openviking_*` |
| omp 启动时 `node` 不在 PATH | 安装器写绝对路径；代理入口不依赖 PATH |
| 配置迁移后 `config.json` 的未知键被 schema 丢弃 | legacy 层保留原始键；inert 警告负责告知 |
| 工具名变化导致既有 prompt/skill 失效 | README 映射表；守卫文案直接给新名字 |
