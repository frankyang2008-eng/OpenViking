# llm_score rerank 优化方案 Round 2（定稿）

评审方式：glm-5.3（架构与设计 7 维度）与 kimi-k3（实现质量与正确性 6 维度）各自独立只读评审当前 HEAD，随后 resume 原 run 互换全文报告交叉质证（每方逐点回应 + 验证对方 3 个可疑主张 + 互漏清单 + 修订 Top-N）。分歧由人工裁决，关键主张（伪造零分、区间乐观偏差、陈旧注释、预算切分）已在本机用真实函数运行时验证。评审原始产出：

- 第 1 轮：`.superpowers/sdd/llm-score-rerank-provider-design/r1-arch-glm53.md`、`r1-impl-kimik3.md`
- 第 2 轮：`.superpowers/sdd/llm-score-rerank-provider-design/r2-cross-arch-glm53.md`、`r2-cross-impl-kimik3.md`
- 聚焦 diff 包：同目录 `review-rerank-focused-c35990d1d..45deeb74c.diff`

范围：`llm_score` provider（mini chat 模型逐文档打分模拟 rerank）及其调用链，基线 = Round 1 方案（`plans/rerank-llm-score-optimization.md`）落地后的 HEAD（45deeb74c）。

---

## 0. 结论

**无 Critical。Round 1 的 8 项修复全部真实落地，未发现回归。** 本轮产出 1 项 Important + 一批 Minor，全部 S 工作量；两个新解析洞已运行时验证。整体判定：**架构健康，修完 Phase A 即可长期使用**。

## 1. Round 1 修复核验（双审一致，无分歧）

| Round 1 结论 | 状态 | 证据 |
|---|---|---|
| C1 process 共享 client + 专用 executor | 已落地✓ | `core.py:380-405`（`_init_shared_rerank_runtime`）；关闭 `core.py:669-678` 先 executor 后 client，均 `asyncio.to_thread` |
| 瞬断伪造 0 分 → NaN + 全失败 None | 已落地✓ | `llm_score_rerank.py:305-380`；retriever 侧 `_finite_score` 回退 |
| 结尾锚定 `_parse_score` | 已落地✓（本轮发现 2 个新洞，见 §2 P2） | `llm_score_rerank.py:81-106` |
| 重试 429/5xx/transport + Retry-After + equal-jitter | 已落地✓（语义小洞见 P8） | `llm_score_rerank.py:101-138,271-285` |
| thinking 地雷 warning | 已落地✓ | `from_config`（L367-379），测试覆盖 |
| 可观测性 record_error + 墙钟 | 已落地✓ | `llm_score_rerank.py:235-243,329-332`；collector 导出 `ERRORS_TOTAL{error_code,scope}` |
| memo 只缓存 finite | 已落地✓（顺序小洞见 P6） | `hierarchical_retriever.py:546-583` |
| batch_timeout + total_budget + executor 隔离 | 已落地✓ | `wait_for`（L548-560）；contextvars 显式拷贝（L483-485） |

测试密度高（两文件 60+ 用例，含乱序对齐、关闭等待 in-flight、重试预算递增）。唯一零覆盖关键路径：service 级 `_init_shared_rerank_runtime` + close 顺序（见 P9-T5）。

## 2. 本轮发现（已裁决合并，去重后 9 项）

### P1【Important】timeout 与 batch_timeout 失配 → brownout 下孤儿批堵塞双池，级联降级上界 ~72s

- `rerank_config.py:52-58`（timeout 默认 30）vs `:63-72`（batch_timeout 默认 8）；`llm_score_rerank.py:185-188` `httpx.Timeout(connect=5, read=30)`。
- `wait_for` 只切调用方等待；孤儿批在 ov-rerank executor + client 内部池上跑到自然结束：最坏 `5 + 30×2 + backoff ≈ 72s`（connect 相位独立计入，kimi 第一轮漏算、glm 补上，交叉轮已统一）。持续 429/5xx 风暴下 8 worker 全 orphan → 后续批排队撞 8s 超时 → 回退向量分 + 烧光 20s total_budget；`close()`（`core.py:660-680` `shutdown(wait=True)`）最坏被拖 ~72s。
- 实测单次评分仅 0.48-0.58s，30s read 余量过大。默认配置即可触发。
- **裁决修复（双审收敛）**：`from_config` 内 llm_score 专属 clamp，`read = min(timeout, batch_timeout + 2.0)`（batch_timeout=0 时不动）——+2s slack 让 "batch cut wins over transport" 的既有文档承诺（`rerank_config.py:84-86`）严格成立，orphan 上界收到 ~17-31s。**不采** kimi 的"全局默认降 15s"（6 provider 共用字段，波及面大；kimi 交叉轮已自行撤回）。同时 config description 补一句约束说明（两方都要求）。工作量 S。

### P2【Minor，已运行时验证】解析两洞：区间取上界 + 间隙数字伪造 0 分

