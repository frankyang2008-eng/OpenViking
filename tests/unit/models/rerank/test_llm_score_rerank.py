# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the llm_score (chat-model pointwise scoring) rerank provider config."""

import pytest
from pydantic import ValidationError

from openviking_cli.utils.config.rerank_config import RerankConfig


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
