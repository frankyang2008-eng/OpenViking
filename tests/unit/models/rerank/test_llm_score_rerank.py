# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the llm_score (chat-model pointwise scoring) rerank client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from openviking.models.rerank import LlmScoreRerankClient, RerankClient
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

    def test_enabled_by_default(self):
        config = RerankConfig(provider="llm_score", api_key="k", api_base="https://x", model="m")
        assert config.enabled is True
        assert config.is_available() is True

    def test_enabled_false_disables_rerank(self):
        config = RerankConfig(
            provider="llm_score", api_key="k", api_base="https://x", model="m", enabled=False
        )
        assert config.is_available() is False

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
        # llm_score must be explicit: bare api_key+api_base auto-detect still resolves to openai
        config = RerankConfig(api_key="k", api_base="https://x/v1/reranks")
        assert config._effective_provider() == "openai"


class TestScoreParsing:
    """Adversarial matrix for the score parser (end-anchored extraction)."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("85", 0.85),
            ("85分。", 0.85),
            ("评分：85", 0.85),
            ("约85分", 0.85),
            ("100分满分给85", 0.85),  # end anchor takes the final score
            ("0.85", None),  # decimal — reject
            ("8.5分", None),
            ("1000", None),  # 4-digit garbage must not truncate to 100
            ("90-100", None),  # rubric range echo — reject
            ("1,000", None),  # thousands separator — reject
        ],
    )
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_parse_score_matrix(self, mock_client_class, content, expected):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        # second doc always scores 50; first doc carries the adversarial content
        mock_client.post.side_effect = [_mock_chat_response(content), _mock_chat_response("50")]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        # concurrency=1: deterministic side_effect consumption order
        assert client.rerank_batch("q", ["a", "b"]) == [0.0 if expected is None else expected, 0.5]


class TestRetry:
    """Retryable errors (429/5xx/transport) retry within the client; 4xx does not."""

    @staticmethod
    def _http_error(status: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://x/v3/chat/completions")
        resp = httpx.Response(status, request=req, headers={"Retry-After": "0"})
        return httpx.HTTPStatusError("err", request=req, response=resp)

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_429_then_success_retries(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [self._http_error(429), _mock_chat_response("85")]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) == [0.85]
        assert mock_client.post.call_count == 2

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_429_exhausts_retries_then_fails(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [self._http_error(429), self._http_error(429)]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) is None  # all failed -> None
        assert mock_client.post.call_count == 2  # 1 initial + 1 retry

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_client_error_not_retried(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [self._http_error(400), _mock_chat_response("85")]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) is None
        assert mock_client.post.call_count == 1  # 4xx: no retry

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_max_retries_zero_disables_retry(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [self._http_error(500), _mock_chat_response("85")]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            max_retries=0,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) is None
        assert mock_client.post.call_count == 1


class TestConcurrencyAlignment:
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_out_of_order_completion_preserves_alignment(self, mock_client_class):
        import time

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        def side_effect(url, json):
            last = json["messages"][-1]["content"]
            if "slow" in last:
                time.sleep(0.2)  # finishes last despite being submitted first
                return _mock_chat_response("10")
            return _mock_chat_response("90")

        mock_client.post.side_effect = side_effect
        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        scores = client.rerank_batch("q", ["slow doc", "fast doc one", "fast doc two"])
        # scores must align with submission order regardless of completion order
        assert scores == [0.10, 0.90, 0.90]


class TestFromConfigGuardrails:
    def test_doubao_model_without_thinking_disabled_warns(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="doubao-seed-2.0-mini-260215",
            thinking_disabled=False,
        )
        with patch("openviking.models.rerank.llm_score_rerank.logger") as mock_logger:
            client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        mock_logger.warning.assert_called_once()
        assert "thinking" in mock_logger.warning.call_args[0][0].lower()

    def test_doubao_model_with_thinking_disabled_no_warning(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="doubao-seed-2.0-mini-260215",
            thinking_disabled=True,
        )
        with patch("openviking.models.rerank.llm_score_rerank.logger") as mock_logger:
            client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        mock_logger.warning.assert_not_called()

    def test_retry_params_flow_from_config(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="m",
            max_retries=3,
            retry_backoff_seconds=0.1,
        )
        client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        assert client.max_retries == 3
        assert client.retry_backoff_seconds == 0.1
