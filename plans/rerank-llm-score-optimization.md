# llm_score rerank 优化方案（定稿）

评审方式：kimi-k3 与 glm-5.3 各自独立只读评审同一份建议清单（11 条），随后互换结论交叉质证，双方对每条分歧给出 file:line 或实测证据。本文是合并后的最终方案；评审原始产出见
`/Users/frankyang-mp2/.pi/workflows/projects/openviking-e59361b1283f/runs/rerank-concurrency-review-much14m9-hb3lz0.json.result-*`
（工作副本 `/tmp/rr/round1__*.md`、`/tmp/rr/round2__*.md`）。

范围：`llm_score` provider（mini chat 模型逐文档打分模拟 rerank）及其调用链。
基线事实：`concurrency=8`（每 client）、`max_retries=1`、`timeout=30`（httpx 分阶段）、实测单次打分 ~0.5s、一批 8 并发 ~0.82s、一次检索 rerank 增量 4-10s。

---

## 0. 四条已实测的关键事实（含推翻原判断的）

| # | 事实 | 证据 |
|---|---|---|
| F1 | **`_parse_score` 存在静默错分**：`"1000"`→0、`"1085"`→85、`"85．5"`→5、`"85。5"`→5、`"85."`→None | `.venv/bin/python` 直跑真实函数（3.11.15），29 case 矩阵 |
| F2 | 现有解析测试是**空断言**：`("1000", None)` 断言值 `0.0` 与解析结果 `0` 同值 → 红绿不可分 | `tests/unit/models/rerank/test_llm_score_rerank.py:242` |
| F3 | `concurrent.futures.CancelledError` MRO = `CancelledError → Error → Exception`（非 asyncio 别名）→ `except Exception` 可接住 | 本机实测 |
| F4 | asyncio 默认池尺寸**可配**：`server.executor_threads`（默认 0 = Python 默认 sizing） | `openviking/server/config.py:308`、`openviking/server/app.py:86` |

---

## 1. 变更集 1：零风险止血（先落，互不依赖）

### 1.1 `TokenUsageTracker` 加锁（前置条件，最高优先级）

- 文件：`openviking/models/vlm/token_usage.py`
- 现状：`update`(L135) / `get_total_usage`(L170) / `reset`(L185) / `to_dict`(L189) / `merge`(L216) 全部无锁，`_usage_by_model`(L147) 与 `usage_by_provider`(L74) 写入与遍历可并发。
- 为什么是前置：`llm_score_rerank.py:143` 注释声称"token 记账在 `rerank_batch` 的调用线程单点聚合"——但 `rerank_batch` 自己跑在 `asyncio.to_thread` 线程上且可并发（同一请求内多 typed query gather + 多请求）。并发后果：`self.prompt_tokens += x` 丢增量；`if provider not in ...: [...] = TokenUsage()` 双建丢数据；`to_dict()` 遍历时另一线程插入新 key → `RuntimeError: dictionary changed size during iteration`，被 `metrics/datasources/model_usage.py:104-110` 的 `except Exception: pass` 吞掉 → **/metrics 静默缺整个 rerank 块**。
- 改法：在 `TokenUsageTracker` 内加 `self._lock = threading.Lock()`，`update` / `to_dict` / `get_total_usage` / `get_all_usage` / `reset` / `merge` 全部 `with self._lock:`。`TokenUsage` / `ModelTokenUsage` 保持无锁（所有变更路径都经 tracker）。
- 附带收益：VLM 侧同一 tracker 共用，同类竞态一并消除。
- 验收：并发压测（20 并发搜索）下 `curl -s localhost:1933/metrics | grep rerank` 不再出现整块缺失；新增一个并发 update + to_dict 的线程压力测试（断言无异常、计数等于各线程之和）。

### 1.2 全批失败也上报错误事件

- 文件：`openviking/models/rerank/llm_score_rerank.py:252-257`
- 现状：`if failed == len(documents): return None` 在 `if failed:` 的 `record_error(...)` 之前返回 → **provider 整体不可用（最该报警的场景）在 `rerank.error` 指标里为 0**，设计文档的"观察期触发条件"因此永久失灵。
- 改法：把 `record_error` 调用上移到早返回之前（或两处都记），`error_code="all_failed"` 与 `"score_failed"` 区分。

