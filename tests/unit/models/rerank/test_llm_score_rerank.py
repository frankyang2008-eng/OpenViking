# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the llm_score (chat-model pointwise scoring) rerank client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from openviking.models.rerank import LlmScoreRerankClient
from openviking_cli.utils.config.rerank_config import RerankConfig


def _mock_chat_response(content: str):
    response = MagicMock()
    response.json.return_value = {
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
    response.raise_for_status = MagicMock()
    response.status_code = 200
    response.text = ""
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

    def test_concurrency_explicit_value_accepted(self):
        config = RerankConfig(
            provider="llm_score", api_key="k", api_base="https://x", model="m", concurrency=5
        )
        assert config.concurrency == 5

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
            concurrency=1,  # concurrency=1: deterministic side_effect consumption order
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

        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,  # concurrency=1: deterministic side_effect consumption order
        )
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
        # out-of-range score on one doc -> 0.0; the other doc unaffected
        mock_client.post.side_effect = [_mock_chat_response("900"), _mock_chat_response("50")]

        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,  # concurrency=1: deterministic side_effect consumption order
        )
        assert client.rerank_batch("q", ["a", "b"]) == [0.0, 0.5]

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_empty_documents_returns_empty_list(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")

        assert client.rerank_batch("q", []) == []
        mock_client.post.assert_not_called()  # no HTTP call for an empty batch

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_api_url_already_suffixed_not_duplicated(self, mock_client_class):
        LlmScoreRerankClient(api_key="k", api_base="https://x/v3/chat/completions", model_name="m")
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3/chat/completions", model_name="m"
        )

        assert client.api_url == "https://x/v3/chat/completions"
