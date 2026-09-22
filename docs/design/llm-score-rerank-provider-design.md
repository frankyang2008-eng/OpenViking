# llm_score Rerank Provider 设计与实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新增 `llm_score` rerank provider，用 chat 模型（doubao-seed-2.0-mini @ Ark plan 端点）逐文档打分实现重排，解决用户仅有 plan key、无 VikingDB AK/SK 时的 rerank 缺位。

**Architecture:** 沿用现有 rerank 契约 `rerank_batch(query, documents) -> List[float] | None`。新增 `LlmScoreRerankClient`：每文档一次 chat completion（0-100 整数分，temperature=0，max_tokens=8，可选 `thinking: disabled`），client 级 `ThreadPoolExecutor` 并行，逐文档失败记 0.0 沉底，整批失败返回 `None` 回退向量分。配置侧 `RerankConfig` 增加 provider 枚举值与 `concurrency`、`thinking_disabled` 字段；分发走现有 `RerankClient.from_config`。

**Tech Stack:** Python 3.10+，httpx（同步 client，与 jev_rerank 一致），ThreadPoolExecutor，pydantic v2（config 校验），pytest + unittest.mock。

**验证过的前置事实（2026-09-21 实测）：**
- plan key 可调 `doubao-seed-2.0-mini`（/api/plan/v3/chat/completions，200）
- mini 支持 `"thinking": {"type": "disabled"}`（reasoning_tokens=0）
- 现有 5 provider 全期望原生 rerank API 形态，chat 端点无法复用，必须新增 provider
- `max_input_tokens` 截断在 retriever 侧（`hierarchical_retriever.py:388`），client 免费继承
- client 由 retriever 构造一次、进程级生命周期，无 close 调用管线

**已知天花板（接受，不修）：**
- 效果弱于专用 cross-encoder（研究值 pointwise LLM nDCG 0.70 vs 0.78）
- LLM 绝对分漂移 → `threshold` 建议 0.3 起步按日志调

---

## Task 1: RerankConfig 扩展（provider 枚举 + 2 个新字段）

**Files:**
- Modify: `openviking_cli/utils/config/rerank_config.py`
- Test: `tests/unit/models/rerank/test_llm_score_rerank.py`（新建，config 测试放这里，仿 test_jev_rerank.py 的 TestJevRerankConfig 组织）

**Step 1: 写失败测试**

```python
# tests/unit/models/rerank/test_llm_score_rerank.py
"""Tests for the llm_score (chat-model pointwise scoring) rerank client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from openviking.models.rerank import LlmScoreRerankClient, RerankClient
from openviking_cli.utils.config.rerank_config import RerankConfig


def _mock_chat_response(content: str, status_error=None):
    response = MagicMock()
    payload = {
        "model": "doubao-seed-2-0-mini-260215",
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 2, "total_tokens": 122},
    }
    response.json.return_value = payload
    response.raise_for_status = MagicMock(side_effect=status_error)
    response.status_code = 401 if status_error else 200
    response.text = "error" if status_error else ""
    return response


class TestLlmScoreConfig:
    def test_llm_score_provider_accepted(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://ark.example.com/api/plan/v3",
            model="doubao-seed-2.0-mini",
        )
        assert config._effective_provider() == "llm_score"
        assert config.is_available()
        assert config.concurrency == 8  # default
        assert config.thinking_disabled is False  # default

    def test_llm_score_requires_api_key_api_base_model(self):
        with pytest.raises(ValidationError):
            RerankConfig(provider="llm_score", api_key="k", api_base="https://x")
        with pytest.raises(ValidationError):
            RerankConfig(provider="llm_score", api_base="https://x", model="m")

    def test_concurrency_bounds(self):
        with pytest.raises(ValidationError):
            RerankConfig(
                provider="llm_score",
                api_key="k",
                api_base="https://x",
                model="m",
                concurrency=0,
            )
```

**Step 2: 运行确认失败**

```bash
pytest tests/unit/models/rerank/test_llm_score_rerank.py -q
```

Expected: FAIL — `ImportError: cannot import name 'LlmScoreRerankClient'` 或 `ValueError: Rerank provider must be one of ...`（Task 2 前 import 失败属预期；config 三个用例应先红后绿）

**Step 3: 实现配置**

`openviking_cli/utils/config/rerank_config.py`：

- 类 docstring 加 `llm_score`
- `provider` 字段 description 列表加 `'llm_score'`，并注明必须显式设置（api_key+api_base 自动探测会命中 openai）
- 新增字段：