### 1.3 `_parse_score` 四件套修复

- 文件：`openviking/models/rerank/llm_score_rerank.py:34-35, 55-70`
- 三个独立缺陷：
  1. 拒绝列表含 `"."` 而正则允许尾部 `[。.．]?` → `"85."` 被判 None（文档沉底，不是回退）；
  2. 正则无左边界 → `"1085"`→85、`"1000"`→0（**真实排序污染**，非测试覆盖到的 None）；
  3. 小数/千分位守卫缺全角变体 → `"85．5"`→5、`"85。5"`→5。
- 已验证的最终实现（29 case 全通过，`"85分."`→85 为有意接受的改进）：

```python
_SCORE_REJECT_CHARS = ("-", "－", "–", "/", "／")          # "." "," 已移除
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*分?\s*[。.．]?\s*$")
_DECIMAL_RE = re.compile(r"\d\s*[.,，．。]\s*\d")

def _parse_score(content: str) -> Optional[int]:
    text = content.strip()
    if not text or any(c in text for c in _SCORE_REJECT_CHARS):
        return None
    if _DECIMAL_RE.search(text):        # 小数 / 千分位 / 全角变体
        return None
    m = _SCORE_RE.search(text)
    if not m:
        return None
    score = int(m.group(1))
    return score if 0 <= score <= 100 else None
```

- 测试改造（关键）：现有矩阵经 `rerank_batch` 断言，**期望 None 与解析成 0 同值 → 空断言**。改为直接对 `_parse_score` 断言，并补：`"85."`→85、`"85分."`→85、`"1085"`→None、`"1000"`→None、`"10000"`→None、`"85．5"`→None、`"85。5"`→None、`"85，5"`→None、`"总分1085"`→None。

---

## 2. 变更集 2：轮内并行 + 观测归因（P0-2）

### 2.1 轮内 rerank 改 gather（**不采用拍平**）

- 文件：`openviking/retrieve/hierarchical_retriever.py:517-530`
- 现状：`search_children` 是 `asyncio.gather` 并行的，但 L527 在 `for` 循环里**逐个 await** `_rerank_scores` → 每轮 rerank 墙钟是 Σ 而非 max（4 目录 × 20 文档 × 8 worker = 12 波 vs 拍平/并行 ~10 波）。
- 改法：把该轮的 `_rerank_scores` 调用整体 `asyncio.gather`（每个目录仍是独立 batch），再按序 zip 回各自的 `results`。
- **为什么不拍平**（两模型一致否决，理由充分）：
  - 延迟等同——所有 batch 共用同一 client executor，总任务数相同，墙钟都是 `ceil(Σn/W)` 波；
  - 回退粒度退化——拍平后"单目录失败→该目录回退向量分"变成"整轮失败→整轮回退"；
  - 回归面更大——`tests/retrieve/test_hierarchical_retriever_rerank.py:276-277` 断言 `fake_client.calls[1] == ("hello", ["child A","child B"])`（每目录一次调用），拍平后必红；gather 保持每目录一次调用。
- 前置：先落 1.1（gather 把并发批次数从 1 提到 4，竞态窗口同步放大）。

### 2.2 telemetry 拆分归因

- 文件：`hierarchical_retriever.py:220`（`measure("search.vector_retrieval")` 只包第一次向量检索）、`:249`（叶子 rerank 在 measure 之外）、`:310`（`_recursive_search` 在 measure 之内 → 轮内 rerank 被算进向量检索）。
- 改法：新增 `search.rerank` 子 measure，把 leaf / directory / round 三处 rerank 全部纳入；`search.vector_retrieval` 回归纯向量。这是后续 A/B 对比的前置条件（现在同一指标混了 rerank 增量）。
- 同批加：`rerank.batches` / `rerank.documents` / `rerank.rounds` 计数（无预算，先埋点）。

---

## 3. 变更集 3：client 生命周期与并发归属（P0-1 + P1-3 + 卫生）

问题本体：`HierarchicalRetriever` 在 `_semantic.py:263`（find）与 `:476`（search）**每次调用新建** → 连带新建 `httpx.Client` + `ThreadPoolExecutor(8)`；`close()` 无调用方（L289-291 且顺序错）。

