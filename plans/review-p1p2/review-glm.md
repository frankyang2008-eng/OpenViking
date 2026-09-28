# Review: P1 (WM UPDATE content normalization) + P2 (cross-tree entity merge deferral)

Reviewer: glm-5.3 (independent re-derivation, thinking high) · Date: 2026-09-28

## Verdicts
1. **P1: CORRECT AND SAFE TO KEEP.** Root cause right, right layer (merge chokepoint), complete for the UPDATE path, zero regression risk; 3/4 new tests are genuine regression tests. One residual same-class gap A-1 (APPEND content fallback, line ~741) — fixed after review.
2. **P2: AGREE, NO ACTION NOW.** Peer inclusion is by design; peer tree is actively auto-written; observed retrieval harm nil. Deferral with monitoring trigger is correct. Wording "two-way sync" imprecise (one-way extraction appends); 6-sample basis thin — both hedged by the trigger.

---

## Part A — P1

### Root cause / fix layer — VERIFIED
- Crash: guards + final apply read UPDATE content as `(op.get("content") or "").strip()` (lines 526, 741, 781, 854, 897, 935, 1053); array/object content has no `.strip()`.
- Schema declares content string, but session.py:3076-3117 does zero schema validation; schema-loose backend passes list through.
- merge_wm_sections is the single chokepoint for direct parse, regex salvage, and direct callers → correct fix layer. No other content consumer exists.

### Coverage
UPDATE sites 526/781/854/897/935/1053 all covered after normalization. APPEND fallback 741 was NOT covered → finding A-1 (now fixed).

### Findings
- **[A-1 · MEDIUM, FIXED]** APPEND op with non-string `content` and empty items + oversized old Key Facts crashes at the `raw = (op.get("content") or "").strip()` fallback. Fix: use `_wm_content_to_text`.
- **[A-2 · LOW, not changed]** `(op.get("op") or "").upper()` assumes string-or-falsy (pre-existing pattern, outside scope).
- **[A-3 · LOW, noted]** `test_update_content_none_is_empty` passes without the fix (robustness pin, not regression test); the other 3 new tests are true regression tests.
- **[A-4 · LOW, acceptable]** No test for a guarded section that ACCEPTS the UPDATE; entry-level normalization makes per-guard coverage largely redundant.

### `_wm_content_to_text` correctness
Nested/mixed lists, dict, None, unicode, NaN all correct; no infinite recursion (json.loads cannot produce cycles); no shared-op mutation (shallow copy + content replaced); zero regression risk to string behavior. Minimal fix — no simpler alternative.

---

## Part B — P2

- Peer tree actively auto-written via extraction routing (`memory_isolation_handler.py`, `streaming_memory_updater.py`); writes observed during this task.
- Peer inclusion by design: `retrieval_targets.py` default_target_directories + `gather.py` memory_target_roots; cross-peer access denied (`is_hidden_by_actor_peer_view`).
- 6 prefetches / 30 slots / 1 peer result / 0 same-entity dupes → current ranker collapses cross-tree copies; existing dedupe is URI-only so entity dedupe would be new work.
- **[B-1 · LOW]** "two-way sync" overstated — it is one-way extraction appends; do-not-merge still stands on the design argument.
- **[B-2 · LOW]** 6-sample/one-day/one-user basis thin; acceptable for "no action now," hedged by the monitoring trigger.
- Correct future fix if harm appears: result-level entity dedupe by basename/title at the candidate-merge layer (`gather.py` dedupe_keep_best). Target scoping to exclude peers would cut recall — correctly not proposed.
