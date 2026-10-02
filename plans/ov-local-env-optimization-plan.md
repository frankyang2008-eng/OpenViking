# 本机 OpenViking 环境优化目标计划（可落地版）

**日期**：2026-09-29 · **分支**：`ov-dev-opt` · **产生方式**：glm-5.3(thinking:max) × kimi-k2.8-preview(thinking:max) 三轮对抗（R1 立场书 → R2 互批 → R3 决策备忘），父 agent 逐条复核争议事实后仲裁

**本文件的用法**：下一个 agent 从 §0 起手，按 §4 任务表逐条落地。每条任务自带改动点、门禁、验收信号、回滚方式。**未经 §5 预注册门的裁决一律不得执行砍除**（这是双方对抗后的共同纪律，不是建议）。

---

## 0. 起手（3 条命令，约 10 分钟）

```bash
cd /Users/frankyang-mp2/Desktop/work/dev_proj/openviking/OpenViking

# ① 拿账号级 key（root key 调数据 API 会 403，见 §2 事实 F8）
K=$(python3 -c "import json;print(json.load(open('$HOME/.openviking/ovcli.conf'))['api_key'])")

# ② 基线快照（重启后累计值；本机无 Prometheus，计数器重启即失）
curl -s localhost:1933/metrics | grep -E "^openviking_(rerank_calls_total|rerank_tokens_input_total|operation_tokens_total)" > /tmp/baseline-$(date +%H%M).txt

# ③ 复现 rerank 敞口（单次 mode=list search ≈ +12 calls / +41k prompt tok / 7–9s）
curl -s -X POST localhost:1933/api/v1/search/search -H "X-API-Key: $K" \
  -H 'Content-Type: application/json' \
  -d '{"query":"<任意真实 query>","mode":"list","limit":5}' -o /tmp/probe.json -w "http=%{http_code} wall=%{time_total}s\n"
```

---

## 1. 证据等级约定（全文遵守）

- `[实测]` 本机跑出的命令/指标输出，附命令
- `[代码]` 源码行号，可 `sed -n` 复核
- `[文档]` plans/ 下历史材料，**不单独作为决策依据**
- `[推断]` 无直接证据，标置信度

---

## 2. 已核验事实（父 agent 亲自复核，两模型曾在此分歧）

| # | 事实 | 等级 | 复核命令/出处 |
|---|---|---|---|
| F1 | **rerank 今日活跃**：`rerank_calls_total=67`、`rerank_tokens_input_total=242,703`（父 agent 两次探针各 +12 calls/+41k tok 后）。全部归属 `operation="search.search",stage="rerank"` | [实测] | `curl -s localhost:1933/metrics \| grep rerank` |
| F2 | **pi 主召回完全不经过 rerank**：pi 扩展只调 `/api/v1/sessions/{id}/context` 与 `/api/v1/content/read`，从不调 `/search/search` | [代码] | `~/.pi/agent/extensions/openviking/client.ts:113-196`；全扩展目录 grep `/api/v1/` 无 search 端点 |
| F3 | 与之互证：`operation="search.context"` 只有 `stage="embed_query"`（16,628 token），**无 rerank stage**；context_assembler 全目录 grep `rerank` 零命中 | [实测][代码] | `curl /metrics`；`grep -rn rerank openviking/retrieve/context_assembler/*.py` → 空 |
| F4 | **ov.conf timeout 今日 09:23 从 120 → 240**（备份文件 `ov.conf.bak.20260929-092345` 内为 120） | [实测] | `diff ~/.openviking/ov.conf ~/.openviking/ov.conf.bak.20260929-092345` |
| F5 | **O4 真实发生**：9-28 `Commit streaming train failed` ×4 = 1×Connection error（21:01）+ 3×Request timed out（21:12/21:49/21:54），全部来自 `compressor_v3`；`openviking.session.session` 侧 phase2 墙钟 157/208/544s | [实测] | `grep -cE "Commit streaming train failed" ~/.openviking/data/log/openviking.log.2026-09-28` |
| F6 | **O6 真实发生但日期是 9-27 不是 9-28**：`AttributeError: 'list' object has no attribute 'strip'` ×2（9-27 23:37），9-28 日志 0 次。缺陷行 `openviking/session/working_memory.py:937`，同类 5 处 `:526/:784/:857/:900/:937` | [实测][代码] | `grep -n "AttributeError" openviking.log.2026-09-27`；`sed -n '937p' openviking/session/working_memory.py` |
| F7 | rerank 调用点就一个开关：`openviking/session/memory/tools.py:340` `rerank=consumer != "prefetch"`；消费者标签 = prefetch / experience / patch_merge / react（默认） | [代码] | `grep -n 'rerank=consumer' openviking/session/memory/tools.py` |
| F8 | **root key 调数据 API 得 403**：`ROOT API keys cannot access tenant-scoped data APIs in api_key mode`。必须用 `ovcli.conf` 的账号级 `api_key`（`X-API-Key` 或 `Authorization: Bearer` 均可） | [实测] | 见 §0 命令 |
| F9 | `ragfs localfs` 静默错误计数：`read` error **12,401**、`stat` 475、`read_dir` 12，且仍在升；但日志零 ERROR、无功能损伤证据 | [实测] | `curl /metrics \| grep 'plugin="localfs",status="error"}'` |
| F10 | rerank 单次成本（父 agent 两次探针）：**~12 次调用 / ~41k prompt tok / 7–9s 墙钟每次 mode=list search** | [实测] | §0 ③ 前后差 |
| F11 | 重型步骤仍开思考：`trajectory_analyzer.py:248`、`gradient_estimator.py:152` 均 `thinking=True`；关思考实测 reasoning 3029→0、46–65s→15.1s、completion 3881→~1000 | [代码][文档 ark-timeout-diagnosis §4b] | `grep -n thinking=True openviking/session/train/components/*.py` |

