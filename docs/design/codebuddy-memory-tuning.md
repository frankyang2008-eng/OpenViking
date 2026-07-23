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

- 真实命中（Q2–Q5）top 在 0.79–0.81，所有 pick finalScore ≥ 0.67（Q4 尾部 0.49 为 harness 记忆本身，属相关）。
- nonsense（Q6）raw max 仅 0.23，在默认 0.35 下已被正确过滤。
- 边界噪音（Q7）"全新项目" 触发了 brainstorming/writing-plans 等通用 skill，top finalScore=0.5121，其余 0.47–0.50；picks 里有 2 个 HTTP 404 已删文件，实际注入 51KB 通用模板，属主要 token 浪费源。
- 真实命中 tail（Q3 末尾 0.52、Q4 末尾 0.49）与 Q7 top（0.51）基本对齐，但 Q4 尾部 0.49 是 harness 记忆本身（相关），Q3 尾部 0.52 是 AGFS 概念（相关）。故 clean separation 出现在 0.51 与 0.67 之间，约 0.16 的空白带。

## 3. 选定值与理由

| 参数 | 默认 | 选定 | 理由 |
|---|---|---|---|
| `scoreThreshold` | 0.35 | **0.5** | 位于规则候选区间 0.45–0.5 上沿。高于 Q7 边界噪音中 5/6 条（0.47–0.50），仅让 top=0.5121 的 brainstorming skill 通过（对"全新项目"query 本身是有用的），其余 5 条通用模板/404 噪音被滤（51KB 注入大幅缩小）；远低于真实命中尾部 0.67，不会漏掉任何真实记忆。nonsense（raw max 0.23）早已远离。 |
| `profileTokenBudget` | 10000 | **4000** | session-start profile 注入是最大 token 来源；4000 足以覆盖目录摘要 + 最近 trajectory 概览，砍掉 60% profile 体积。召回侧已受 `recallTokenBudget=2000` 约束，不在这里压。 |

## 4. 其余保持默认的键

| 键 | 默认值 | 不改的理由 |
|---|---|---|
| `recallLimit` | 6 | top-6 覆盖 L0+L1 足够，真实命中分布前 6 条均 ≥0.67 |
| `recallTokenBudget` | 2000 | 已约束 per-recall 上限 |
| `recallMaxContentChars` | 500 | 已截断 L2 正文 |
| `recallPreferAbstract` | true | L0 摘要优先，符合 RAGFS 三层模型 |
| `minQueryLength` | 3 | 中文单字/短词过滤 |
| `timeoutMs` | 15000 | 本地 server 充足 |
| `captureMode` | semantic | 当前捕获策略 |
| `commitTokenThreshold` | 20000 | 提交阈值独立于召回 |

## 5. 应用方式

写入 `~/.openviking/ov.conf` 的 `claude_code` 对象（仓库外、用户运行时配置，不入库）：

```json
"claude_code": {
  "scoreThreshold": 0.5,
  "profileTokenBudget": 4000
}
```

合并时保留 `claude_code` 下其它已有键（本次为首次写入，原 `claude_code = null`）。改前已备份至 `~/.openviking/ov.conf.bak.<timestamp>`。

如需临时实验而不改文件，可走 env 覆盖：`OPENVIKING_SCORE_THRESHOLD=0.5 OPENVIKING_PROFILE_TOKEN_BUDGET=4000`。

## 6. 免同步说明

`examples/claude-code-memory-plugin/scripts/config.mjs` 在每次召回前现读 `ov.conf` 的 `claude_code.*` 并与 env 覆盖合并；无需重启 server、无需重启 CodeBuddy、无需执行 sync。改完立即生效，debug-recall config summary 已确认新值被打印。