本机用真实 `_parse_score` 执行验证（3.11）：

| 输入 | 现行为 | 问题 |
|---|---|---|
| `"85—90"` / `"85～90"` / `"85〜90"` | **90**（0.9） | `_SCORE_REJECT_CHARS`（`:39`）缺 U+2014 em-dash、全角/半角 tilde、U+301C wave dash；同族 `"85-90"` → None，行为不一致且取上界是乐观偏差 |
| `"1 0 0"` | **0**（0.0） | 间隙数字末位命中结尾锚定 → 伪造 0 分；threshold=0.05 下 0.0 直接掉出结果集 = recall 损失，比乐观偏差更伤 |
| `"相关性：85"` | 85 | 正确（kimi 第一轮矩阵误判 None，glm 交叉轮纠正，运行时确认） |
| `{"score":85}` | None | 正确（安全方向失败） |

- **裁决修复**：reject 字符集补 `"—", "~", "～", "〜"`；新增 `\d\s+\d` 间隙数字检查 → None（与 `"85分"`/`"Score: 85"` 合法形态不冲突，已推演）。各配 1 个测试。工作量 S。
- 顺带钉住已验证的既定行为防回归：`"相关性：85"`→0.85、`{"score":85}`→None。

### P3【Minor】parse 失败 / 全失败批的 token 消耗不上报

- `_score_one` 已构建 `usage_info`（`llm_score_rerank.py:213-219`），parse 失败分支丢弃（`:221-226`）；`rerank_batch` 的 `usages.append` 只在成功分支（`:340-341`），全失败早退不调 `update_token_usage`。
- API 调用已真实计费但 `TOKENS_INPUT_TOTAL` 记 0 → 线上"账单高于指标"排查盲区（parse 失败罕见，影响量低但成本核算必须完整）。
- **裁决**：失败路径保留 usage，`_score_one` 与 `rerank_batch` 两处都动（kimi 补的第二处采纳）。同批加 `rerank.partial_fallback_docs` 计数（P5 的观测半边）。工作量 S。

### P4【Minor】memo 免费命中被 budget 检查跳过

- `hierarchical_retriever.py:501-510` budget exhausted 直接 return fallback，memo 查找在 `:546` 之后；零耗时零成本的 memo 命中被丢弃。
- **裁决**：memo 过滤前置于 budget 检查（pending 为空直接返回）。边角改善：budget 耗尽但 memo 全命中时不再计 `rerank.skipped`——语义更准确。工作量 S。

### P5【Minor，双审分歧已裁决】partial 批分数尺度混合：只加观测，不加开关

- 现象属实：NaN→向量分使同批混合 LLM 绝对分（0-1）与 cosine 分（典型 0.3-0.8），`_finite_score` per-doc（retriever `:573-582`）。
- glm 提"整批回退可选开关"，kimi 反对（per-doc fallback 目标是保召回；混合方向不定，无数据前加开关是配置面膨胀）。
- **裁决采 kimi**：先加 `rerank.partial_fallback_docs` 计数指标（并入 P3 同 commit），数据证明有害再谈开关。

### P6【Minor】`CALLS_TOTAL` 语义是批数不是 API 调用数

- `record_call` 每批 +1（`collectors/rerank.py:104-109`），`:157` 注释自述 "a batch error is not a provider call" 与实际计数自相矛盾；token 计数却是 doc 级——容量/单价核算分母会错。
- **裁决**：只改 metric description 为 "rerank batches"（不破坏序列；改语义需 release note，不做）。工作量 S。

### P7【Minor】文档/注释三处失真（交叉轮互抓成果）

1. `_score_one` docstring "TokenUsageTracker is not thread-safe"（`llm_score_rerank.py:191`）与 `token_usage.py:133,140` 的 RLock 线程安全实现**直接矛盾**——陈旧注释误导维护者（双方第一轮都漏，kimi 交叉轮新发现）。
2. `base.py:53-64` 契约 docstring 只写 None 通道，NaN 双通道语义只存在于 provider 注释——下一个写 provider 的人读不到约定（glm）。
3. `batch_timeout=0` 时无任何兜底切刀（单批最坏 ~72s），config description 未提示；`timeout` 约束同 P1 一并补。
- 工作量 S（纯文档）。

### P8【Minor】Retry-After 超 cap 仍重试，浪费唯一重试

- `llm_score_rerank.py:277` cap 10s 后照常重试：服务端 `Retry-After: 60` → 等 10s 再撞 429，白耗唯一重试 + 10s worker 占用。
- **裁决采 glm 阈值**：`retry_after > batch_timeout` → 本次调用内不重试直接 terminal（重试结果不可能在批被切之前返回，这才是"浪费"的完整判据；绑 10s cap 是不完整版本）。工作量 S。

### P9【Minor】测试缺口最小新增清单（6 条，双审逐条表决采纳）

