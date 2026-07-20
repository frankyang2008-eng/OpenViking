# CodeBuddy CLI harness 测试报告（openviking-memory 插件）

| 项目 | 信息 |
|-----|------|
| 对应方案 | [`codebuddy-cli-harness-plan.md`](./codebuddy-cli-harness-plan.md) |
| 测试日期 | 2026-07-20 |
| 分支 | `ov-dev-opt` |
| 环境 | macOS Darwin 25.5.0 / CodeBuddy Code CLI 2.124.0（brew）/ CodeBuddy CN IDE 4.10.3（运行中） |
| 被测对象 | `examples/claude-code-memory-plugin/.codebuddy-plugin/plugin.json` + `examples/memory-plugin-shared/install.sh` codebuddy harness |

---

## 结论汇总

| 阶段 | 用例数 | 通过 | 需用户操作 | 失败 |
|------|-------|------|-----------|------|
| 阶段二 自测（2.1–2.7） | 7 | 7 | 0 | 0 |
| 阶段三 E2E（3.1–3.6） | 6 | 3（3.1/3.5/3.6） | 3（3.2/3.3/3.4） | 0 |

- **可自动化用例 10/10 全部通过**，无失败项。
- **3.2 / 3.3 / 3.4 需用户交互**（CLI 登录会话 / 重启 IDE / 双端同开），无法在本会话自动完成，已标注验证步骤。
- 达到验收标准 1、2、5、6、7（自测全过 + E2E 可自动化项全过）；标准 3、4 依赖 3.2/3.3，待用户确认。

---

## 阶段二：自测（离线）

| 用例 | 方法 | 预期 | 实际 | 结论 |
|------|------|------|------|------|
| 2.1 manifest 合法 | `codebuddy plugin validate examples/claude-code-memory-plugin` | exit 0 | `✔ Validation passed`，`valid=true` | ✅ |
| 2.2 脚本语法 | `bash -n examples/memory-plugin-shared/install.sh` | exit 0 | 通过 | ✅ |
| 2.3 函数齐套 | grep `install_codebuddy()\|uninstall_codebuddy()\|materialize_codebuddy` | 定义+调用均存在 | 三函数定义在案且被 dispatch 调用 | ✅ |
| 2.4 挂接无遗漏 | 对照方案 §2.6 表逐项 grep | ~28 处全部落地 | 变量/检测/TUI/门控/dispatch/validate/summary/usage 全部就位 | ✅ |
| 2.5 物化排除正确 | 隔离目录跑 `materialize_codebuddy_marketplace` | 无 `.omc/node_modules/package-lock.json`；版本正确 | 产物排除三项；`marketplace.json` version=`0.4.3` | ✅ |
| 2.6 既有测试不回归 | `node --test scripts/marketplace.test.mjs` | 通过 | 19/19 pass，0 fail | ✅ |
| 2.7 TUI 不回归 | `SEL_CODEBUDDY` 默认 0，prelude 行为同 `SEL_QODER` | 默认不选中 | 默认未选，TUI 链路镜像 qoder | ✅ |

**2.5 备注**：隔离测试首次用 `awk '/^materialize.../,/^\}/'` 提取函数时被 heredoc 内列 0 的 `}`（JSON 闭合）截断，报 syntax error。改用显式行范围（`sed -n '418,428p'`、`sed -n '2739,2763p'`）提取后通过。属**测试提取方式缺陷**，非函数 bug（整文件 `bash -n` 本已通过）。

---

## 阶段三：整体验证（E2E，真实 `~/.codebuddy`）

### 3.1 安装 — ✅

- **方法**：`bash examples/memory-plugin-shared/install.sh --harness codebuddy --source dev --yes --lang en`（安装前快照 `settings.json`/`known_marketplaces.json`/`installed_plugins.json` 至临时目录）。
- **实际**：
  - `!! CodeBuddy IDE is running; restart it after install to load the plugin.`（pgrep 警告正确触发）
  - `✔ Validation passed`（`.codebuddy-plugin/plugin.json`，valid=true）
  - `✔ Marketplace 'openviking-local' added successfully`（`type: directory`）
  - `✔ Successfully installed plugin: openviking-memory@openviking-local`
  - `validate_install`：`codebuddy: marketplace registered` / `codebuddy: ... visible in plugin list` / `codebuddy: cached stdio proxy parses`，无 `ok=0`
  - `marketplace.test.mjs` 19/19 pass