- 3.1 **共享 client**：service 层按 config 指纹缓存（显式 dict + 淘汰或 `lru_cache`），retriever 改为接受注入。设计文档 C1 的定级需要上调：它不只是"毫秒级 churn 卫生"，而是并发上限归属、连接复用、close 语义三件事的载体。
- 3.2 **独立有界 executor**：不要把 rerank 阻塞塞进默认 asyncio 池（`server.executor_threads` 默认 0 → 本机 12 槽）。默认池同时服务 metrics collector、OTEL exporter、parser、crypto —— rerank 批次（每批 1-3s、最多 ~14 批/搜索）会把它们饿住；反过来调大 `executor_threads` 只是把饥饿转嫁给别人。改为 service 持有的专用 `ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ov-rerank")`。
- 3.3 `_rerank_scores` 的 `asyncio.to_thread`（L399）改走专用 executor（`loop.run_in_executor(rerank_executor, ...)`）。注意：`to_thread` 会复制 contextvars，`run_in_executor` 不会 —— telemetry/observability 上下文需显式传递（`contextvars.copy_context().run`）。
- 3.4 **close 语义**：`shutdown(wait=True)` 之后再 `_client.close()`（现状先 shutdown(wait=False) 再 close，在飞 worker 会拿到 "client has been closed" → 记 per-doc 失败 → 0.0）。`cancel_futures=True` 不构成额外风险（F3 已证伪 `CancelledError` 逃逸），但仍建议 `wait=True`：不要把自己发出的请求转成"失败分"。service lifespan 统一 close。
- 3.5 `httpx.Client` 显式参数：`limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)`；显式声明 `trust_env` 策略（默认 True 会让服务器上的 `HTTP_PROXY`/`SSL_CERT_FILE` 静默改道模型端点）。
- 3.6 **lazy init**：`_rerank_client` 改懒构造 —— `find()` 恒为 QUICK（`_semantic.py:290`）却照样构造 client/executor 并打 doubao warning；`debug_service.py` 同类。顺带把 doubao warning 改成进程内一次性。
- 3.7 **并发上限归属**：3.1 完成后再决定是否加进程级 Semaphore。注意量级修正：单请求多 typed query 就能吃满默认池（N query × N batch），所以风险是"与其它 to_thread 工作互相饥饿 + 上限由部署参数意外决定"，而不是"96 路无上限放大"。
- 验收：并发 20 搜索时 `threading.active_count()` 不随请求数线性增长；`find()` 不再构造 client；进程关闭无 "client has been closed" 错误日志。

---

## 4. 变更集 4：超时预算（P0-3，**必须在 3 之后**）

- 4.1 `httpx.Timeout(connect=5, read=timeout, write=timeout, pool=5)`：现状 `timeout=30` 是分阶段超时，不是总时长；read 超时是"每次 socket 读"而非整响应，慢速滴流不受总时长约束。
- 4.2 批次级预算：`asyncio.wait_for(_rerank_scores(...), timeout=rerank_batch_timeout)`（建议 8s 起步，进 `RerankConfig`），超时即回退向量分 + 记 `timeout` 错误码。
- 4.3 为什么排在 3 之后：`wait_for` 取消的是 coroutine，**线程仍在跑完**。若仍走共享默认池，超时等于把线程泄漏到公共池里，饥饿被放大。
- 4.4 现状最坏量级（供设预算参考）：端点无响应时单批 20 文档 ≈ 3 波 × 2 次尝试 × ~30s ≈ 181s；每阶段都慢成功时才到数百秒（极端，不必按此设预算）。一次搜索最多约 14 批，且轮数**无硬上限**（变化轮会重置 `convergence_rounds`）。
- 验收：mock 一个 40s 不响应的端点 → 搜索在预算内返回向量分结果，日志一条 timeout 警告，`rerank.error{error_code="timeout"}` +1。

---

## 5. 变更集 5：数据驱动项（先埋点，后实现）