### 争议仲裁（记录在案，防翻案）

| 争议 | glm-5.3 主张 | kimi 主张 | 仲裁（父 agent 实测） |
|---|---|---|---|
| 今日 rerank 是否活跃 | 「今日 rerank 0」（过期采样） | 28→43 calls、106k tok | **kimi 胜**。glm 采样早于流量窗（server 09:23 重启） |
| 那批 rerank 是否来自 pi 会话注入 | 「pi 主召回不走 rerank」 | 「URI 与会话注入逐一致、注入走 mode=list」 | **glm 胜（机制）**。pi 扩展根本不调 search 端点（F2）；rerank 来自显式 `mode=list` 调用 |
| O4 阻塞性 | 阻塞级 | 240s 下未验证 | **kimi 口径胜**。timeout 今日才 120→240（F4），历史失败生在 120s 期；240s 下未验证 |
| O6 严重性与行号 | 934，9-28 触发 ×1 | 937，日志无实证 | **kimi 行号胜、glm 存在性胜**。真实触发在 9-27 ×2（F6） |
| 9-28 失败计数 | 3/5 失败含 strip×1 | 4 次（1 conn + 3 timeout） | **kimi 胜**（F5），strip 与 9-28 无关 |
| localfs churn | 2.3/s、3881→7211 | 算术错、计数器仍在升 | **双方各错一半**：现象真（现 12,401）、速率口径 glm 算错。零决策权重 |

---

## 3. 裁决

### 3.1 双方共识（作为计划前提，不再讨论）

1. **mini pointwise 绝对分禁止进入 `dir_queue`**，只允许做 L2 终排。机制证据：524 个真实分只有 12 个唯一值、73.5% ≤ 0.15、top-2 并列 42.6% 靠 `heappush(dir_queue, (-score, uri))` 的 URI 字典序破并列；而分数经 `score_propagation_alpha=0.7` 混入并改写递归扩展路径（`hierarchical_retriever.py:828/853`，两模型独立复核一致）。**跨 query 漂移的绝对分不适合驱动路由**——路由需要组内可比的相对分。
2. **先门后砍**：任何消费方的砍除必须先过 §5 预注册门。无证据砍除属违规。
3. **ledger 只做分布监控，永不当真值**：`~/.openviking/pi-recall-ledger/` 无相关性标注、无 query 原文（仅 hash）、只覆盖 pi 召回。
4. **O4 在 240s 下未验证**（F4），重测后才谈严重性。
5. `prefetch` 已 `rerank=False`（F7）保持不变。

### 3.2 父 agent 的仲裁差异（两模型未覆盖或权重不同）

**D1：rerank 的实际影响面比双方假设都窄 → T5 优先级下调。**
两模型都把 search 路径当"活跃敞口"，kimi 还进一步外推成会话注入。F2/F3 证明它只影响**显式 `mode=list` search**（今日 13–15 次请求、每次 ~41k tok / 7–9s），pi 的每次会话召回完全不经过它。因此它的紧急度低于 O6/O4——后者直接杀掉记忆沉淀（F5/F6 各有真实失败）。

**D2：token 性价比的真正大头不是 rerank，是 phase2 的 VLM reasoning。**
今日 rerank 242k tok 是累计量；而单次重型 phase2 请求固定烧 ~3029 reasoning token、46–65s（F11），并有 4 次失败（F5）。**关思考是唯一同时降 token 与降延迟的动作**（−70% 延迟 / −75% completion），因此 T3 应为最高优先级，而不是排名靠后。

