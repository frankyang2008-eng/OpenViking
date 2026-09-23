# CONTEXT.md — 术语表

本文件是项目领域术语表，只收词汇定义，不收实现细节。

## Rerank

**Rerank（重排）**：检索的第二阶段精排。统一契约为 `rerank_batch(query, documents)`，返回与输入同序的 0.0-1.0 浮分；返回 `None` 表示整批失败，调用方回退到向量相似度分。

**Rerank provider**：rerank 后端类型。当前有 vikingdb、cohere、openai、litellm、jev、llm_score。

**llm_score provider**：用 chat 模型逐文档打分（pointwise scoring）的 rerank 后端。每个 (query, document) 对独立调用一次 chat completion，输出 0-100 整数分映射为 0.0-1.0。分数是 LLM 绝对判断，跨 query 会漂移，threshold 过滤语义弱于专用 rerank 模型。

**Scorer（打分器）**：本方案中指代执行相关性判断的 chat 模型（如 doubao-seed-2.0-mini）。它不是 rerank 模型：效果弱于 cross-encoder，强于纯向量检索。

**L0 abstract**：约 100 tokens 的内容摘要，是 rerank 的实际输入文档（不是全文）。

**确定性异常**：rerank 验证场景分类之一——输入可复现、被测行为可精确断言的异常输入或故障注入（如超时、429、畸形响应、非阿拉伯数字分数）。用离线 mock 测试，精确断言。

**概率性灰区**：rerank 验证场景分类之一——scorer 真实行为产生、只能统计分布不能逐例断言的输出（如分数漂移、注入鲁棒性、区间回答率）。用 live API 加分布统计，阈值型断言。

**竞态时序**：rerank 验证场景分类之一——多线程、超时、预算交错产生的非确定性执行路径（如 orphan 批占池、budget 边界、close 与 in-flight 交错）。用真实线程加不变量断言。