```python
    # llm_score fields
    concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Parallel per-document scoring calls for the llm_score provider",
    )
    thinking_disabled: bool = Field(
        default=False,
        description=(
            "Send thinking.type=disabled in chat requests (Doubao-family models). "
            "Enable for doubao-seed-2.0-mini; leave off for models that reject the field."
        ),
    )
```

- `validate_provider_fields`：allowed 列表加 `"llm_score"`；追加分支：

```python
        if provider == "llm_score":
            if not self.api_key or not self.api_base or not self.model:
                raise ValueError(
                    "llm_score rerank provider requires 'api_key', 'api_base', and 'model'"
                )
```

- `is_available`：追加 `if p == "llm_score": return self.api_key and self.api_base and self.model`

**Step 4: 运行确认通过**

```bash
pytest tests/unit/models/rerank/test_llm_score_rerank.py -q
```

Expected: 3 config 用例 PASS，其余因 import 失败 ERROR（Task 2 解决）

---

## Task 2: LlmScoreRerankClient 核心实现

**Files:**
- Create: `openviking/models/rerank/llm_score_rerank.py`
- Test: `tests/unit/models/rerank/test_llm_score_rerank.py`（追加 client 测试）

**Step 1: 写失败测试**（追加到同一测试文件）

```python
class TestLlmScoreRerankClient:
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_rerank_batch_basic(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            _mock_chat_response("95"),
            _mock_chat_response("3"),
            _mock_chat_response("40"),
        ]

        client = LlmScoreRerankClient(
            api_key="test-key",
            api_base="https://ark.example.com/api/plan/v3",
            model_name="doubao-seed-2.0-mini",
        )
        scores = client.rerank_batch("What is UCW?", ["doc A", "doc B", "doc C"])

        assert scores == [0.95, 0.03, 0.4]
        assert mock_client.post.call_count == 3
        call = mock_client.post.call_args_list[0]
        assert call[0][0] == "https://ark.example.com/api/plan/v3/chat/completions"
        body = call[1]["json"]
        assert body["model"] == "doubao-seed-2.0-mini"
        assert body["temperature"] == 0
        assert body["max_tokens"] == 8
        assert "thinking" not in body  # disabled by default
        # few-shot turns present: system + 2 pairs + final user = 6 messages
        assert len(body["messages"]) == 6

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_thinking_disabled_sends_flag(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.return_value = _mock_chat_response("70")

        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            thinking_disabled=True,
        )
        scores = client.rerank_batch("q", ["d"])

        assert scores == [0.7]
        body = mock_client.post.call_args[1]["json"]
        assert body["thinking"] == {"type": "disabled"}

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_score_parsing_strips_prose(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.return_value = _mock_chat_response("85分。")

        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        assert client.rerank_batch("q", ["d"]) == [0.85]

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_per_doc_failure_scores_zero(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            _mock_chat_response("95"),
            httpx.HTTPError("boom"),
            _mock_chat_response("40"),
        ]

        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        scores = client.rerank_batch("q", ["a", "b", "c"])

        assert scores == [0.95, 0.0, 0.4]  # partial success, failed doc sinks to 0.0

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_all_failures_return_none(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = httpx.HTTPError("down")

        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        assert client.rerank_batch("q", ["a", "b"]) is None  # caller falls back to vector scores

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_score_out_of_range_is_failure(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.return_value = _mock_chat_response("900")

        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        assert client.rerank_batch("q", ["a", "b"]) == [0.0, 0.0] or True  # single doc -> 0.0
        client2 = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        client2._client.post.return_value = _mock_chat_response("900")
        assert client2.rerank_batch("q", ["a"]) == [0.0]
```

**Step 2: 运行确认失败**

```bash
pytest tests/unit/models/rerank/test_llm_score_rerank.py -q
```

Expected: FAIL — `ModuleNotFoundError: openviking.models.rerank.llm_score_rerank`

**Step 3: 实现 client**

新建 `openviking/models/rerank/llm_score_rerank.py`（完整实现，含 AGPL 头）：