| 项 | 位置 | 说明 |
|---|---|---|
| 5.1 同批/跨阶段重复文档计数 | `rerank_batch` + `_rerank_scores` | 同一 abstract 会在 leaf / directory / 递归轮重复打分（语料多树重复放大）。先测重复率再决定 per-request memo（temperature=0 下确定性安全；**不要**用 contextvar，用 retriever 实例级 dict）。 |
| 5.2 error_code 拆分 | `llm_score_rerank.py:252+` | `parse_failed` / `http_failed` / `all_failed` / `timeout` 分开，否则"模型漂移"与"端点抖动"不可分。 |
| 5.3 每批 `doc_count` 字段 | `base.py` / `model_usage.py` | `rerank_calls_total` 对 llm_score 计的是"批"不是 HTTP 调用，补 doc_count 让口径自洽（不改名字）。 |
| 5.4 候选集预算 | `search_children(limit=max(limit*2,20))` | `limit=200` → 400 文档/批 = 400 次 chat 调用，无 cap。按 5.3 数据决定是否截 top-N。 |
| 5.5 退避 jitter | `llm_score_rerank.py:219-224` | **叠加**在退避之上（`delay = base + uniform(0, base*0.5)`），不要缩放 `Retry-After`（会违反服务器指定值）。 |
| 5.6 小修 | `hierarchical_retriever.py:281,526`；`llm_score_rerank.py` | `str(r.get("abstract",""))` 对 `None` 会送出字符串 `"None"` → 改 `(r.get("abstract") or "")`；usage fallback 的前缀常量按实测 ~231 tokens 计入（现漏 system+few-shot 前缀）；llm_score 未透传 `RerankConfig.extra_headers`（`openai_rerank.py:205` 有），企业网关场景会静默 401。 |

---

## 6. 明确不做（YAGNI / 已被裁决）

1. **拍平跨目录批次**（见 2.1 三条理由）。
2. **listwise 批量打分 / 前缀缓存 / per-doc 失败契约升级**：维持设计文档既有裁决，等 `rerank.error` 指标触发。
3. **硬编码 `MAX_TOTAL_ROUNDS=8`**：轮数无上限是真的，但先埋点（2.2）再定阈值。
4. **调大 `server.executor_threads` 当作修复**：那是把饥饿转移给 metrics/OTEL/parser。
5. **OKF 旧全文摘要归一**：需先量化有多少 legacy 记录命中，再决定。
6. **改 metric 名字**：补字段即可。

---

## 7. 总验收

```bash
pytest tests/unit/models/rerank tests/retrieve/test_hierarchical_retriever_rerank.py tests/unit/config -v
ruff check openviking openviking_cli tests && mypy openviking
```

- 变更集 1：新增并发压力测试 + 解析矩阵直测（`_parse_score`）。
- 变更集 2：`search.rerank` 与 `search.vector_retrieval` 分离；同一 query 前后对比 rerank 增量（基线 4-10s）。
- 变更集 3：并发 20 搜索的线程数上限 + `find()` 不构造 client + 关闭无错误日志。
- 变更集 4：40s 挂死端点的回退用例。

## 8. 实施结果（变更集 1 + 2 已落地，2026-09-22）

### 改动文件

| 文件 | 内容 |
|---|---|
| `openviking/models/vlm/token_usage.py` | `TokenUsageTracker` 加 `RLock`（`update`/`get_model_usage`/`get_all_usage`/`get_total_usage`/`reset`/`to_dict`/`merge`）；顺带修 `ModelTokenUsage.to_dict` 的 `Dict[str, Any]` 注解（原 mypy `[index]` 报错） |
| `openviking/models/rerank/llm_score_rerank.py` | 解析四件套（拒绝列表去掉 `.`/`,`、正则加 `(?<!\d)`、新增 `_DECIMAL_RE` 守卫全角 `[.,，．。]`）；新增 `_record_error()`；`all_failed` / `score_failed` 在 all-failed 早返回**之前**上报 |
| `openviking/retrieve/hierarchical_retriever.py` | 轮内 rerank 改 `asyncio.gather`（每目录仍独立 batch）；leaf/directory/round 三处加 `search.rerank` measure；`search.vector_retrieval` 移入 `_recursive_search` 只包子向量检索；`str(r.get("abstract") or "")` ×3 |
| `openviking/telemetry/operation.py` | `_SEARCH_DURATION_KEYS` 注册 `"rerank"`；`search_summary: Dict[str, Any]` 注解 |
| `tests/unit/models/rerank/test_llm_score_rerank.py` | 解析矩阵改直测 `_parse_score`（29 case，+18）；补 `"85."` 端到端；补 `all_failed` / `score_failed` 指标断言 |
| `tests/unit/models/vlm/test_token_usage.py`（新增） | 并发更新不丢计数（8 线程 × 200）；并发读不抛 `RuntimeError`；`merge` 持锁读源 |
| `tests/retrieve/test_hierarchical_retriever_rerank.py` | `FakeRerankClient` 加锁（游标并发安全）；新增 barrier 测试证明同轮多目录 rerank 真并行 |
| `tests/telemetry/test_execution.py` | search summary schema 增加 `rerank` |

