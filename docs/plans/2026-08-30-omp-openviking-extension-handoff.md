# omp OpenViking 扩展维护 Handoff（omp 专属通道）

日期：2026-08-30
状态：Bug #1（`buildContextEntries` 崩溃）已修复并冒烟通过；Bug #2（systemPrompt 数组污染）已定位待修。
用户决策：omp 的 openviking 插件**独立维护，不与 pi 共用**（避免跨 harness 补丁互相打架）。

## 背景与根因（已实证）

omp（oh-my-pi，pi 的 fork）中加载 `~/.omp/agent/extensions/openviking/` 扩展，每次 LLM 请求报：

```
ctx.sessionManager.buildContextEntries is not a function
```

证据链：

1. 崩点：`index.ts:181`（context 钩子内）裸调 `ctx.sessionManager.buildContextEntries()`。
2. 该调用来自 OpenViking 上游提交 `69dd1d65`（#4237 "key recall ledger by stable entry ids"，2026-08-24，Zayn Jarvis）。
3. `buildContextEntries` 是 **pi（earendil-works/pi）的新 API**（GitHub 证实 pi 仓库 8 处存在，含官方 docs/extensions.md）。omp fork 未跟进该 API：
   - omp 本地 clone `/Users/frankyang-mp2/Desktop/work/dev_proj/omp`（v18.0.11+31）：`git log -S buildContextEntries --all` 零命中
   - 运行时二进制 `strings` 零命中
4. omp 的实际合同是 `ReadonlySessionManager`：`getBranch()` / `getEntries()` / `getEntry()` / `buildSessionContext()`（omp 源码 `packages/coding-agent/src/session/session-manager.ts:2563-2573`，扩展类型 `extensibility/extensions/types.ts:471`）。
5. 扩展其他处对 `getBranch` 都有 `typeof` 守卫（index.ts:108），#4237 新增行唯独漏了。

## 影响面（精确，勿误判为全挂）

- ✅ 正常：7 个 viking 工具注册、profile 系统提示注入、`turn_end` 记忆回写 OV（走 `getBranch()`）、`session_before_compact` 合同兼容（omp `CompactionPreparation` 有 `firstKeptEntryId`/`tokensBefore`）、`ui.notify`/`ui.setStatus(key,text)`/`registerCommand` 均兼容
- ❌ Bug #1（已修）：`context` 钩子抛错 → 每轮 recall 检索注入与 takeover 改写跳过，recall-ledger 永远为空

## Bug #2（已定位，待修）——systemPrompt 数组污染

- omp 合同：`BeforeAgentStartEvent.systemPrompt` 是 `string[]`（omp `extensibility/extensions/types.ts:756-760`）
- pi 合同：同一字段是 string（扩展按此写）
- 扩展 `index.ts:164-166`：`return { systemPrompt: event.systemPrompt + "\n\n" + additions }`
- 后果：JS 数组 + 字符串 → 数组隐式 `toString()`（逗号连接）→ omp 系统提示被**逗号拼接损坏**（不崩溃，静默污染）。runner 收到 string 后包成 `[一条长串]`（omp `extensibility/extensions/runner.ts:1736-1740`，对返回值 string/array 都兼容，问题只在读 event 时）
- 修法：`const base = Array.isArray(event.systemPrompt) ? event.systemPrompt.join("\n\n") : event.systemPrompt;` 再拼接。此守卫对 pi/omp 双向安全

## 已完成

1. Bug #1 修复已写入部署副本 `~/.omp/agent/extensions/openviking/index.ts:181-196`：`typeof` 守卫，pi 走 `buildContextEntries()`，omp 降级 `getBranch()`。降级安全性：ledger key = entry id + 内容 hash（`recall.ts:107`），id 错位最多一次缓存 miss，不会污染上下文
2. 同款修复已镜像回 OV 仓库规范源码 `examples/pi-coding-agent-extension/index.ts`（**未提交**；部分内容来自并行会话，注释更全，以当前工作区版本为准）
3. 验证已通过：`bun build` 转译 OK；真实 `omp -p` 冒烟（`cd /tmp && ~/.bun/bin/omp -p "reply with exactly: OK"`）干净返回、扩展错误消失

## 待办（切换到 OV 目录后按序执行）

1. **建 omp 专属副本**：`examples/omp-openviking-extension/`（从 `examples/pi-coding-agent-extension/` 当前修复态整体复制）。此后：
   - pi 版（`examples/pi-coding-agent-extension/`）只吃上游更新
   - omp 版吃 omp 兼容补丁（Bug #2 及后续），不回灌 pi 版，除非补丁双向安全（可顺手 PR 上游 OpenViking）
   - 部署流：omp 版目录 → `cp/rsync` 到 `~/.omp/agent/extensions/openviking/`（omp 自动发现该路径，已实测）
2. **修 Bug #2**（omp 版内）：按上文修法改 `before_agent_start` 返回段；跑三重验证：
   - `bun build <omp版>/index.ts --target=bun --external '*' --outfile /dev/null`
   - `node --test <omp版>/tests/*.mjs`（node v26.7.0 原生跑 TS；现有 7 个测试文件含 recall-ledger）
   - `cd /tmp && ~/.bun/bin/omp -p "..."` 冒烟 + 触发一次带记忆召回的提问确认 `<openviking-context source="session">` 注入恢复
3. **提交 OV 仓库**：omp 版目录 + pi 版 index.ts 修复（`ov-dev-opt` 分支，HEAD `cf786c97`）。注意工作区已有与本任务无关的改动：`.gitignore` 已修改（保留，勿回滚）、未跟踪目录 `.agents/skills/`、`.claude-flow/`、`.mcp.json`、`.swarm/`（别动别提交）
4. **上游动作（可选）**：Bug #1 + #2 守卫双向安全，值得 PR 给 volcengine/OpenViking（upstream/main 尚无修复）
5. **回归观察**：修复后 recall-ledger（`~/.openviking/pi-recall-ledger/<session>.json`）应开始有内容；若仍空查 `logLevel: info`

## 环境事实

- 运行时：`~/.bun/bin/omp` = omp/18.0.11（bun 1.3.14）；`~/.local/bin/omp` 是旧 17.3.8（PATH 优先 ~/.local/bin，勿混淆；全局升级需重编译，见记忆）
- 扩展 import 兼容性：`tools.ts` 的值导入 `import { StringEnum } from "@earendil-works/pi-ai"` 在 omp 下可解析（扩展已成功加载并注册工具），omp 做了旧包名映射；`@earendil-works/pi-coding-agent` 仅类型导入（运行时擦除），无需处理
- OV 服务器：`http://localhost:1933/health` = ok / 0.4.17.dev178 / auth_mode api_key
- omp 源码：`/Users/frankyang-mp2/Desktop/work/dev_proj/omp`；OV 源码：`/Users/frankyang-mp2/Desktop/work/dev_proj/OpenViking/OpenViking`（ov-dev-opt）
- 扩展目录内 `config.json` 为部署本地配置（overwrite 部署副本时注意保留，勿被仓库默认覆盖）
- e2e 脚本（`scripts/e2e-live.sh`）针对真 pi 设计，omp 下不直接适用；omp 验收以 `omp -p` 冒烟 + node 测试为准