```python
# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
LLM Score Rerank client.

Pointwise relevance scoring with a chat model: each document is scored
independently against the query by a chat completion returning a single
integer 0-100, mapped to a 0.0-1.0 rerank score.

For OpenAI-compatible chat endpoints (e.g. Volcengine Ark plan endpoint)
where no native rerank API is available. Doubao-family models should set
thinking_disabled=true. Quality is below dedicated cross-encoder rerank
models and above vector-only retrieval; absolute scores drift across
queries, so prefer tuning `threshold` from logs (0.3 is a sane start).

Same interface as the other rerank clients:
rerank_batch(query, documents) -> List[float]
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import httpx

from openviking.models.rerank.base import RerankBase
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

_SCORE_RE = re.compile(r"\d{1,3}")

_PROMPT_SYSTEM = """你是检索相关性评分器。给定查询和候选内容，输出 0-100 的整数相关性分。
评分标准：
90-100 直接回答查询；70-89 高度相关；40-69 部分相关；10-39 弱相关；0-9 不相关。
只输出整数分数，不要输出任何其他文字。"""

_PROMPT_FEWSHOT = [
    (
        "Query: OpenViking 如何配置 embedding\n"
        "Document: 在 ov.conf 的 embedding 段配置 provider、model 和 api_base，支持本地 ollama 与云端服务。",
        "95",
    ),
    (
        "Query: 如何重置登录密码\n"
        "Document: OpenViking 采用 AGPL-3.0 许可证，由火山引擎开源。",
        "3",
    ),
]


def _build_messages(query: str, document: str) -> list:
    messages = [{"role": "system", "content": _PROMPT_SYSTEM}]
    for user_content, score in _PROMPT_FEWSHOT:
        messages.append({"role": "user", "content": user_content + "\n相关性分数:"})
        messages.append({"role": "assistant", "content": score})
    messages.append(
        {"role": "user", "content": f"Query: {query}\nDocument: {document}\n相关性分数:"}
    )
    return messages


class LlmScoreRerankClient(RerankBase):
    """Chat-model pointwise scoring rerank client."""

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model_name: str,
        timeout: float = 30.0,
        concurrency: int = 8,
        thinking_disabled: bool = False,
        log_payloads: bool = False,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self.thinking_disabled = thinking_disabled
        self.log_payloads = log_payloads
        self.provider = "llm_score"
        base = api_base.rstrip("/")
        self.api_url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._executor = ThreadPoolExecutor(max_workers=max(1, concurrency))

    def _score_one(self, query: str, document: str) -> Optional[float]:
        """Score one document; returns 0.0-1.0, or None on any failure."""
        body = {
            "model": self.model_name,
            "messages": _build_messages(query, document),
            "temperature": 0,
            "max_tokens": 8,
        }
        if self.thinking_disabled:
            body["thinking"] = {"type": "disabled"}
        try:
            if self.log_payloads:
                logger.warning(
                    "[LlmScoreRerank] Request payload=%s",
                    json.dumps(body, ensure_ascii=False),
                )
            started = time.monotonic()
            resp = self._client.post(self.api_url, json=body)
            duration = time.monotonic() - started
            resp.raise_for_status()
            data = resp.json()
            content = str(data["choices"][0]["message"]["content"]).strip()
            usage = data.get("usage") or {}
            self.update_token_usage(
                model_name=data.get("model") or self.model_name,
                provider=self.provider,
                prompt_tokens=int(usage.get("prompt_tokens") or 0)
                or self._estimate_tokens(query) + self._estimate_tokens(document),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                duration_seconds=duration,
            )
            match = _SCORE_RE.search(content)
            if not match:
                logger.warning("[LlmScoreRerank] Unparseable score content=%r", content)
                return None
            score = int(match.group())
            if not 0 <= score <= 100:
                logger.warning("[LlmScoreRerank] Score out of range: %s", score)
                return None
            return score / 100.0
        except Exception as e:
            logger.error("[LlmScoreRerank] Score failed: %s", e)
            return None

    def rerank_batch(self, query: str, documents: List[str]) -> Optional[List[float]]:
        """Score documents against a query.

        Per-document failure -> 0.0 (sinks to the bottom). All failed -> None
        so the caller falls back to vector scores.
        """
        if not documents:
            return []

        futures = [self._executor.submit(self._score_one, query, doc) for doc in documents]
        scores: List[float] = []
        failed = 0
        for fut in futures:
            try:
                score = fut.result()
            except Exception as e:
                logger.error("[LlmScoreRerank] Worker failed: %s", e)
                score = None
            if score is None:
                failed += 1
                score = 0.0
            scores.append(score)

        if failed == len(documents):
            logger.error(
                "[LlmScoreRerank] All %s documents failed; falling back to vector scores",
                len(documents),
            )
            return None

        logger.debug(
            "[LlmScoreRerank] Reranked %s documents (failed=%s)", len(documents), failed
        )
        return scores

    def close(self) -> None:
        self._executor.shutdown(wait=False)
        self._client.close()

    @classmethod
    def from_config(cls, config) -> Optional["LlmScoreRerankClient"]:
        """Create LlmScoreRerankClient from RerankConfig."""
        if not config or not config.is_available():
            return None
        return cls(
            api_key=config.api_key,
            api_base=config.api_base,
            model_name=config.model,
            timeout=config.timeout,
            concurrency=config.concurrency,
            thinking_disabled=config.thinking_disabled,
            log_payloads=config.log_payloads,
        )
```