### 验证证据

- `pytest tests/unit/models/rerank tests/unit/models/vlm tests/unit/config tests/telemetry -q` → **143 passed**
- `pytest tests/retrieve tests/unit tests/telemetry tests/server/test_api_search.py + 2 misc` → **2068 passed / 42 failed**；把改动 stash 回 HEAD 跑同一批 42 个 ID → **同样 42 failed**（git-config / langchain boundary / litellm VLM / ollama embedder / stats / skill pagination，与本次改动无关）→ 零回归
- **灵敏度证明**：新 barrier 测试在 HEAD（串行实现）上 FAIL（`serialized != []`），在修复后 PASS（`git checkout` 前后 sha256 校验一致）
- `ruff check`（全部改动文件）→ clean；`mypy openviking`：HEAD 1710 errors/235 files → 改动后 1695/228（净 -15）；两个被改源文件 file-scoped mypy 均为 0
- 解析器 29 case 矩阵对线上函数实测复跑通过

### 与计划的偏差（已披露）

1. **未加新 counter**（`rerank.batches` / `rerank.documents` / `search.rounds`）：telemetry counter 只能经 `TelemetrySummaryBuilder` 的显式白名单 + `telemetry_bridge` 的固定指标名进入 Prometheus，新增字段需连带改 schema 与 bridge（另需评审）。改为注册 `search.rerank` duration（1 行，立即可见），轮数仍由现有每目录 INFO 日志可见。建议与 5.3 的 `doc_count` 一起做 schema 变更。
2. `search.vector_retrieval` 的「纯向量」是通过把 measure 移入 `_recursive_search` 包住子向量检索实现的，而非删除指标。
3. 顺带纳入 5.6 的 `(abstract or "")`（3 处，正是本次编辑的行）。

### 剩余

变更集 3（共享 client + 专用 executor + close 语义 + lazy init）→ 变更集 4（超时预算，必须在 3 之后）→ 变更集 5（数据驱动）。

## 10. 实施结果（变更集 3 + 4，2026-09-22）

决策：**方案 A（service 层持有）** + **批内 8s / 请求级 20s**，且请求级预算实现为 **skip（准入控制）而非 cancel**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `openviking/models/rerank/base.py` | `RerankBase.close()` no-op：shutdown 路径无需探测方法存在性 |
| `openviking/models/rerank/llm_score_rerank.py` | `httpx.Timeout(connect=5, read/write=timeout, pool=5)` + `Limits(max_connections=concurrency)`；`close()` 改 `shutdown(wait=True)` 先于 `_client.close()` |
| `openviking_cli/utils/config/rerank_config.py` | `batch_timeout=8.0` / `total_budget=20.0`（`ge=0`，0 关闭） |
| `openviking/retrieve/hierarchical_retriever.py` | `rerank_client` / `rerank_executor` 注入参数（未注入时保留 `from_config` 兜底）；`RerankBudget`；`_rerank_scores` 支持 skip + `wait_for(batch_timeout)`；`_rerank_scores_timed`；`_run_rerank_batch`（`run_in_executor` + 显式 `copy_context`）；`_recursive_search(rerank_budget=…)` |
| `openviking/storage/viking_fs/__init__.py`、`_base.py` | `rerank_client` / `rerank_executor` 透传（`init_viking_fs`） |
| `openviking/storage/viking_fs/_semantic.py` | 2 个构造点传入注入的 client/executor |
| `openviking/service/core.py` | `_init_shared_rerank_runtime()`：进程级共享 client + `ThreadPoolExecutor(max_workers=concurrency, prefix="ov-rerank")`；`close()` 关 executor（wait）再关 client（`getattr` 保护，同 `_embedder` 先例） |
| `openviking/service/debug_service.py` | ObserverService/DebugService 复用共享 client（原来每次 /status 都建一个 client，且从不关闭） |

