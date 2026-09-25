# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Unit tests for the step-2 replay acceptance metrics (pure functions only)."""

from scripts.compare_internal_search_rerank import build_replay_query, usable_items


def _item(uri, score=0.9):
    return {"uri": uri, "score": score}


class TestUsableItems:
    def test_drops_summaries_and_keeps_order(self):
        items = [
            _item("viking://u/memories/preferences/a.abstract.md", 0.95),
            _item("viking://u/memories/entities/b.md", 0.8),
            _item("viking://u/memories/cases/c.overview.md", 0.7),
            _item("viking://u/memories/entities/d.md", 0.6),
        ]

        assert [i["uri"] for i in usable_items(items, 5)] == [
            "viking://u/memories/entities/b.md",
            "viking://u/memories/entities/d.md",
        ]

    def test_starvation_is_visible_not_padded(self):
        """A summary-flooded pool yields usable=1 at top=5 — the metric must show 1."""
        items = [_item("viking://u/memories/entities/b.md", 0.8)] + [
            _item(f"viking://u/memories/entities/s{i}.abstract.md", 0.9 - i * 0.01)
            for i in range(4)
        ]

        assert len(usable_items(items, 5)) == 1

    def test_cut_to_top(self):
        items = [_item(f"viking://u/memories/entities/{i}.md") for i in range(8)]

        assert len(usable_items(items, 5)) == 5


class TestBuildReplayQuery:
    def test_matches_production_shape(self):
        """Byte-identical to live prefetch: shared builder, whole-query normalize
        flattens the section separator into a single space (production does too)."""
        query = build_replay_query(
            [
                {"role": "user", "content": "hello world"},
                {"role": "assistant", "content": "hi there"},
            ]
        )

        assert query == "user: hello world assistant: hi there"

    def test_user_and_assistant_parts_capped_by_role(self):
        query = build_replay_query(
            [
                {"role": "user", "content": "x" * 1500},
                {"role": "assistant", "content": "y" * 800},
            ]
        )

        user_section, assistant_section = query.split(" assistant: ")
        assert user_section == "user: " + "x" * 997 + "..."  # user cap 1000
        assert assistant_section == "y" * 497 + "..."  # assistant cap 500

    def test_blank_messages_produce_empty_query(self):
        assert build_replay_query([{"role": "user", "content": ""}]) == ""