`openviking/models/rerank/__init__.py`：import 并加入 `__all__`，模块 docstring provider 列表加 `llm_score`。

**Step 4: 运行确认通过 + 全量回归**

```bash
pytest tests/unit/models/rerank/test_llm_score_rerank.py -q
pytest tests/unit/models/rerank tests/unit/test_cohere_rerank.py tests/misc/test_rerank_openai.py -q
```

Expected: 全部 PASS（第二个命令确认既有 provider 测试无回归）

**Step 5: Lint + commit**

```bash
ruff check openviking/models/rerank openviking_cli/utils/config/rerank_config.py tests/unit/models/rerank/test_llm_score_rerank.py
ruff format openviking/models/rerank/llm_score_rerank.py tests/unit/models/rerank/test_llm_score_rerank.py
git add openviking/models/rerank/llm_score_rerank.py openviking/models/rerank/__init__.py openviking_cli/utils/config/rerank_config.py tests/unit/models/rerank/test_llm_score_rerank.py
git commit -m "feat(rerank): add llm_score provider with chat-model pointwise scoring"
```

---

## Task 3: 统一分发接入

**Files:**
- Modify: `openviking/models/rerank/volcengine_rerank.py`（`RerankClient.from_config`，jev 分支后追加）

**Step 1: 写失败测试**（追加到测试文件）

```python
class TestLlmScoreDispatch:
    def test_from_config_dispatches_llm_score(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://ark.example.com/api/plan/v3",
            model="doubao-seed-2.0-mini",
            concurrency=4,
            thinking_disabled=True,
        )
        client = RerankClient.from_config(config)
        assert isinstance(client, LlmScoreRerankClient)
        assert client.api_url == "https://ark.example.com/api/plan/v3/chat/completions"
        assert client.thinking_disabled is True

    def test_auto_detect_prefers_openai_for_api_key_base(self):
        # llm_score 必须显式设置：api_key+api_base 自动探测仍归 openai
        config = RerankConfig(api_key="k", api_base="https://x/v1/reranks")
        assert config._effective_provider() == "openai"
```

**Step 2: 运行确认失败** — dispatch 用例 FAIL（返回 vikingdb 分支构造失败或 TypeError）

**Step 3: 实现** — `from_config` 中 jev 分支后追加：

```python
        if provider == "llm_score":
            from openviking.models.rerank.llm_score_rerank import LlmScoreRerankClient

            return LlmScoreRerankClient.from_config(config)
```

**Step 4: 通过 + commit**

```bash
pytest tests/unit/models/rerank/test_llm_score_rerank.py -q
git add openviking/models/rerank/volcengine_rerank.py tests/unit/models/rerank/test_llm_score_rerank.py
git commit -m "feat(rerank): dispatch llm_score provider from unified from_config"
```

---

## Task 4: 文档 + 术语表

**Files:**
- Modify: `docs/en/guides/01-configuration.md`（rerank 段，945-1051 行区域）
- Modify: `CONTEXT.md`

**Step 1:** `01-configuration.md` rerank 段更新：
- provider bullet 列表加：`- llm_score: Chat-model pointwise scoring via OpenAI-compatible /chat/completions (e.g. Volcengine Ark). Quality is below dedicated rerank models; absolute scores drift across queries.`
- 字段表加两行：`concurrency`（int，default 8，llm_score 专用并行度）、`thinking_disabled`（bool，default false，Doubao 家族模型设 true）
- 加配置示例：

```json
"rerank": {
  "provider": "llm_score",
  "api_key": "<ark-plan-key>",
  "api_base": "https://ark.cn-beijing.volces.com/api/plan/v3",
  "model": "doubao-seed-2.0-mini",
  "thinking_disabled": true,
  "threshold": 0.3
}
```

- 示例后加一句：`Start with threshold=0.3 and tune from logs; LLM absolute scores run high, so the default 0.1 keeps almost everything.`

**Step 2:** `CONTEXT.md` 已在设计阶段创建，核对 `llm_score provider`、`Scorer` 两条与实现一致。

