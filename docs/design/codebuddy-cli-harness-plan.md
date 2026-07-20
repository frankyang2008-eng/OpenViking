# CodeBuddy Code CLI harness 接入方案（openviking-memory 插件）

| 项目 | 信息 |
|-----|------|
| 状态 | `已实现并验证`（自测+E2E 可自动化项全过；3.2/3.3/3.4 待用户交互） |
| 创建日期 | 2026-07-20 |
| 分支 | `ov-dev-opt` |
| 前置状态 | P0（CLI 安装）✅ / P1（真机探测）✅ / 评审裁决 ✅ |

---

## 概述

为 `openviking-memory` 插件新增 **CodeBuddy Code CLI** harness，使其可像 Qoder 一样通过 `install.sh --harness codebuddy` 一键安装，并同时服务于 **CodeBuddy Code CLI**（终端）与 **CodeBuddy CN IDE**（桌面）双端。方案采用与 Qoder 相同的 **native 安装路径**（调用官方 CLI 子命令），**零手工 JSON 合并**，插件本体（hooks/scripts/servers/commands）**零改动**，仓库仅新增 1 个 manifest 文件。

---

## 目录

- [背景与目标](#背景与目标)
- [调研结论（真机实测语义）](#调研结论真机实测语义)
- [方案设计](#方案设计)
- [实现计划（四阶段）](#实现计划四阶段)
- [验收标准](#验收标准)
- [风险与缓解](#风险与缓解)
- [附录：评审裁决记录](#附录评审裁决记录)

---

## 背景与目标

用户提出四个目标（2026-07-20）：

| # | 目标 | 状态 |
|---|------|------|
| 1 | 安装 CodeBuddy Code CLI 供终端开发 | ✅ 已完成（brew，2.124.0，`codebuddy`+`cbc`）。待用户首次 `codebuddy` 交互登录 |
| 2 | 判断最合理安装方式（brew vs npm） | ✅ 已选定 **brew**（见下） |
| 3 | 在 `ov-dev-opt` 增加 codebuddy 的 openviking 插件（仿 qoder） | 设计定稿，**待实现** |
| 4 | 验证 CLI 与 IDE 双端均可用 | ✅ 可行性已实证，待 harness 完成后 E2E 复验 |

### 安装方式选型（目标 2）

选定 **brew**，理由：
- 预编译二进制（COS 广州 tarball），**无 Node 版本耦合**；npm 方式（`@tencent-ai/codebuddy-code`）受 Node 运行时影响。
- brew 集中管理、随 `brew upgrade` 升级；tap 为腾讯官方 `tencent-codebuddy/tap`。
- **坑（已记录）**：tap 后须先 `brew trust tencent-codebuddy/tap`，否则报 `Refusing to load formula ... from untrusted tap`。

### 双端共享原理（目标 4 可行性实证）

CLI 与 IDE **实时共享** `~/.codebuddy`（settings / marketplaces / cache / 台账）。实测：在终端执行 `codebuddy plugin list` 直接列出 IDE 当前启用的 9 个插件。因此**一次 user-scope 安装，双端同时生效**。

---

## 调研结论（真机实测语义）

以下来自对 **CodeBuddy Code CLI 2.124.0** 的真机探测（2026-07-20），为逆向所得、仓库与官方文档均无完整记录。**官方文档滞后**（仅记载 `plugin validate` 一个子命令）。

1. **非交互子命令齐全**：`codebuddy plugin` 含 `validate / list / marketplace(add|list|update|remove) / install / uninstall / prune / enable / disable / update`，全部可在 shell 脚本非交互调用。
2. **本地市场注册**：`codebuddy plugin marketplace add <dir>` 写入 `~/.codebuddy/plugins/known_marketplaces.json`，形态为
   `{"type":"directory","source":{"source":"directory","path":"<dir>"},"installLocation":"<dir>","autoUpdate":false}`。
   `directory` 型**不拷贝市场本体**，实时读源目录 → 源目录改动即生效。
3. **安装即启用**：`codebuddy plugin install <name>@<mp> --scope user` 会拷贝插件到 `~/.codebuddy/plugins/cache/<mp>/<name>/<version>/`，写 `settings.json` 的 `enabledPlugins["<name>@<mp>"]=true`，**自动启用**，并记台账 `installed_plugins.json`。
4. **占位符双支持**：二进制 strings 实证 `${CODEBUDDY_PLUGIN_ROOT}`（原生，53 处）与 `${CLAUDE_PLUGIN_ROOT}`（兼容，10 处）**均被解析**；IDE 侧 superpowers 插件长期使用 `${CLAUDE_PLUGIN_ROOT}`。→ 插件 `hooks/hooks.json`、`.mcp.json`（均用 `${CLAUDE_PLUGIN_ROOT}`）**无需改动**。
5. **卸载半自清**：`codebuddy plugin uninstall <id> --scope user` 自清 `settings.json` + 台账（无幽灵，优于 qoder），**但残留 cache 目录** → 需补 `rm -rf`。
6. **副作用（已知悉）**：CLI 的 marketplace 增删会用其自有模型重写 `known_marketplaces.json`（实测将 IDE 登记的 zip 型 Teams 市场条目丢弃；磁盘无损、无 enabledPlugins 引用、无实际损害）。
7. **hook 事件兼容**：CodeBuddy 文档列 26 个 hook 事件，本插件用到的 9 个（`SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/PreCompact/SessionEnd/SubagentStart/SubagentStop`）**全部在内**；stdin 字段（`session_id/cwd/prompt/tool_name/tool_input`）与 Claude Code 兼容；timeout 单位=秒。
8. **manifest 校验**：`codebuddy plugin validate` 对本文草拟的 `.codebuddy-plugin/plugin.json`（纯元数据）与 `marketplace.json` **均通过**。

> 证据锚点：`~/.codebuddy/settings.json`（9 个 `@codebuddy-plugins-official` 启用项）、`~/.codebuddy/plugins/known_marketplaces.json`、CLI 二进制 strings。探测后 `settings.json` 与备份逐字节一致，测试残留已清理。

---

## 方案设计

### 总体思路

仿照 Qoder harness（commit `733fec66`）的 **native 模式**，但比 Qoder 更轻：Qoder 需在 uninstall 时手工 scrub `settings.json` 幽灵条目，CodeBuddy 官方 uninstall 已自清，故 **全程零手工 JSON 合并**。安装路径：`物化本地市场 → validate → marketplace add → plugin install`。

### 1. 仓库新增（仅 1 个文件）

`examples/claude-code-memory-plugin/.codebuddy-plugin/plugin.json`：**纯元数据**（不声明 `commands`/`hooks`/`mcpServers`，依赖约定布局自动发现——已被官方 context7 插件无 mcpServers 声明所证实）。已通过 `codebuddy plugin validate`。

```json
{
  "name": "openviking-memory",
  "displayName": "OpenViking Memory",
  "version": "0.4.3",
  "description": "Long-term semantic memory for CodeBuddy, powered by OpenViking. Auto-recall relevant memories at session start and capture important information during conversations.",
  "descriptionZh": "由 OpenViking 驱动的 CodeBuddy 长期语义记忆。会话开始时自动召回相关记忆，并在对话过程中捕获重要信息。",
  "author": { "name": "OpenViking", "url": "https://github.com/volcengine/OpenViking" },
  "homepage": "https://github.com/volcengine/OpenViking",
  "repository": "https://github.com/volcengine/OpenViking",
  "license": "Apache-2.0",
  "keywords": ["memory", "openviking", "semantic-search", "long-term-memory", "context-engine"],
  "category": "developer-tools"
}
```

**版本锁步纪律**：`version` 跟随 `.claude-plugin/plugin.json`（当前 `0.4.3`），因 cache 路径含版本号（同 Qoder `0179132f` 纪律）。每次上游合并后检查并同步 bump。

### 2. install.sh 新增 codebuddy harness

文件：`examples/memory-plugin-shared/install.sh`（共 2999 行）。所有改动镜像 Qoder 挂接点。

#### 2.1 变量定义（加在 L63–82 变量块，镜像 L70/L81-82 的 QODER_*）

```bash
CODEBUDDY_DIR="${CODEBUDDY_CONFIG_DIR:-$HOME/.codebuddy}"        # 尊重 config-dir 覆盖
CODEBUDDY_MKT_NAME="openviking-local"                            # 防与未来官方市场碰撞
CODEBUDDY_MKT_DIR="$CODEBUDDY_DIR/marketplaces/$CODEBUDDY_MKT_NAME"
CODEBUDDY_PLUGIN_ID="${PLUGIN_NAME}@${CODEBUDDY_MKT_NAME}"
CODEBUDDY_CACHE_DIR="$CODEBUDDY_DIR/plugins/cache/$CODEBUDDY_MKT_NAME"
```

#### 2.2 检测（镜像 L380/L388/L455）

- `HAVE_CODEBUDDY`：`command -v codebuddy >/dev/null 2>&1 && HAVE_CODEBUDDY=1`。
- **IDE-only 不支持**：无 CLI 时无法走 native 路径，`validate_selected_bins`（L932，镜像 L960 的 qoder 检查）报「未在 PATH 找到 codebuddy，需先安装 CLI」。

#### 2.3 install_codebuddy()（加在 L2669 uninstall_qoder 之后）

```bash
install_codebuddy() {
  heading "$(t '4. CodeBuddy plugin' '4. CodeBuddy 插件')"
  command -v codebuddy >/dev/null 2>&1 || { warn "未找到 codebuddy，跳过"; return 0; }
  pgrep -f "CodeBuddy CN" >/dev/null 2>&1 && \
    warn "IDE 运行中：安装后需重启 IDE 加载插件"
  local plugin_dir
  plugin_dir="$(plugin_dir_on_disk claude-code-memory-plugin)" || { warn "未找到插件源码，跳过"; return 0; }
  codebuddy plugin validate "$plugin_dir" || warn "manifest 校验未过"
  materialize_codebuddy_marketplace "$plugin_dir" || { err "物化市场失败"; return 1; }
  codebuddy plugin marketplace remove "$CODEBUDDY_MKT_NAME" >/dev/null 2>&1 || true
  codebuddy plugin marketplace add "$CODEBUDDY_MKT_DIR" || { err "marketplace add 失败"; return 1; }
  codebuddy plugin uninstall "$CODEBUDDY_PLUGIN_ID" --scope user >/dev/null 2>&1 || true
  codebuddy plugin install "$CODEBUDDY_PLUGIN_ID" --scope user || { err "plugin install 失败"; return 1; }
  info "CodeBuddy 插件已安装并启用：$CODEBUDDY_PLUGIN_ID（cache 快照，改源需重跑 install.sh）"
}
```

`materialize_codebuddy_marketplace()`：把插件拷到 `$CODEBUDDY_MKT_DIR/plugins/openviking-memory/`（**排除** `.omc/`、`node_modules/`、`package-lock.json`），并用插件当前 `version` 生成 `$CODEBUDDY_MKT_DIR/.codebuddy-plugin/marketplace.json`：

```json
{
  "name": "openviking-local",
  "description": "OpenViking local marketplace (dev)",
  "owner": { "name": "OpenViking", "url": "https://github.com/volcengine/OpenViking" },
  "plugins": [
    { "name": "openviking-memory", "description": "Long-term semantic memory powered by OpenViking",
      "version": "<读自 .claude-plugin/plugin.json>", "source": "./plugins/openviking-memory", "license": "Apache-2.0" }
  ]
}
```

> 版本号在物化时从插件 manifest 动态读取，避免在仓库再维护一个需锁步的 marketplace.json（DRY）。

#### 2.4 uninstall_codebuddy()（镜像 L2669）

```bash
uninstall_codebuddy() {
  heading "$(t 'Remove CodeBuddy plugin' '移除 CodeBuddy 插件')"
  pgrep -f "CodeBuddy CN" >/dev/null 2>&1 && \
    warn "IDE 运行中可能回写 settings.json 致条目复活，建议先退出 IDE"
  command -v codebuddy >/dev/null 2>&1 && \
    codebuddy plugin uninstall "$CODEBUDDY_PLUGIN_ID" --scope user >/dev/null 2>&1 || true
  command -v codebuddy >/dev/null 2>&1 && \
    codebuddy plugin marketplace remove "$CODEBUDDY_MKT_NAME" >/dev/null 2>&1 || true
  rm -rf "$CODEBUDDY_CACHE_DIR"   # 官方 uninstall 残留 cache（实测）
  rm -rf "$CODEBUDDY_MKT_DIR"     # 物化目录
  info "已移除 CodeBuddy OpenViking 插件"
}
```

#### 2.5 validate（加进 validate_install() L2709，镜像 L2886–2907 的 qoder 块）

```bash
if contains_harness codebuddy; then
  if command -v codebuddy >/dev/null 2>&1; then
    codebuddy plugin marketplace list 2>/dev/null | grep -q "$CODEBUDDY_MKT_NAME" \
      && info "codebuddy: 市场已注册" || { warn "codebuddy: 市场未注册"; ok=0; }
    list="$(codebuddy plugin list 2>/dev/null || true)"
    str_contains "$list" "$CODEBUDDY_PLUGIN_ID" \
      && info "codebuddy: $CODEBUDDY_PLUGIN_ID 已出现在插件列表" || { warn "..."; ok=0; }
    cached=$(find "$CODEBUDDY_CACHE_DIR" -name 'mcp-proxy.mjs' -path '*/servers/*' 2>/dev/null | sort | tail -n 1)
    [ -n "$cached" ] && { node --check "$cached" && info "codebuddy: 缓存 stdio 代理语法正常" || ok=0; } \
      || { warn "codebuddy: 未找到缓存 stdio 代理"; ok=0; }
  else
    warn "codebuddy: 未找到 codebuddy，插件未校验"; ok=0
  fi
fi
```

#### 2.6 挂接点清单（镜像 Qoder，约 28 处）

| 位置 | 行号（现状） | 改动 |
|---|---|---|
| header 注释/one-liner | L11, L13 | harness 列加 codebuddy |
| usage 文本 | L154 | 支持列表加 codebuddy |
| 变量块 | L63–82 | 加 CODEBUDDY_*（§2.1） |
| 检测初始化 | L380, L455 | HAVE_ 块加 HAVE_CODEBUDDY=0 |
| 检测命令 | L388 同位 | `command -v codebuddy` |
| SEL 初始化 | L467 | SEL_CODEBUDDY=0 |
| TUI 全链路 | L567, L576, L589, L611, L680, L704, L792, L806, L877 | 各加 codebuddy 镜像行 |
| validate_selected_harnesses case | L923 | case 加 `codebuddy` |
| validate_selected_bins | L960 同位 | 加 codebuddy CLI 检查 |
| uninstall dispatch | L2125–2126 | `if contains_harness codebuddy; then uninstall_codebuddy; fi` |
| install/uninstall 函数 | L2669 后 | 新增 §2.3/§2.4 |
| validate_install | L2886–2907 同位 | 加 §2.5 块 |
| install dispatch | L2983 | `if contains_harness codebuddy; then install_codebuddy; fi` |
| summary | L2999 | 结果汇总加 codebuddy |

> TUI 测试无需改：`SEL_CODEBUDDY` 默认 0，prelude 初始化位置同 `SEL_QODER`。

### 3. 占位符兜底（预计用不上）

若 E2E 实测 CLI 会话中 hook/MCP 未启动（概率很低：二进制 + IDE 双重实证），在 `materialize_codebuddy_marketplace` 加一行 `sed`，把拷贝副本里的 `${CLAUDE_PLUGIN_ROOT}` 重写为 `${CODEBUDDY_PLUGIN_ROOT}`（文档原生占位符）。

### 4. 明确不做（YAGNI）

IDE-only 文件放置回退 / Windows 分支 / project scope / 远程 marketplace / 插件内容（hooks/scripts/servers）任何改动 / 仓库内 ship 市场目录。

---

## 实现计划（四阶段）

### 阶段一：实现（P2）

| # | 任务 | 产出 | 完成判据 |
|---|------|------|---------|
| 1.1 | 新增 `.codebuddy-plugin/plugin.json` | 1 文件（§1 内容） | `codebuddy plugin validate` 通过 |
| 1.2 | install.sh 变量块 + 检测 | §2.1/§2.2 | `command -v codebuddy` 门控生效 |
| 1.3 | `materialize_codebuddy_marketplace` + `install_codebuddy` + `uninstall_codebuddy` | §2.3/§2.4 | 函数定义存在且被 dispatch |
| 1.4 | validate 块 + 全部 TUI/dispatch/summary 挂接（§2.6） | ~120 行 | `bash -n` 通过；无未定义引用 |

### 阶段二：自测（无需 CodeBuddy 会话，可离线）

| # | 检查 | 方法 | 通过标准 |
|---|------|------|---------|
| 2.1 | manifest 合法 | `codebuddy plugin validate examples/claude-code-memory-plugin` | exit 0 |
| 2.2 | 脚本语法 | `bash -n examples/memory-plugin-shared/install.sh` | exit 0 |
| 2.3 | 函数齐套 | `grep -E 'install_codebuddy\(\)|uninstall_codebuddy\(\)\|materialize_codebuddy' install.sh` | 定义 + 调用均存在 |
| 2.4 | 挂接无遗漏 | 对照 §2.6 表逐项 grep | 28 处全部落地 |
| 2.5 | 物化排除正确 | 对临时副本跑 materialize，检查产物 | 无 `.omc/node_modules/package-lock.json`；marketplace.json 版本正确 |
| 2.6 | 既有测试不回归 | `node --test scripts/marketplace.test.mjs` | 通过 |
| 2.7 | TUI 不回归 | SEL_CODEBUDDY 默认 0，prelude 行为同前 | 默认不选中 |

### 阶段三：整体验证（E2E，需用户已 `codebuddy` 登录）

| # | 场景 | 方法 | 通过标准 |
|---|------|------|---------|
| 3.1 | 安装 | `install.sh --harness codebuddy --source dev --yes` | `validate_install` 全过；`plugin list` 见 enabled |
| 3.2 | CLI 会话 | 终端 `codebuddy` 交互 | `SessionStart` 注入 openviking 上下文；`/ov` 命令与 MCP 工具可用 |
| 3.3 | IDE 端 | 重启 CodeBuddy CN | 插件列表可见已启用；hooks 触发 |
| 3.4 | 双端共存 | 双端同开 | 互不干扰，配置一致 |
| 3.5 | 升级幂等 | 再跑一次 install | 无重复条目，版本更新 |
| 3.6 | 卸载 | `install.sh --uninstall --harness codebuddy` | settings/台账/known_marketplaces/cache/物化目录 **五处清零** |

### 阶段四：测试报告

> ✅ 已产出 [`codebuddy-cli-harness-test-report.md`](./codebuddy-cli-harness-test-report.md)。阶段二 2.1–2.7 自测 7/7 通过；阶段三 3.1/3.5/3.6 E2E 通过，3.2/3.3/3.4 需用户交互（详见报告）。

产出 `docs/design/codebuddy-cli-harness-test-report.md`（或追加本文件「测试结果」节），逐项记录：

```
| 用例 | 环境 | 方法 | 预期 | 实际 | 结论 | 证据 |
```

- 覆盖阶段二 2.1–2.7（自测）与阶段三 3.1–3.6（E2E）全部用例。
- 每条记录实际输出/截图/命令回显作为证据；失败项注明根因与处置。
- 结论汇总：通过率、遗留风险、是否达到验收标准。

---

## 验收标准

1. `install.sh --harness codebuddy --source dev --yes` 一键安装成功，`validate_install` 无 `ok=0`。
2. `codebuddy plugin list` 显示 `openviking-memory@openviking-local` 为 enabled。
3. CLI 交互会话 `SessionStart` 成功注入 openviking 上下文（`/ov` 可用）。
4. 重启 IDE 后插件可见且启用，hooks 触发。
5. 重复安装幂等，无重复市场/插件条目。
6. `install.sh --uninstall --harness codebuddy` 后五处（settings / 台账 / known_marketplaces / cache / 物化目录）无残留。
7. 自测 2.1–2.7 全过；E2E 3.1–3.6 全过并产出测试报告。

---

## 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| IDE 运行中回写 `settings.json` 覆盖安装/卸载结果 | 低（实测 CLI 写入与运行中 IDE 共存无损） | install/uninstall 加 `pgrep -f "CodeBuddy CN"` 警告；卸载后复查条目 |
| CLI 重写 `known_marketplaces.json` 丢 IDE 侧 Teams 条目 | 低（实测无损） | 已知悉；Teams 市场异常则在 IDE 内重加 |
| 自动更新与 brew 升级漂移 | 低 | 观察版本；必要时 `DISABLE_AUTOUPDATER=1` |
| cache 快照致 dev 源改动不生效 | 已知语义 | 安装成功输出打印 cache 路径 + 「改源需重跑 install.sh」（同 qoder @local） |
| `${CLAUDE_PLUGIN_ROOT}` 在 CLI 会话失效 | 很低（二进制 10 处实证） | §3 兜底：物化时 sed 重写为 `${CODEBUDDY_PLUGIN_ROOT}` |
| `CODEBUDDY_CONFIG_DIR` 自定义配置目录 | 低 | 变量尊重该覆盖（§2.1），默认 `~/.codebuddy` |

---

## 附录：评审裁决记录

三路评审（architect `REQUEST_CHANGES` / critic `需补探测后执行` / docs 文档核查）针对 v1 计划。裁决分两类：

**(a) 被真机探测定论（reviewer 的 blocker 已消解）**
- critic F1/H1/H2/H3、architect C1/H2：v1 的「未证实假设」（本地市场类型、注册文件、CLI≠IDE）已由 P1 探测全部实证。
- docs「CLI 仅 validate 一个子命令」：**驳回**——文档滞后，实测 2.124.0 九个非交互子命令齐全。
- critic M4（插件目录布局）：实测 `plugins/` 布局安装+启用成功。

**(b) 采纳进本方案**
- architect M1：物化拷贝排除 `.omc/node_modules/package-lock.json`（§2.3）。
- architect L2：尊重 `CODEBUDDY_CONFIG_DIR`（§2.1）。
- architect L3：validate 增加 `marketplace list` 市场注册检查（§2.5）。
- architect M3：挂接点计数更正为 ~28 处（§2.6）。
- critic L1：市场名 `openviking-local` 防碰撞（§2.1）。
- critic H4/H5：install/uninstall 加 IDE 进程检测警告（§2.3/§2.4）。
- critic M5：cache 快照语义，成功输出打印「改源需重跑」（§2.3）。

**(c) 驳回**
- IDE-only 手工文件放置回退：native 路径已完备，不建脆弱备用路径（YAGNI）。
- npm 反转选型：brew 已装好 2.124.0 且二进制无 Node 耦合，选型论据成立。