**D3：「rerank 模拟」的裁决是"机制错配 + 池内有效"，不是"无用"。**
受控同池实验显示池内排序能力真实（nDCG 0.9388；9-22 人工标注 0.992 vs 向量 0.789），而全链路实验显示它把递归路由带偏（双盲标注 OFF 独有选文 1.569 vs ON 1.091）。两者不矛盾：**能力存在，接入点错了**。所以正确动作是"换接入点/换实现"，不是"删掉重排"。

**D4：O5（peer 树重复实体）/ O7（混合制）不排期。** O5 与召回噪音相关但非同因（语料问题 ≠ rerank 问题），O7 的前置条件（OFF 排序伤害用户的证据）不存在。

---

## 4. 任务表（按落地顺序）

### T1 —— 判别实验（零代码，S，约 15 分钟）
**目标**：用一次真实 commit + 一次 search 同时定 O4 严重性、O6 敞口、rerank 敞口基线。
**步骤**
```bash
# a) 快照
curl -s localhost:1933/metrics | grep -E "rerank|localfs" > /tmp/t1-before.txt
wc -l ~/.openviking/data/log/openviking.log
# b) 一次显式 search（复现敞口）
K=$(python3 -c "import json;print(json.load(open('$HOME/.openviking/ovcli.conf'))['api_key'])")
curl -s -X POST localhost:1933/api/v1/search/search -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"query":"<真实 query>","mode":"list","limit":5}' -w "\nhttp=%{http_code} wall=%{time_total}\n"
# c) 一次真实 commit 重放（payload 复用 plans/rerank-mini-verification/step3-replays/）
#    POST /api/v1/sessions/{sid}/messages/batch  →  POST /api/v1/sessions/{sid}/commit
#    轮询 task_id 至 phase2 结束
# d) 判读
grep -cE "ArkAPITimeout|Request timed out" ~/.openviking/data/log/openviking.log
grep -cE "'list' object has no attribute 'strip'" ~/.openviking/data/log/openviking.log
grep -oE "consumer=[a-z_]+" ~/.openviking/data/log/openviking.log | sort | uniq -c
curl -s localhost:1933/metrics | grep -E "^openviking_rerank_calls_total|^openviking_rerank_tokens_input_total"
```
**判读规则（先锁）**：timeout ≥1 → O4 在 240s 下仍成立，T3 升为 P0；=0 且墙钟 <240s → O4 关闭并把失败归因回端点排队。AttributeError ≥1 → O6 立即修（本来就修）。consumer 分布给出 experience/patch_merge 的真实敞口。
**验收**：四个读数写入本文件附录 A。
**回滚**：无（零代码改动）。副作用 = 一次真实记忆写入。

### T2 —— O6 防御修复（S）
**改动点**：`openviking/session/working_memory.py:937` 及同类 `:526/:784/:857/:900`——`op.get("content")` 为 list/dict 时走规范化而非 `.strip()`。
**门禁**：str 行为不变（既有单测全绿）。
**验收**：新增单测喂 list content 的 UPDATE op 不抛异常；T1 重放 AttributeError=0。
**回滚**：单点 revert。

### T3 —— 重型 phase2 步骤关闭 reasoning（S/M，**最高收益**）
**改动点**：`openviking/session/train/components/trajectory_analyzer.py:248`、`gradient_estimator.py:152` 的 `thinking=True` → 按步可覆盖（若配置不支持按步覆盖，先合 #5432 extra_body 转发）。
**门禁**：小样本（n≥20）双盲质量 A/B 无方向回退，κ≥0.75。
**验收**：phase2 墙钟降幅 ≥30%（诊断实测 −70%，留余量）、completion token 降幅 ≥50%、9-28 式失败归零。
**依赖**：T1（判定 240s 下 O4 是否仍成立）。
**回滚**：`thinking=True` 单点 revert。

### T4 —— rerank 归因指标落盘（S）
**目标**：现在 `per-consumer tok/calls` 随重启即失（本机无 Prometheus），导致所有后续 A/B 无基线。
**改动点**：metrics 计数器持久化（落地位置在 `openviking/metrics/` 侧）。
**顺带**：修 `account_id="__unknown__"` 标签问题（[实测] `curl /metrics | grep rerank` 明明走账号级 key 仍标 unknown）。
**验收**：重启 server 后 `per-consumer tok/延迟` 仍可读回。
**依赖**：无（但 T5/T6 都依赖它）。

