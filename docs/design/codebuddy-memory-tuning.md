# CodeBuddy / Claude memory plugin 参数调优 — 测量记录

日期：2026-07-23
Server：v0.4.11.dev20 @ http://127.0.0.1:1933
工具：`examples/claude-code-memory-plugin/scripts/debug-recall.mjs`（直连 server，独立于 CodeBuddy IDE）

## 1. 测量方法

对 7 条代表性 query 逐条执行 `node scripts/debug-recall.mjs "<query>"`，记录每条的：

- config summary 中 `scoreThreshold` / `recallLimit`（测量时为默认 0.35 / 6）
- PostProcess 后 raw result 的最高分（`finalScore`）
- Final Picks 条数与生成 system message 字节数

所有分数为**实测值**，非估计。测量前 `claude_code = null`，因此全部走内置默认阈值 0.35。

## 2. 电池 query 实测结果

| # | Query | top finalScore | picks | system msg bytes | 判定 |
|---|---|---|---|---|---|
| 1 | 我喜欢什么编程语言 | n/a（24 条全被滤掉，raw max=0.3301 < 0.35） | 0 | 0（无注入） | 真实偏好 query，但未存对应记忆 |
| 2 | 上次升级 OpenClaw 遇到了哪些坑 | **0.7949**（分布 0.68–0.79） | 6 | 12530 | 真实命中 |
| 3 | vikingdb 和 vikingfs 是什么关系 | **0.8033**（分布 0.52–0.80） | 6 | 32450 | 真实命中 |
| 4 | install.sh 支持哪些 harness | **0.8083**（分布 0.49–0.81） | 6 | 16147 | 真实命中 |
| 5 | 我的 Homebrew 镜像偏好是什么 | **0.8016**（分布 0.56–0.80） | 6 | 9997 | 真实命中 |
| 6 | 量子引力波怎么做红烧肉 | n/a（24 条全被滤掉，raw max≈0.2321 < 0.35） | 0 | 0（无注入） | nonsense，正确过滤 |
| 7 | 帮我写一个完全没接触过的全新项目 | **0.5121**（分布 0.47–0.51，brainstorming/plan 类通用 skill） | 6 | 51012 | 边界噪音：通用 skill 被命中，注入 51KB 通用模板 |

关键观察：

- 真实命中（Q2–Q5）top 在 0.79–0.81，主体 pick finalScore ≥ 0.67，但尾部下探至 Q3=0.52、Q4=0.49（Q4 尾部 0.49 为 harness 记忆本身，属相关）。
- nonsense（Q6）raw max 仅 0.23，在默认 0.35 下已被正确过滤。
- 边界噪音（Q7）"全新项目" 触发了 brainstorming/writing-plans 等通用 skill，top finalScore=0.5121，其余 0.47–0.50；picks 里有 2 个 HTTP 404 已删文件，实际注入 51KB 通用模板，属主要 token 浪费源。
- 真实命中 tail（Q3 末尾 0.52、Q4 末尾 0.49）与 Q7 top（0.51）基本对齐，但 Q4 尾部 0.49 是 harness 记忆本身（相关），Q3 尾部 0.52 是 AGFS 概念（相关）。故 clean separation 出现在 0.51 与 0.67 之间，约 0.16 的空白带。

## 3. 选定值与理由

| 参数 | 默认 | 选定 | 理由 |
|---|---|---|---|
| `scoreThreshold` | 0.35 | **0.35**（保持默认） | **2026-07-24 实测推翻 0.5 建议**。Q4（install.sh harness）Final Picks finalScore 实测分布 0.4345–0.5404：`shell-installer-cli-flag-addition`=0.5404、`install.sh交互退出bug检测`=0.4907、`openviking_add_new_harness`=0.4589、`openviking_tui_exit_bug_fix`=0.4345。0.5 阈值只留 top 0.5404 一条开发轨迹，滤掉 0.4907/0.4589/0.4345 三条核心相关记忆（add_new_harness/交互退出bug 均为 install.sh harness 主题核心），误杀。Q7 边界噪音 top finalScore <0.5，0.5 下 0 picks，但代价是 Q4 误杀。clean separation 不存在：Q4 真实命中尾部 0.4345 与 Q7 噪音重叠，无法用单阈值分离。0.35 默认合理，Q7 噪音 ~20KB 注入是保留 Q4 核心的必要代价。 |
| `profileTokenBudget` | 10000 | **10000**（保持默认） | 2026-07-23 建议 4000 未实施：debug-recall 只测 recall 不测 session-start profile 注入，4000 无新数据支撑。保持 10000，待单独测 profile 注入大小后再定。 |

## 4. 其余保持默认的键

| 键 | 默认值 | 不改的理由 |
|---|---|---|
| `recallLimit` | 6 | top-6 覆盖 L0+L1 足够，真实命中主体分布 ≥0.67（尾部最低至 Q4 0.49） |
| `recallTokenBudget` | 2000 | 已约束 per-recall 上限 |
| `recallMaxContentChars` | 500 | 已截断 L2 正文 |
| `recallPreferAbstract` | true | L0 摘要优先，符合 RAGFS 三层模型 |
| `minQueryLength` | 3 | 中文单字/短词过滤 |
| `timeoutMs` | 15000 | 本地 server 充足 |
| `captureMode` | semantic | 当前捕获策略 |
| `commitTokenThreshold` | 20000 | 提交阈值独立于召回 |

## 5. 应用方式

**2026-07-24 修正**：原建议写入 `~/.openviking/ov.conf` 的 `claude_code` 块——**此法不可行，会让 server 启动崩溃**。`openviking_cli/utils/config/open_viking_config.py` 的 `OpenVikingConfig.from_dict` 调用 `raise_unknown_config_fields`，valid_fields = `model_fields | {server, bot, parsers}`，不含 `claude_code`；ov.conf 顶层出现 `claude_code` 即被拒。插件端 `config.mjs` 的 `loadConfig` 虽用 `JSON.parse` 宽松读取 `claude_code`（line 111），但与 server 共享同一 ov.conf，无法绕过 server 的 pydantic 校验。

**结论**：0.35 默认值已合理（见 §3），无需覆盖。如需临时实验，走 env 覆盖（需重启 CodeBuddy 会话使 env 注入生效）：`OPENVIKING_SCORE_THRESHOLD=0.5`。改源码默认值需编辑 `examples/claude-code-memory-plugin/scripts/config.mjs` 并 `bash examples/memory-plugin-shared/install.sh --sync codebuddy`（版本号未 bump 时须先删 `~/.codebuddy/plugins/cache/openviking-local/openviking-memory/<ver>/` 再 sync，否则 cache 命中旧快照）。

## 6. 同步说明

**2026-07-24 修正**：原"无需重启"结论错误。(1) env 覆盖方式需重启 CodeBuddy 会话——env 在会话启动时注入 hook 子进程，运行中的会话不会重读。(2) ov.conf `claude_code` 方式不可用（见 §5，崩 server）。(3) 改源码默认值需 `install.sh --sync codebuddy` 重新物化 + 重装，且版本号未 bump 时须先删 cache 目录强制重拷。

`config.mjs` 的 `loadConfig` 确实每次召回现读 ov.conf（line 82 `readFileSync`），插件 hook 每次是新进程读最新文件——但这只对"不崩 server 的字段"有效；`claude_code` 不在此列。
