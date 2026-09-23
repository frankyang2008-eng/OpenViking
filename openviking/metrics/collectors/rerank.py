# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""
Event collector: RerankCollector.

Tracks rerank call count, duration, and token usage:
- Calls counter by provider/model
- Duration histogram by provider/model
- Token counters by provider/model

This collector is fed by `RerankEventDataSource.record_call(...)` events emitted from rerank code
paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from openviking.metrics.core.base import MetricCollector

from .base import EventMetricCollector


def _to_int(value) -> int:
    """Best-effort int coercion for metric payloads; garbage counts as 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> float:
    """Best-effort float coercion for metric payloads; garbage counts as 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class RerankCollector(EventMetricCollector):
    """
    Translate rerank call events into per-provider/model counters and latency/token metrics.

    The collector mirrors the VLM per-call metric family so rerank can participate in the same
    dashboard patterns without introducing provider-specific series names.
    """

    DOMAIN: ClassVar[str] = "rerank"
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_calls_total
    # e.g.: openviking_rerank_calls_total
    # One increment per rerank BATCH, not per provider API call: llm_score fans a
    # single batch out to one API call per document inside it, so this series
    # counts batches while the token counters count document-level usage.
    CALLS_TOTAL: ClassVar[str] = MetricCollector.metric_name(DOMAIN, "calls", unit="total")
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_call_duration_seconds
    # e.g.: openviking_rerank_call_duration_seconds
    CALL_DURATION_SECONDS: ClassVar[str] = MetricCollector.metric_name(
        DOMAIN, "call_duration", unit="seconds"
    )
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_tokens_input_total
    # e.g.: openviking_rerank_tokens_input_total
    TOKENS_INPUT_TOTAL: ClassVar[str] = MetricCollector.metric_name(
        DOMAIN, "tokens_input", unit="total"
    )
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_tokens_output_total
    # e.g.: openviking_rerank_tokens_output_total
    TOKENS_OUTPUT_TOTAL: ClassVar[str] = MetricCollector.metric_name(
        DOMAIN, "tokens_output", unit="total"
    )
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_tokens_total
    # e.g.: openviking_rerank_tokens_total
    TOKENS_TOTAL: ClassVar[str] = MetricCollector.metric_name(DOMAIN, "tokens", unit="total")
    # rule: <METRICS_NAMESPACE>_<DOMAIN>_errors_total
    # e.g.: openviking_rerank_errors_total
    ERRORS_TOTAL: ClassVar[str] = MetricCollector.metric_name(DOMAIN, "errors", unit="total")

    SUPPORTED_EVENTS: ClassVar[frozenset[str]] = frozenset({"rerank.call", "rerank.error"})

    def collect(self, registry=None) -> None:
        """Implement the unified collector interface as a no-op for this event-driven collector."""
        return None

    def receive_hook(self, event_name: str, payload: dict, registry) -> None:
        """Translate one supported rerank event into the corresponding metric writes."""
        if event_name == "rerank.error":
            self.record_error(
                registry,
                error_code=str(payload.get("error_code") or "unknown"),
                scope=str(payload.get("scope") or "batch"),
                account_id=(
                    None if payload.get("account_id") is None else str(payload.get("account_id"))
                ),
            )
            return
        self.record_call(
            registry,
            provider=str(payload["provider"]),
            model_name=str(payload["model_name"]),
            duration_seconds=_to_float(payload.get("duration_seconds")),
            prompt_tokens=_to_int(payload.get("prompt_tokens")),
            completion_tokens=_to_int(payload.get("completion_tokens")),
            account_id=(
                None if payload.get("account_id") is None else str(payload.get("account_id"))
            ),
        )

    def record_call(
        self,
        registry,
        *,
        provider: str,
        model_name: str,
        duration_seconds: float,
        prompt_tokens: int,
        completion_tokens: int,
        account_id: str | None = None,
    ) -> None:
        """Record one rerank batch: calls/tokens counters plus a latency histogram sample.

        The calls counter increments once per batch. For llm_score one batch fans
        out to one provider API call per document, so ``calls`` is a batch count,
        while the token counters accumulate document-level usage.
        """
        labels = {"provider": str(provider), "model_name": str(model_name)}
        prompt_tokens = _to_int(prompt_tokens)
        completion_tokens = _to_int(completion_tokens)
        registry.inc_counter(
            self.CALLS_TOTAL,
            labels=labels,
            label_names=("provider", "model_name"),
            account_id=account_id,
        )
        registry.observe_histogram(
            self.CALL_DURATION_SECONDS,
            _to_float(duration_seconds),
            labels=labels,
            label_names=("provider", "model_name"),
            account_id=account_id,
        )
        if prompt_tokens > 0:
            registry.inc_counter(
                self.TOKENS_INPUT_TOTAL,
                labels=labels,
                label_names=("provider", "model_name"),
                amount=prompt_tokens,
                account_id=account_id,
            )
        if completion_tokens > 0:
            registry.inc_counter(
                self.TOKENS_OUTPUT_TOTAL,
                labels=labels,
                label_names=("provider", "model_name"),
                amount=completion_tokens,
                account_id=account_id,
            )
        total_tokens = prompt_tokens + completion_tokens
        if total_tokens > 0:
            registry.inc_counter(
                self.TOKENS_TOTAL,
                labels=labels,
                label_names=("provider", "model_name"),
                amount=total_tokens,
                account_id=account_id,
            )

    def record_error(
        self,
        registry,
        *,
        error_code: str,
        scope: str = "batch",
        account_id: str | None = None,
    ) -> None:
        """Record one rerank error, labeled by cause and scope.

        ``error_code`` names the cause (``rate_limited`` / ``server_error`` /
        ``client_error`` / ``timeout`` / ``transport`` / ``parse`` / ``exception`` /
        ``mixed``) and ``scope`` how much of the batch it hit (``all`` / ``partial``) -
        the two things the old ``all_failed`` / ``score_failed`` codes conflated.
        Kept separate from CALLS_TOTAL on purpose: a batch error is not a provider call,
        and mixing the two would make the call counter incomparable with the token
        counters.
        """
        registry.inc_counter(
            self.ERRORS_TOTAL,
            labels={
                "error_code": str(error_code or "unknown"),
                "scope": str(scope or "batch"),
            },
            label_names=("error_code", "scope"),
            account_id=account_id,
        )
