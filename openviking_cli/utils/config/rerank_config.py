# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from typing import Dict, Optional

from pydantic import BaseModel, Field, model_validator


class RerankConfig(BaseModel):
    """Configuration for rerank API. Supports VikingDB, Cohere, OpenAI-compatible, LiteLLM, Jev (TypeSafe), and llm_score (chat-model pointwise scoring) providers."""

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch for rerank. False withholds the rerank client, which also "
            "degrades retrieval to the flat QUICK strategy (no candidate reranking) — "
            "it is not a scoring-only switch. To keep the global-recall strategy while "
            "skipping LLM scores, pass rerank=False per call instead. A "
            "disabled-but-configured block still supplies its threshold to the vector "
            "path; provider credentials may stay configured."
        ),
    )

    provider: Optional[str] = Field(
        default=None,
        description=(
            "Rerank provider: 'vikingdb', 'cohere', 'openai', 'litellm', 'jev', or 'llm_score'. "
            "Auto-detected from config if omitted, except 'llm_score' which must be set "
            "explicitly (api_key+api_base alone auto-detects as 'openai')."
        ),
    )

    # VikingDB fields
    ak: Optional[str] = Field(default=None, description="VikingDB Access Key")
    sk: Optional[str] = Field(default=None, description="VikingDB Secret Key")
    host: str = Field(
        default="api-vikingdb.vikingdb.cn-beijing.volces.com", description="VikingDB API host"
    )
    model_name: str = Field(default="doubao-seed-rerank", description="Rerank model name")
    model_version: str = Field(default="251028", description="Rerank model version")

    # Shared provider fields
    api_key: Optional[str] = Field(
        default=None,
        description="API key for Cohere, OpenAI-compatible, Jev, or llm_score providers",
    )
    api_base: Optional[str] = Field(default=None, description="Custom endpoint URL")
    model: Optional[str] = Field(
        default=None,
        description="Model name for OpenAI-compatible, LiteLLM, Jev, or llm_score providers",
    )
    mode: Optional[str] = Field(
        default="noul",
        description="Jev rerank mode: 'noul' or 'choice'",
    )

    extra_headers: Optional[Dict[str, str]] = Field(
        default=None, description="Extra HTTP headers for OpenAI-compatible providers"
    )

    timeout: float = Field(
        default=30.0,
        description=(
            "HTTP request timeout in seconds for rerank calls. Increase for local "
            "LLM servers with model cold-start latency. For llm_score the effective "
            "read timeout is clamped to batch_timeout + 2 when a batch_timeout is set, "
            "so orphaned batches die near the batch cut instead of at the full timeout."
        ),
    )

    threshold: float = Field(
        default=0.1, description="Relevance threshold (score > threshold is relevant)"
    )

    batch_timeout: float = Field(
        default=8.0,
        ge=0,
        description=(
            "Wall-clock budget for a single rerank batch in seconds; 0 disables the cut. "
            "On timeout the batch falls back to vector scores. Keep it below the provider "
            "HTTP timeout so the batch cut wins over the transport. With 0 the only cut "
            "is the transport itself: a hung provider can occupy a rerank worker for up "
            "to timeout x (max_retries + 1) + backoff seconds."
        ),
    )

    total_budget: float = Field(
        default=20.0,
        ge=0,
        description=(
            "Wall-clock budget for all rerank batches of one search in seconds; 0 disables "
            "the cut. Once exhausted, remaining batches skip rerank and keep vector scores "
            "(ordering quality only, no recall loss)."
        ),
    )

    max_input_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum estimated raw-text tokens for each query-document pair sent to "
            "the rerank provider; 0 disables truncation"
        ),
    )

    log_payloads: bool = Field(
        default=False,
        description=(
            "Log complete rerank request and response payloads. Disabled by default "
            "because payloads may contain sensitive query and document content."
        ),
    )

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
    max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description=(
            "Retry attempts for transient rerank errors (429/5xx/transport); "
            "4xx fails fast without retry. 0 disables retry."
        ),
    )
    retry_backoff_seconds: float = Field(
        default=0.5,
        ge=0,
        description=(
            "Base backoff seconds for rerank retries; doubles per attempt, "
            "Retry-After response header takes precedence for 429"
        ),
    )

    def _effective_provider(self) -> Optional[str]:
        """Auto-detect provider from config fields when not explicitly set."""
        if self.provider:
            return self.provider.lower()
        if self.api_base and "typesafe" in self.api_base:
            return "jev"
        if self.api_key and self.api_base:
            return "openai"
        if self.api_key:
            return "cohere"
        if self.ak and self.sk:
            return "vikingdb"
        return None

    @model_validator(mode="after")
    def validate_provider_fields(self) -> "RerankConfig":
        if 0 < self.max_input_tokens < 128:
            raise ValueError("Rerank max_input_tokens must be 0 or at least 128")

        provider = self._effective_provider()
        if provider == "jev" and self.mode is not None:
            self.mode = self.mode.strip().lower()
            if self.mode not in ("noul", "choice"):
                raise ValueError("Jev rerank mode must be one of ['noul', 'choice']")

        if provider and provider not in [
            "vikingdb",
            "cohere",
            "openai",
            "litellm",
            "jev",
            "llm_score",
        ]:
            raise ValueError(
                "Rerank provider must be one of "
                "['vikingdb', 'cohere', 'openai', 'litellm', 'jev', 'llm_score'], got "
                f"'{provider}'"
            )
        if provider == "openai":
            if not self.api_key or not self.api_base:
                raise ValueError(
                    "OpenAI-compatible rerank provider requires 'api_key' and 'api_base'"
                )
        if provider == "litellm":
            if not self.model:
                raise ValueError("LiteLLM rerank provider requires 'model'")
        if provider in ("cohere", "jev"):
            if not self.api_key:
                label = "Cohere" if provider == "cohere" else "Jev"
                raise ValueError(f"{label} rerank provider requires 'api_key'")
        if provider == "vikingdb":
            if not self.ak or not self.sk:
                raise ValueError("VikingDB rerank provider requires 'ak' and 'sk'")
        if provider == "llm_score":
            if not self.api_key or not self.api_base or not self.model:
                raise ValueError(
                    "llm_score rerank provider requires 'api_key', 'api_base', and 'model'"
                )
        return self

    def is_available(self) -> bool:
        """Check if rerank is configured and enabled."""
        if not self.enabled:
            return False
        p = self._effective_provider()
        if p in ("cohere", "jev"):
            return self.api_key is not None
        if p == "openai":
            return self.api_key is not None and self.api_base is not None
        if p == "litellm":
            return self.model is not None
        if p == "vikingdb":
            return self.ak is not None and self.sk is not None
        if p == "llm_score":
            return self.api_key is not None and self.api_base is not None and self.model is not None
        return False