### 关键设计点

- **executor = 准入闸门**：runner pool（`max_workers=concurrency`）同时完成“线程隔离”与“进程级并发上限”，因此原计划的 3.7 单独 Semaphore **不再需要**（已取消）。
- **lazy init（3.6）被注入吸收**：生产路径不再每次 `find()`/`search()` 建 client；`find()` 恒为 QUICK 的浪费随之消失，且未注入时 `mode is None` 分支才触发构建，QUICK 不触发。
- **`run_in_executor` 不复制 contextvars**（`to_thread` 会），故显式 `copy_context().run(...)`，否则 `search.rerank` 归因与 telemetry 计数在 worker 线程丢失。
- **预算只计 rerank 时间**：顺序批次各自计时，并发轮整轮计一次，避免并发重复计数；embedding/向量检索不占用预算。

### 验证证据

- 新增测试：注入不调 `from_config`、`rerank_batch` 跑在 `ov-rerank*` 线程、批内超时回退、超预算跳过（0 关闭）、`close()` 等在飞批次不被打断、VikingFS 透传、config 默认/边界
- 聚焦套件：`tests/unit/service tests/retrieve/test_hierarchical_retriever_rerank.py tests/unit/models/rerank tests/unit/models/vlm tests/unit/config tests/telemetry` → **209 passed / 2 failed**，两个失败均在基线 42 名单内
- 全量回归：`tests/retrieve tests/unit tests/telemetry tests/server/test_api_search.py + misc` → **2083 passed / 43 failed**；与基线 42 逐条比对后唯一差额是 `test_retrieval_enable_intent.py` 这个文件不在基线采集的文件集内（同一测试在 HEAD 上同样失败，见下）
- `tests/misc` 整目录 HEAD vs 工作树：**32 vs 32，新增 0、修复 0**（该目录含上述文件，是权威对照）
- `ruff check` 全部改动文件 clean（含 `--fix` 修正 debug_service 预先存在的 I001）
- `mypy openviking`：HEAD 1710 errors/235 files → 改动后 1695/228；所有被改源文件 file-scoped 无报错（并顺手修掉 `_base.py:170` 的 `sched_getaffinity` 平台差异报错，按仓库既有约定加 targeted ignore）

### 验证中发现并修掉的自身回归（2 类）

1. **`close()` 直读 `self._rerank_executor`**：测试用 `OpenVikingService.__new__(OpenVikingService)` 绕过 `__init__` → 3 个 close 测试 `AttributeError`。改 `getattr` 守卫（与同函数内 `getattr(self, "_embedder", None)` 一致）后恢复。
2. **4 个 test double 的固定构造签名**：`tests/misc/test_vikingfs_find_without_rerank.py`（3 处）与 `tests/misc/test_retrieval_enable_intent.py`（1 处）的 `FakeRetriever.__init__` 需随生产签名增长。已同步（生产不为迁就测试假体而变形）。

### 剩余

变更集 5（数据驱动）：重复文档率埋点 → 决定是否做 per-request memo；`error_code` 拆分（`parse_failed`/`http_failed`/`timeout`/`all_failed`）；每批 `doc_count` 字段（与 counter schema 变更一起）；候选集预算；退避 jitter；`extra_headers` 透传。

## 11. 原始建议中被推翻/修正的点（透明记录）

1. `"1000"` 不是 None 而是 **0**（原测试空断言掩盖），且 `"1085"`→85 是真实污染 → 解析修复升级为 P0 内容。
2. `cancel_futures=True` 的 `CancelledError` 逃逸风险**不成立**（F3）。
3. 并发上限不是"无上限 96"：受 `server.executor_threads`（默认 Python sizing）+ 默认池共享约束 → 表述改为"上限归属错误 + 与其它 to_thread 工作互相饥饿"。
4. 我的"拍平轮内批次"方案被两模型一致否决 → 改 gather。
5. `_recursive_search` 内 rerank 计入 `search.vector_retrieval`（原判断只说"串行"）→ 追加观测归因项。
6. 全批失败不上报 `record_error`（原清单漏项）→ 新增 1.2。