1. `test_llm_score_rerank.py::test_parse_score_emdash_tilde_wave_and_gap_digits` — 锁死 P2。
2. `...::test_retry_after_over_batch_timeout_skips_retry` — 锁死 P8。
3. `...::test_retry_after_float_header_accepted` — `"1.5"` 浮点 header 路径（现测试只覆盖 "0"/"2"）。
4. `test_hierarchical_retriever_rerank.py::test_round_gather_charges_budget_once` — 并行轮只计一次费（防回归 double-charge）。
5. service 级 `test_close_shuts_down_rerank_executor_before_client` — `_init_shared_rerank_runtime` + close 顺序目前**零覆盖**。
6. `...::test_batch_timeout_includes_executor_queue_wait` — 钉住排队时间计入 8s 的兜底语义（P1 的行为锚）。

## 3. 交叉轮错误主张更正记录（评审自身的纠错闭环）

| 原主张 | 更正 |
|---|---|
| kimi E-1：`"相关性：85"` → None | glm 反驳 + 运行时验证：→ 0.85（`：` 不在 reject 表） |
| kimi D2 表：`"85—90"` 只是 Minor | glm 升级：同族 `"1 0 0"`→0.0 是 recall 损失，运行时确认 |
| glm D11：生产 model 名不以 "doubao" 开头所以 warning 不生效 | kimi 反驳：`doubao-seed-2-0-mini` 字面即以 doubao 开头；但其底层机制（Ark `ep-xxx` endpoint id 绕过前缀检测）真实，值得在 P7 文档批补一句提示 |
| glm M-6：legacy 路径泄漏生产/回放可达 | kimi 验证 `playback.py:262` 不传 rerank_config → 不可达，降 P3（仅测试场景，legacy 分支加 warning 即可） |
| glm 优化 1：max_input_tokens=512 省 ~72% | kimi 纠正预算切分（`hierarchical_retriever.py:517` doc 拿全部余量非 1/4）→ 实际 ~35%，运行时确认 |
| kimi 第一轮 `_parse_score` 行号 192-219 | kimi 自我更正为 :81-106 |

## 4. 成本优化（双审方向一致、数字已重锚）

**生产配置 `max_input_tokens=512` 起步**（纯配置，S）：

- token 构成：固定 prompt（system+fewshot ≈250 tokens）约 9%，doc 内容约 90%——截断是最大结构性降本杠杆。
- 收益锚定 **~35%**（512 下短 query 的 doc 仍有 ~490 tokens；单 call ≈200 固定 + ≤512 ≈712 vs 实测 ~1097），**不是** glm 原估的 72%。
- 截断实现已确认温和：`truncate_text_to_token_budget` 保头 75%+尾 25%（`token_estimation.py:56-70`），memo 键用截断后文本确定性无问题，解析侧零风险（输出契约不变）。
- **前置条件**：13 条 query nDCG 回归（threshold=0.05 基线 0.992）；L1 overview（~2000 tokens）截 85% 才会实质伤质量，512 不触及。
- 前缀缓存/共享 system 前缀：**维持 Round 1 YAGNI 裁决**，双审无新论据，不翻案。listwise/cross-encoder 演进：`rerank_batch` 批形状契约不被阻塞（glm D6 确认），无需现在动作。

## 5. 执行计划

**Phase A（一个 commit 批次，全部 S，互不依赖）**：
P1 timeout clamp + config 描述 → P2 reject 字符 + 间隙数字 + 2 测试 → P3 失败 usage + partial_fallback_docs 计数 → P4 memo/budget 对调 → P6 CALLS_TOTAL description → P7 三处文档 → P8 Retry-After 阈值 → P9 测试 6 条（其中 5 随 P1、4 随 P4）。

**Phase B（配置动作 + 回归）**：`max_input_tokens=512` 上生产，跑 13 query nDCG 回归，成本收益按 ~35% 锚定验收。

**不做（裁决存档）**：全局 timeout 默认降值（kimi 撤回）；整批回退开关（等 P3 数据）；memo single-flight（M 工作量，等成本数据）；CALLS_TOTAL 语义变更（序列破坏）；前缀缓存（YAGNI）；listwise 预研（契约不阻塞，触发条件未到）。

## 6. 置信度

| 项 | 置信度 | 依据 |
|---|---|---|
| Round 1 八项修复落地、无回归 | 高 | 双审逐条 file:line + 测试存在 |
| P1 孤儿数学（~72s）与修复方向 | 高 | 双审独立收敛 + connect 相位补全；触发频率依赖 brownout（生产未观测） |
| P2 两洞 | 高 | 本机真实函数运行时验证（非推演） |
| P3/P4/P6/P8 | 高 | 代码路径直接可见，双方交叉确认 |
| 成本 ~35%（非 72%） | 高 | 预算切分公式运行时核验（`:517`） |
| max_input_tokens=512 质量无损 | 中 | 截断实现温和但需 nDCG 回归实证 |
| 整体「修完 Phase A 可长期使用」 | 高 | 无 Critical + 双审独立 OK-with-notes |