- **独立复核**：`plugin list` 显示 `openviking-memory@openviking-local / Version 0.4.3 / Scope user / Status enabled`；`settings.json` `enabledPlugins["openviking-memory@openviking-local"]=true`；`known_marketplaces.json` 含 `openviking-local`(directory)；cache 目录 `.../cache/openviking-local/openviking-memory/0.4.3/` 存在。
- **collateral**：官方市场 `codebuddy-plugins-official` 完好；凭据 `ovcli.conf`（url `http://127.0.0.1:1933` + 117 字符 api_key）原样保留。

### 3.2 CLI 会话 — ⏳ 需用户操作

- **为何无法自动**：`codebuddy` 交互会话需用户首次登录（浏览器授权），无法在脚本内完成。
- **验证步骤**（用户）：终端执行 `codebuddy` 进入交互 → 完成登录 → 观察会话启动时 `SessionStart` 是否注入 openviking 上下文；`/ov` 命令与 MCP 工具（`add_resource`/`search`/`recall` 等）是否可用。
- **预期**：插件 hooks 触发，`${CLAUDE_PLUGIN_ROOT}` 被 CLI 兼容层解析（二进制 strings 已实证 10 处），MCP stdio 代理自 plugin root 启动。

### 3.3 IDE 端 — ⏳ 需用户操作

- **为何无法自动**：需用户重启 CodeBuddy CN IDE 并目视确认。
- **验证步骤**（用户）：退出并重启 CodeBuddy CN → 打开插件面板确认 `openviking-memory@openviking-local` 已启用 → 触发一次会话确认 hooks 生效。
- **依据**：CLI 与 IDE 实时共享 `~/.codebuddy`，3.1 已将 enabled 状态写入共享 `settings.json`，IDE 重启后应直接可见。

### 3.4 双端共存 — ⏳ 需用户操作

- **验证步骤**（用户）：CLI 与 IDE 同时打开，确认两边插件状态一致、互不干扰、配置共享。

### 3.5 升级幂等 — ✅

- **方法**：再次运行 `install.sh --harness codebuddy --source dev --yes --lang en`。
- **实际**（独立复核）：
  - `known_marketplaces.json` openviking marketplace keys = `["openviking-local"]`，count=1
  - `settings.json` enabledPlugins openviking-memory 条目 count=1
  - `installed_plugins.json` 台账 openviking-memory count=1
  - cache 目录单一版本 `0.4.3`（无残留旧版本目录）
  - 第二次 CLI 重写后官方市场 `codebuddy-plugins-official` 仍完好（keys: `codebuddy-plugins-official, openviking-local`）
- **结论**：无重复条目，幂等成立。

### 3.6 卸载 — ✅（五处清零）

- **方法**：`bash examples/memory-plugin-shared/install.sh --uninstall --harness codebuddy --yes --lang en`（IDE 运行中执行，revival 警告正确触发：`!! CodeBuddy IDE is running and may rewrite settings.json, reviving the entry; quit the IDE first.`）。
- **五处复核**：

| # | 位置 | 结果 |
|---|------|------|
| 1 | `settings.json` enabledPlugins 条目 | CLEAN ✓（无 openviking 条目） |
| 2 | `installed_plugins.json` 台账 | CLEAN ✓ |
| 3 | `known_marketplaces.json` openviking-local | CLEAN ✓（仅剩 `codebuddy-plugins-official`） |
| 4 | cache 目录 `plugins/cache/openviking-local` | CLEAN ✓（已删除） |
| 5 | 物化市场目录 `marketplaces/openviking-local` | CLEAN ✓（已删除） |

- **附加**：`plugin list` 不再显示 openviking-memory；官方市场与凭据（url + api_key）卸载后仍完好。
- **注**：尽管 IDE 运行中，条目**未复活**——官方 uninstall 在 IDE 任何回写前已自清 settings。revival 警告作为预防正确触发。

---

## 测试后处置

为保持目标 3「为 codebuddy 新增并保留插件」的最终状态，3.6 卸载验证完成后**已重新安装**。当前系统处于已安装/启用状态（`Status: enabled`，version 0.4.3）。

## 遗留风险与跟进

| 项 | 说明 | 处置 |
|---|------|------|
| 3.2/3.3/3.4 | CLI 登录会话、IDE 重启、双端同开 | 待用户操作后回填本报告 |
| IDE 运行中卸载 | 本次未复活，但理论上运行中 IDE 可能回写 settings.json | 卸载脚本已加 pgrep 警告；建议卸载前先退 IDE |
| cache 快照语义 | 改插件源后 cache 不自动更新 | 安装输出已提示「改源需重跑 install.sh」（同 qoder @local） |
