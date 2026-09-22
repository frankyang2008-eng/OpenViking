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
        "Query: 如何重置登录密码\nDocument: OpenViking 采用 AGPL-3.0 许可证，由火山引擎开源。",
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

    def _score_one(self, query: str, document: str) -> Optional[tuple]:
        """Score one document; returns (score 0.0-1.0, usage dict), or None on failure.

        Runs on a worker thread: touches no shared state here — token usage is
        aggregated once on the calling thread in ``rerank_batch``
        (JevRerankClient precedent; TokenUsageTracker is not thread-safe).
        """
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
            usage_info = {
                "model_name": data.get("model") or self.model_name,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0)
                or self._estimate_tokens(query) + self._estimate_tokens(document),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "duration_seconds": duration,
            }
            match = _SCORE_RE.search(content)
            if not match:
                logger.warning("[LlmScoreRerank] Unparseable score content=%r", content)
                return None
            score = int(match.group())
            if not 0 <= score <= 100:
                logger.warning("[LlmScoreRerank] Score out of range: %s", score)
                return None
            return score / 100.0, usage_info
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
        usages: List[dict] = []
        failed = 0
        for fut in futures:
            try:
                result = fut.result()
            except Exception as e:
                logger.error("[LlmScoreRerank] Worker failed: %s", e)
                result = None
            if result is None:
                failed += 1
                scores.append(0.0)
            else:
                score, usage_info = result
                scores.append(score)
                usages.append(usage_info)

        if failed == len(documents):
            logger.error(
                "[LlmScoreRerank] All %s documents failed; falling back to vector scores",
                len(documents),
            )
            return None

        if usages:
            # One token-usage update per batch, on the calling thread.
            self.update_token_usage(
                model_name=usages[0]["model_name"],
                provider=self.provider,
                prompt_tokens=sum(u["prompt_tokens"] for u in usages),
                completion_tokens=sum(u["completion_tokens"] for u in usages),
                duration_seconds=sum(u["duration_seconds"] for u in usages),
            )

        logger.debug("[LlmScoreRerank] Reranked %s documents (failed=%s)", len(documents), failed)
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
