# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the llm_score (chat-model pointwise scoring) rerank client."""

import math
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from openviking.models.rerank import LlmScoreRerankClient, RerankClient
from openviking.models.rerank.llm_score_rerank import _parse_score
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


class TestRerankBudgetConfig:
    """Batch and per-search rerank budgets (0 disables each cut)."""

    def test_budget_defaults(self):
        config = RerankConfig(ak="ak", sk="sk")
        assert config.batch_timeout == 8.0
        assert config.total_budget == 20.0

    def test_zero_disables_both_cuts(self):
        config = RerankConfig(ak="ak", sk="sk", batch_timeout=0, total_budget=0)
        assert config.batch_timeout == 0.0
        assert config.total_budget == 0.0

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            RerankConfig(ak="ak", sk="sk", batch_timeout=-1)
        with pytest.raises(ValidationError):
            RerankConfig(ak="ak", sk="sk", total_budget=-1)


@patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
def test_close_waits_for_in_flight_batch(mock_client_class):
    """close() must not cut a running request into a fake 0.0 score."""
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    in_flight = threading.Event()
    release = threading.Event()

    def slow_post(*args, **kwargs):
        in_flight.set()
        release.wait(5.0)
        return _mock_chat_response("90")

    mock_client.post.side_effect = slow_post
    client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
    results: list = []

    worker = threading.Thread(target=lambda: results.append(client.rerank_batch("q", ["a"])))
    worker.start()
    assert in_flight.wait(5.0)

    closer = threading.Thread(target=client.close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()  # close() waits instead of tearing the request down

    release.set()
    worker.join(5.0)
    closer.join(5.0)

    assert results == [[0.9]]  # the in-flight document kept its real score
    mock_client.close.assert_called_once()


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
    def test_per_doc_failure_yields_nan_not_zero(self, mock_client_class):
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

        # A failed document reports "no score" (NaN), not 0.0: the caller maps a
        # non-finite score back to that document's vector score, so a transient
        # failure cannot sink a relevant document below irrelevant ones.
        assert scores is not None
        assert scores[0] == 0.95
        assert math.isnan(scores[1])
        assert scores[2] == 0.4

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_all_failures_return_none_and_report_cause(
        self, mock_client_class, mock_record_error
    ):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = httpx.HTTPError("down")

        client = LlmScoreRerankClient(api_key="k", api_base="https://x/v3", model_name="m")
        # caller falls back to vector scores
        assert client.rerank_batch("q", ["a", "b"]) is None
        # ...and a provider-wide outage must still be visible in rerank.error, with the
        # cause and the scope spelled out instead of one opaque "all_failed".
        assert mock_record_error.call_args.kwargs["error_code"] == "exception"
        assert mock_record_error.call_args.kwargs["scope"] == "all"

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_partial_failure_reports_cause_and_scope(self, mock_client_class, mock_record_error):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            _mock_chat_response("95"),
            httpx.HTTPError("boom"),
        ]

        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        scores = client.rerank_batch("q", ["a", "b"])
        assert scores is not None
        assert scores[0] == 0.95
        assert math.isnan(scores[1])
        assert mock_record_error.call_args.kwargs["error_code"] == "exception"
        assert mock_record_error.call_args.kwargs["scope"] == "partial"

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_score_out_of_range_is_failure(self, mock_client_class, mock_record_error):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        # out-of-range score on one doc -> NaN (no score); the other doc unaffected
        mock_client.post.side_effect = [_mock_chat_response("900"), _mock_chat_response("50")]

        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,  # concurrency=1: deterministic side_effect consumption order
        )
        scores = client.rerank_batch("q", ["a", "b"])
        assert scores is not None
        assert math.isnan(scores[0])
        assert scores[1] == 0.5
        assert mock_record_error.call_args.kwargs["error_code"] == "parse"
        assert mock_record_error.call_args.kwargs["scope"] == "partial"

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_mixed_causes_collapse_to_mixed(self, mock_client_class, mock_record_error):
        """A 429 on one document and an unparseable answer on another is not one cause."""

        def _status_error(status: int) -> httpx.HTTPStatusError:
            request = httpx.Request("POST", "https://x/v3/chat/completions")
            response = httpx.Response(status, request=request, headers={"Retry-After": "0"})
            return httpx.HTTPStatusError("err", request=request, response=response)

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            _status_error(429),
            _status_error(429),
            _mock_chat_response("not a number"),
        ]

        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a", "b"]) is None
        assert mock_record_error.call_args.kwargs["error_code"] == "mixed"
        assert mock_record_error.call_args.kwargs["scope"] == "all"

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_http_status_failures_map_to_bounded_causes(
        self, mock_client_class, mock_record_error
    ):
        """Quota, provider outage, and a rejected request must not share one series."""

        def _status_error(status: int) -> httpx.HTTPStatusError:
            request = httpx.Request("POST", "https://x/v3/chat/completions")
            response = httpx.Response(status, request=request, headers={"Retry-After": "0"})
            return httpx.HTTPStatusError("err", request=request, response=response)

        for status, expected in (
            (429, "rate_limited"),
            (503, "server_error"),
            (401, "client_error"),
        ):
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_client.post.side_effect = [_status_error(status)] * 2
            client = LlmScoreRerankClient(
                api_key="k",
                api_base="https://x/v3",
                model_name="m",
                concurrency=1,
                retry_backoff_seconds=0,
            )

            assert client.rerank_batch("q", ["a"]) is None
            assert mock_record_error.call_args.kwargs["error_code"] == expected
            assert mock_record_error.call_args.kwargs["scope"] == "all"

    @patch("openviking.metrics.datasources.RerankEventDataSource.record_error")
    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_timeout_and_transport_failures_are_distinguished(
        self, mock_client_class, mock_record_error
    ):
        """A slow endpoint and a refused connection need different operator actions."""
        for error, expected in (
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("refused"), "transport"),
        ):
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_client.post.side_effect = [error, error]
            client = LlmScoreRerankClient(
                api_key="k",
                api_base="https://x/v3",
                model_name="m",
                concurrency=1,
                retry_backoff_seconds=0,
            )

            assert client.rerank_batch("q", ["a"]) is None
            assert mock_record_error.call_args.kwargs["error_code"] == expected
            assert mock_record_error.call_args.kwargs["scope"] == "all"

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
    """Adversarial matrix for the score parser (end-anchored, digit-bounded).

    Asserted directly on ``_parse_score``: routing the matrix through
    ``rerank_batch`` mapped a parse failure to the same 0.0 that a genuine "0"
    score produces, so the old ``("1000", None)`` case passed whatever the
    parser returned.
    """

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("85", 85),
            ("85分。", 85),
            ("85.", 85),  # trailing ASCII period is a valid suffix
            ("85分.", 85),
            ("85．", 85),
            ("85。", 85),
            ("评分：85", 85),
            ("约85分", 85),
            ("总分85", 85),
            ("100分满分给85", 85),  # end anchor takes the final score
            ("100", 100),
            ("0", 0),
            ("0.85", None),  # decimal — reject
            ("8.5分", None),
            ("85.5", None),
            ("85．5", None),  # full-width decimal separator
            ("85。5", None),
            ("85,5", None),
            ("85，5", None),
            ("1000", None),  # 4-digit garbage must not truncate to 100 or 0
            ("10000", None),
            ("1085", None),  # embedded digits must not become a high score
            ("总分1085", None),
            ("1234", None),
            ("90-100", None),  # rubric range echo — reject
            ("1,000", None),  # thousands separator — reject
            ("85—90", None),  # em-dash range — must not take the upper bound
            ("85～90", None),  # full-width tilde range
            ("85〜90", None),  # CJK wave dash range
            ("85~90", None),  # ASCII tilde range
            ("1 0 0", None),  # gap digits must not fake a trailing 0
            ("10 0", None),  # gap digits (2-digit head)
            ("相关性：85", 85),  # full-width colon label is tolerated
            ("Score: 85", 85),  # ASCII colon label is tolerated
            ('{"score":85}', None),  # JSON tail is not a score
            ("８５", 85),  # full-width digits (\d is unicode-aware)
            ("８５分", 85),  # full-width digits with suffix
            ("85\u3000", 85),  # ideographic space suffix (\s is unicode-aware)
            ("", None),
            ("满分", None),
            ("None", None),  # str(None) from a null content field
        ],
    )
    def test_parse_score_matrix(self, content, expected):
        assert _parse_score(content) == expected

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_trailing_period_score_flows_through_batch(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [_mock_chat_response("85."), _mock_chat_response("50")]

        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        # concurrency=1: deterministic side_effect consumption order
        assert client.rerank_batch("q", ["a", "b"]) == [0.85, 0.5]


class TestFailureUsage:
    """Failed documents still consumed real tokens — usage must not vanish."""

    @staticmethod
    def _chat_response(content: str, prompt_tokens: int) -> MagicMock:
        response = _mock_chat_response(content)
        response.json.return_value["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 2,
            "total_tokens": prompt_tokens + 2,
        }
        return response

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_parse_failure_usage_is_reported(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._chat_response("满分", 120),  # unparseable -> NaN, tokens still burned
            self._chat_response("85", 30),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        with patch.object(client, "update_token_usage") as mock_update:
            scores = client.rerank_batch("q", ["bad", "good"])

        assert scores is not None and len(scores) == 2
        assert scores[1] == 0.85
        assert math.isnan(scores[0])
        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["prompt_tokens"] == 150  # 120 + 30

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_all_failed_batch_reports_usage(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._chat_response("满分", 120),
            self._chat_response("满分", 120),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        with patch.object(client, "update_token_usage") as mock_update:
            assert client.rerank_batch("q", ["a", "b"]) is None

        mock_update.assert_called_once()
        assert mock_update.call_args.kwargs["prompt_tokens"] == 240

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_partial_batch_counts_fallback_docs(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._chat_response("满分", 120),
            self._chat_response("85", 30),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        telemetry = MagicMock()
        with (
            patch.object(client, "update_token_usage"),
            patch(
                "openviking.models.rerank.llm_score_rerank.get_current_telemetry",
                return_value=telemetry,
            ),
        ):
            client.rerank_batch("q", ["bad", "good"])

        telemetry.count.assert_called_once_with("rerank.partial_fallback_docs", 1)


class TestRetry:
    """Retryable errors (429/5xx/transport) retry within the client; 4xx does not."""

    @staticmethod
    def _http_error(status: int, retry_after: str = "0") -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://x/v3/chat/completions")
        resp = httpx.Response(status, request=req, headers={"Retry-After": retry_after})
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
    def test_retry_backoff_is_jittered(self, mock_client_class):
        """Workers that hit the provider limit together must not retry together."""
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", retry_backoff_seconds=1.0
        )
        delays: list = []
        with patch(
            "openviking.models.rerank.llm_score_rerank.time.sleep", side_effect=delays.append
        ):
            for _ in range(20):
                client._sleep_before_retry(0, None)

        assert all(0.5 <= delay <= 1.0 for delay in delays)  # equal jitter band
        assert len(set(delays)) > 1  # ...and actually spread out, not lockstep

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_retry_after_is_a_floor_not_jittered_away(self, mock_client_class):
        """A server-provided Retry-After must never be undercut by jitter."""
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", retry_backoff_seconds=0.1
        )
        response = MagicMock()
        response.headers = {"retry-after": "2"}
        delays: list = []
        with patch(
            "openviking.models.rerank.llm_score_rerank.time.sleep", side_effect=delays.append
        ):
            client._sleep_before_retry(0, response)

        assert delays == [2.0]

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_retry_after_float_header_is_respected(self, mock_client_class):
        """Float Retry-After headers (e.g. \"1.5\") parse and set the floor."""
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", retry_backoff_seconds=0.1
        )
        response = MagicMock()
        response.headers = {"retry-after": "1.5"}
        delays: list = []
        with patch(
            "openviking.models.rerank.llm_score_rerank.time.sleep", side_effect=delays.append
        ):
            client._sleep_before_retry(0, response)

        assert delays == [1.5]

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_retry_after_over_batch_timeout_skips_retry(self, mock_client_class):
        """A Retry-After the batch cut can never wait out is a terminal failure."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [self._http_error(429, retry_after="60")]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            max_retries=1,
            retry_backoff_seconds=0,
            batch_timeout=8.0,
        )
        assert client.rerank_batch("q", ["a"]) is None
        assert mock_client.post.call_count == 1  # retrying could never return in time

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_retry_after_under_batch_timeout_still_retries(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._http_error(429, retry_after="1"),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            max_retries=1,
            retry_backoff_seconds=0,
            batch_timeout=8.0,
        )
        assert client.rerank_batch("q", ["a"]) == [0.85]
        assert mock_client.post.call_count == 2

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

    def test_read_timeout_clamped_to_batch_timeout(self):
        """Orphan batches must die near the batch cut, not at the full HTTP timeout."""
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="m",
            timeout=30.0,
            batch_timeout=8.0,
        )
        client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        assert client._client.timeout.read == 10.0  # batch_timeout + 2 slack

    def test_read_timeout_untouched_when_batch_timeout_disabled(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="m",
            timeout=30.0,
            batch_timeout=0.0,
        )
        client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        assert client._client.timeout.read == 30.0

    def test_read_timeout_kept_when_already_below_clamp(self):
        config = RerankConfig(
            provider="llm_score",
            api_key="k",
            api_base="https://x",
            model="m",
            timeout=5.0,
            batch_timeout=8.0,
        )
        client = LlmScoreRerankClient.from_config(config)
        assert client is not None
        assert client._client.timeout.read == 5.0


class TestMalformedResponses:
    """A1: provider responses that violate the happy-path shape must degrade safely."""

    @staticmethod
    def _response(payload) -> MagicMock:
        response = MagicMock()
        response.json.return_value = payload
        response.raise_for_status = MagicMock()
        return response

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_invalid_json_all_failed_returns_none(self, mock_client_class):
        """Every doc failing the JSON shape -> whole-batch None channel."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.return_value = MagicMock()
        mock_client.post.return_value.raise_for_status = MagicMock()
        mock_client.post.return_value.json.side_effect = ValueError("bad json")
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        assert client.rerank_batch("q", ["a", "b"]) is None

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_null_content_parses_as_failure(self, mock_client_class):
        """Partial malformed batch: the failed doc gets NaN, the valid one scores."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._response(
                {"choices": [{"message": {"role": "assistant", "content": None}}], "usage": {"prompt_tokens": 10}}
            ),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        scores = client.rerank_batch("q", ["bad", "good"])
        assert scores is not None and math.isnan(scores[0]) and scores[1] == 0.85

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_missing_choices_degrades_to_nan(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._response({"usage": {"prompt_tokens": 10}}),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        scores = client.rerank_batch("q", ["bad", "good"])
        assert scores is not None and math.isnan(scores[0]) and scores[1] == 0.85

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_truncated_empty_content_degrades_to_nan(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self._response(
                {"choices": [{"message": {"role": "assistant", "content": ""}}], "usage": {"prompt_tokens": 10}}
            ),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        scores = client.rerank_batch("q", ["bad", "good"])
        assert scores is not None and math.isnan(scores[0]) and scores[1] == 0.85

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_missing_usage_falls_back_to_estimation(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.return_value = self._response(
            {"choices": [{"message": {"role": "assistant", "content": "85"}}]}
        )
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=1
        )
        with patch.object(client, "update_token_usage") as mock_update:
            scores = client.rerank_batch("q", ["a"])

        assert scores == [0.85]
        mock_update.assert_called_once()
        kwargs = mock_update.call_args.kwargs
        assert kwargs["prompt_tokens"] > 0  # estimated, not zero

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_family_retries_consistently(self, mock_client_class, status):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.post.side_effect = [
            self.__class__._http_error(status),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) == [0.85]
        assert mock_client.post.call_count == 2

    @staticmethod
    def _http_error(status: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://x/v3/chat/completions")
        resp = httpx.Response(status, request=req, headers={"Retry-After": "0"})
        return httpx.HTTPStatusError("err", request=req, response=resp)

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_retry_after_http_date_falls_back_to_jitter(self, mock_client_class):
        """HTTP-date Retry-After is not numeric: parse yields 0, retry still happens."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        req = httpx.Request("POST", "https://x/v3/chat/completions")
        resp = httpx.Response(
            429, request=req, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
        mock_client.post.side_effect = [
            httpx.HTTPStatusError("err", request=req, response=resp),
            _mock_chat_response("85"),
        ]
        client = LlmScoreRerankClient(
            api_key="k",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            retry_backoff_seconds=0,
        )
        assert client.rerank_batch("q", ["a"]) == [0.85]
        assert mock_client.post.call_count == 2

    def test_log_payloads_never_contains_api_key(self, caplog):
        client = LlmScoreRerankClient(
            api_key="super-secret-key",
            api_base="https://x/v3",
            model_name="m",
            concurrency=1,
            log_payloads=True,
        )
        with caplog.at_level("WARNING", logger="openviking.models.rerank.llm_score_rerank"):
            client._score_one("q", "d")
        joined = "\n".join(str(r.getMessage()) for r in caplog.records)
        assert "super-secret-key" not in joined
        client.close()


class TestLifecycleRace:
    """B3: shared-client races — concurrent batches and close-during-flight."""

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_concurrent_batches_share_client_safely(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        def slow_post(url, json):
            time.sleep(0.01)
            return _mock_chat_response("85")

        mock_client.post.side_effect = slow_post
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=8
        )
        usage_lock = threading.Lock()
        usage_totals = {"prompt": 0, "calls": 0}

        def record_usage(**kwargs):
            with usage_lock:
                usage_totals["prompt"] += kwargs["prompt_tokens"]
                usage_totals["calls"] += 1

        results: list = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    client.rerank_batch("q", ["a", "b", "c", "d"])
                )
            )
            for _ in range(10)
        ]
        with patch.object(client, "update_token_usage", side_effect=record_usage):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == 10
        assert all(r == [0.85] * 4 for r in results)  # no lost scores, no NaN
        assert usage_totals["calls"] == 10  # one usage update per batch, none lost
        assert usage_totals["prompt"] == 10 * 4 * 120
        client.close()

    @patch("openviking.models.rerank.llm_score_rerank.httpx.Client")
    def test_close_during_in_flight_batch_completes_it(self, mock_client_class):
        """close() drains in-flight docs before closing the HTTP pool under them."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        def slow_post(url, json):
            time.sleep(0.3)
            return _mock_chat_response("85")

        mock_client.post.side_effect = slow_post
        client = LlmScoreRerankClient(
            api_key="k", api_base="https://x/v3", model_name="m", concurrency=2
        )
        results: list = []
        worker = threading.Thread(
            target=lambda: results.append(client.rerank_batch("q", ["a", "b"]))
        )
        worker.start()
        time.sleep(0.1)  # batch is now in flight
        client.close()  # must wait for the batch, not close httpx under it
        worker.join(timeout=10)

        assert results == [[0.85, 0.85]]  # in-flight batch survived the close