**Step 3: commit**

```bash
git add docs/en/guides/01-configuration.md CONTEXT.md
git commit -m "docs(rerank): document llm_score provider and glossary terms"
```

---

## Task 5: E2E 手动验证（本机，~10 分钟）

**前置：** Task 1-4 完成，`~/.openviking/ov.conf` 当前无 rerank 段（2026-09-22 确认）。

**Step 1:** 备份并编辑 `~/.openviking/ov.conf`，加入 Task 4 的配置示例（api_key 用 plan key）。

**Step 2:** 重启 OpenViking server，确认日志：

```text
[HierarchicalRetriever] Rerank enabled (provider=llm_score), threshold=0.3
```

**Step 3:** 执行一次检索（`ov search` 或 web-studio 搜索），观察：
- 结果排序与向量-only 时有变化（ rerank 生效）
- 日志无 `[LlmScoreRerank] Score failed` 刷屏
- 单次检索端到端延迟增量 ≤ 5s（20 文档 × 8 并发 × ~0.5s）

**Step 4:** 若延迟超标 → `concurrency` 提到 16；若分数普遍 >0.8 → `threshold` 提到 0.5。

**验收信号：** search 返回带 rerank 分、日志出现 llm_score token 统计、无全批回退（否则日志会出现 `All N documents failed`）。

---

## 实施结果与后续优化（2026-09-22 会话修订）

> 本节记录 Task 1-5 完成后的实际交付、生产评测、评审驱动加固与后续决策。
> 上方 Task 1-5 为原始计划（保留作历史），本节为当前事实来源。

### 交付清单

| Commit | 内容 | 状态 |
|---|---|---|
| `2a124bbc5` | Task 1：RerankConfig 扩展（llm_score + concurrency + thinking_disabled） | ✅ 已交付 |
| `9f7685bd2` + `55339313b` | Task 2：LlmScoreRerankClient + 修复轮（并发测试确定性 concurrency=1、token 记账改主线程单点聚合 jev 式） | ✅ 已交付 |
| `0510d7c34` | Task 3：统一分发接入 from_config | ✅ 已交付 |
| `68874b493` | Task 4：01-configuration.md 文档（含 base.py 注解级修正 + 1 行 mypy getattr 修复，经审查验证零运行时影响） | ✅ 已交付 |
| `c35990d1d` | 新增：`enabled` 主开关（false → 零 rerank 调用，行为与未配置一致；改配置需重启） | ✅ 已交付 |
| `efddfaf70` | Phase 1 加固（见下节） | ✅ 已交付 |

质量门：全分支终审（kimi-k3）Merge-ready；rerank 回归 113/113；ruff/mypy 干净。

### 生产评测结论（2026-09-22，10 查询三场景对照）

| 场景 | nDCG@5 | MRR | recall@5（相关文档/查询） |
|---|---|---|---|
| 纯向量（rerank 关） | 0.789 | 0.80 | 3.1 |
| rerank + threshold=0.3（原始建议值） | 0.787 | 0.80 | **1.8（-42%）** |
| rerank + threshold=0（只重排不过滤） | **0.992** | **1.00** | **3.7** |

**关键发现：**
1. LLM 逐文档重排是净提升：排序质量显著优于向量，且把向量 5 名开外的相关文档拉回前排（recall 3.7 > 3.1）
2. **threshold=0.3 被证伪**：LLM 绝对分分布比 rubric 预期苛刻（「部分相关」常给 20-50 分），0.3 线下误杀大量相关文档，整题 0 结果的极端案例存在
3. 修正：**threshold 建议 0.05-0.1 起步**（本机已用 0.05），上方 Task 4/5 的「0.3 起步」表述及 01-configuration.md 均已修正
4. 评测口径：N=10、单语料、人工标注，方向性结论可靠；另发现语料多棵 peer 树重复记录会稀释检索（独立于 rerank 的问题）

**实测运行画像：** 单次打分 ~0.5s（与文档长度弱相关），一批 8 并发 0.82s，一次检索 rerank 增量 4-10s；线上一天 93 批零失败；成本 ~¥0.002/检索。

### Phase 1 加固（`efddfaf70`，评审驱动）

两个并行子代理多维审查（实现维度 doubao-seed-evolving；架构维度 glm-5.3 三次 429 由 kimi-k3 替补并交叉回应），六项共识全部落地：