### T5 —— `mode=list` search 的三臂门（M）
**臂**：OFF（`rerank=False`）/ mini L2 终排（**停掉 α 混入 dir_queue**，`hierarchical_retriever.py:828/853` 重构为只在最终候选上排序）/ 本地 cross-encoder（jina-reranker-v3-mlx / bge-reranker-v2-m3 / qwen3-rerank 选一）。
**门禁**：§5 全部门。
**验收**：14 配对 query 全部指标出数，门判定写入 decision log。
**依赖**：T4（无持久基线则 A/B 无意义）。
**回滚**：call-site flag 单点 revert（`tools.py:340` 形态）。
**注**：这是本计划最大的投入项；若 T1 显示 search 路径实际使用频率极低（今日 13–15 次），可与用户确认是否降级为"仅做 OFF vs mini 终排"两臂。

### T6 —— experience / patch_merge 同门（M，依赖 T4）
**改动点**：`openviking/session/memory/tools.py:340` 的消费者白名单扩展到 experience / patch_merge。
**前置**：T4 计量给出真实单耗（现只能用 prefetch 形态外推，不可靠）。
**验收**：同 T5 协议。

### T7 —— localfs read error churn 根因（M，独立线）
**现象**：`read` error 12,401 且升（F9），零日志、无功能损伤证据。
**验收**：churn 归零，或产出根因报告（能解释为何 error 不落日志）。
**注**：不进 rerank 裁决，不阻塞其他任务。

### 不排期
- **O5** peer 树重复实体合并：与 rerank 裁决正交，语料问题另立项。
- **O7** 混合制（mini 粗排 + cross 精排）：仅当 T5 证明 OFF 排序实际伤害用户才启动。
- **#5431** Windows launcher readiness：macOS 零收益。
- **#5432**：仅当 T3 无法按步覆盖 thinking 时才需要。
- **2 个未合 upstream commit**：独立于本计划，按既有 sync 节奏处理。

---

## 5. 预注册指标（先锁标准，再看结果；禁止事后调门）

**样本**：n≥14 配对 query（kimi 主张 n≥30，glm 主张 14；取 14 起步，若方向不一致再扩到 30）。替换集协议，双盲 judge，judge 间 κ≥0.75。

**每消费方独立过门（四项全过才算过门）**：
1. 替换集共识分 ≥ OFF − 0.25，且逐 query 方向一致率 ≥70%
2. McNemar p<0.05（方法同 `benchmark/memory_organization/grader.py:218`）
3. p95(臂) − p95(OFF) ≤ +2s
4. tok/query ≤ 旧 ON 的 10%。**外部 search 旧 ON 基线（本次实测）**：~41k prompt tok/次、~12 calls/次、7–9s ⇒ 门 = ≤4.1k tok/次

**裁决规则**：三臂全不过 → 该消费方 `rerank=False` 定案，不翻案。过门 → 选达标臂中成本最低者。

**T3 门**：质量降幅 ≤0.25 且延迟降幅 ≥50%，缺一就回到 `thinking=True`。

**完成信号统一**：McNemar p / p95 / tok-per-query / commit 成功率 四项，缺项视为未完成。

---

## 6. 附录

### A. 判读记录（T1 执行后填写）
_待填：四个读数 + 结论_

### B. 原始对抗材料（保留可追溯）
| 轮次 | glm-5.3 | kimi-k2.8-preview |
|---|---|---|
| R1 立场书 | `subagent-artifacts/8934a50f-fdb7-413d-851a-0e9ae67120c3_delegate_output.md` | `b47bd1ab-e42c-4b36-8642-f4792950f36e_delegate_output.md` |
| R2 互批 | `38fc968d-4287-4c94-ac0c-f5cdc183e985_delegate_output.md` | `a1976c42-7fd1-46f3-8990-f3b174db2a11_delegate_output.md` |
| R3 决策备忘 | `e95c875e-5c48-423a-9d31-ed84a1df1d9c_delegate_output.md` | `0ed09070-5496-4da1-9c7b-437096e3197c_delegate_output.md` |

路径前缀：`~/.pi/agent/sessions/--Users-frankyang-mp2-Desktop-work-dev_proj-openviking-OpenViking--/`

Workflow run：`bb430003-301a-4a08-bf18-5457c6359ac0`

### C. 历史材料（本计划的前序，值得先读）
- `plans/rerank-mini-verification/optimization-report.md`（O1–O7、Round 3/4 账本）
- `plans/rerank-mini-verification/phase1/verdict.md`（9 query 双盲标注：OFF 1.569 vs ON 1.091）
- `plans/rerank-mini-verification/test-plan.md`、`test-report.md`（step-3 四门）
- `plans/rerank-mini-verification/reviews/`（前一轮三人对抗）
- `plans/ark-timeout-diagnosis.md`（O4 根因 §4/§4b）