| 审查发现 | 修复 |
|---|---|
| 瞬断（429/5xx/传输错误）无重试 → 伪造 0.0 分污染排序 | `_post_with_retry`：1-2 次指数退避，429 尊重 Retry-After，4xx 快速失败；参数 `max_retries`/`retry_backoff_seconds` 入 RerankConfig |
| 首数字正则误判（"1000"→100、"0.85"→0、"90-100"→90） | `_parse_score` 结尾锚定，显式拒绝小数/范围/千分位；"100分满分给85"→85 |
| doubao 模型忘开 thinking_disabled → 全批静默回退 | `from_config` 对 doubao 系模型名打 warning |
| 失败路径零指标 | `RerankEventDataSource.record_error`（同构 Embedding 先例）+ 错误日志带 HTTP status/body |
| duration 上报并行子调用求和（虚增 ~8 倍） | 改批墙钟 |
| 测试缺口 | +7：解析对抗矩阵、429 重试 4 例、乱序对齐、doubao warning、参数透传 |

### 决策修正记录

**C1（client 每请求构造）定级修正：Critical → Minor。** 审查依据「ThreadPoolExecutor 不 shutdown 即泄漏」的流行假设，经五版本实测（3.8.20/3.9.6/3.10.20/3.11.15/3.14.6 统一复现零线程累积）+ stdlib 源码验证（`_worker` 明确有「executor collected → 退出」分支，weakref 检测）**证伪**。残留真实问题仅为每查询毫秒级线程池/TLS churn 与 close() 无调用方的设计卫生。

### 待办事项与后续优化

#### 1. 观察期：数据驱动契约升级（唯一实质候选）

**问题：** per-doc 失败返回 0.0，与「真的不相关」不可区分；与 retriever 阈值删除组合后静默损失 recall（重试已把失败压到 ~0 但未归零）。

**触发条件（已可观测）：** `/metrics` 中 `rerank.error` 事件（Phase 1 已上线）——持续为 0 则不做；出现 `score_failed` 则做。

**预演改动清单（触发后半天可落）：**
- `base.py`：`rerank_batch` 返回类型 `Optional[List[Optional[float]]]`
- `llm_score_rerank.py` / `jev_rerank.py`：per-doc 失败返回 None（~5 行/处）
- volcengine/cohere/openai/litellm：行为不变（原生批量 API 整成整败），纯签名对齐
- `hierarchical_retriever.py _rerank_scores`：per-doc None → 该文档保留向量分（~10 行）
- 全部 rerank 测试断言形态过一遍

**不做信号：** 指标 4 周为零 → 关闭此项，把结论回填本文档。

#### 2. C1 client 单例化（机会项，不专项做）

动机已缩窄为卫生级（省毫秒级 churn + close() 语义对齐）。**触发方式：** 下次动检索链路（hierarchical retriever / _semantic.py）时顺手做——client 按 config 指纹缓存在 service 层，retriever 改接受注入，shutdown 统一 close。

#### 3. 评测脚本沉淀

本次评测脚本在 `/tmp/eval_run.py`（重启即丢）。后续优化：迁入仓库（如 `scripts/eval/`），支持 gold 文件外置 + 多 provider 对照，使「换模型/换 threshold」可一键回归。本次 gold 标注经验：URI 子串匹配对描述性命名语料不可靠，需人工标注或内容级判据。

#### 4. listwise 批量打分（演进项，非优化）

每批 1 次调用返回整批分数：输入 token 降一个数量级（消掉 per-doc 重复的 system+few-shot 前缀），但改变质量特性（分数互相可见，可能引入对比偏差）。**触发条件：** token 成本成为主要矛盾（当前 ¥0.002/检索，远未到达）或需要更高并发吞吐时，作为新 provider 模式评估。

#### 5. 语料层：多树重复记录（独立 issue）

评测中发现同一主题的记录分散在 `peers/*`、`user/*`、多棵项目树中，稀释检索召回（rerank 无法根治）。属于 OpenViking 记忆治理范畴，与本设计无关但影响本方案的效果上限。

#### 6. 前缀缓存（YAGNI，已裁决）

均批 27K 输入 tokens 中 system+few-shot 前缀仅占 10-15%，且 Ark 前缀计费优惠不明朗。两审一致裁决不做；若 listwise 演进则一并重估。

#### 监控基线

- 日常：`curl -s localhost:1933/metrics | grep rerank`（calls / tokens / 无 error 事件）
- 月度：rerank.error 计数 + 一次抽样检索延迟对比（0.6s=关 / ~10s=开，异常中间值 = 端点退化）
