# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Step-2 offline replay: one real internal search query, rerank on vs off.

Answers the only question the rerank decision still has: with the same corpus and
the same query, how much does the internal (prefetch / merge / experience) search
result move when the LLM rerank is skipped? Nothing here changes production
behavior — `rerank=False` is a capability flag whose default is today's behavior.

The workspace has an exclusive OS lock, so stop the server first:

    openviking-server  # Ctrl-C, or kill $(pgrep -f openviking-server)
    python scripts/compare_internal_search_rerank.py --messages-json /tmp/msgs.json
    openviking-server --with-bot

Query construction delegates to the production prefetch query builder
(build_prefetch_search_query_from_parts) so an offline run sees byte-identical
query text to what live prefetch searched.

Output: a table on stdout, plus JSON (--json, default under plans/rerank-mini-verification/).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TARGETS = [
    "viking://user/trae_dever/memories/preferences",
    "viking://user/trae_dever/memories/entities",
    "viking://user/trae_dever/memories/cases",
]


def build_replay_query(messages: List[Dict[str, Any]]) -> str:
    """Byte-identical to live prefetch: delegates to the production query builder."""
    from openviking.session.memory.session_extract_context_provider import (
        build_prefetch_search_query_from_parts,
    )

    entries = []
    for msg in messages:
        role = str(msg.get("role") or "")
        speaker = msg.get("peer_id") or role
        text = " ".join(str(msg.get("content") or "").split())
        entries.append((role, speaker, [text] if text else []))
    return build_prefetch_search_query_from_parts(entries)


def usable_items(items: List[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """What the consumer would actually read: non-summary files, ordered, cut to top-n.

    Acceptance metric for the replay. Raw top-n Jaccard conflates two different
    failures: a side whose candidate pool is flooded with L0/L1 summaries delivers
    fewer than ``top`` usable files (starvation), which is a collection problem,
    not a rerank-ordering signal. Compare usable sets, and report per-side usable
    counts so starvation stays visible instead of deflating the overlap.
    """
    out: List[Dict[str, Any]] = []
    for item in items:
        uri = str(item.get("uri") or "")
        if not uri or uri.endswith(".abstract.md") or uri.endswith(".overview.md"):
            continue
        out.append({"uri": uri, "score": item.get("score")})
        if len(out) >= top:
            break
    return out


async def run_side(viking_fs, query: str, targets: List[str], limit: int, rerank: bool, ctx):
    result = await viking_fs.search(query, target_uri=targets, limit=limit, ctx=ctx, rerank=rerank)
    raw = result.to_dict()
    return {
        "rerank": rerank,
        "raw": raw,
        "items": [{"uri": i.get("uri"), "score": i.get("score")} for i in (raw.get("memories") or raw.get("results") or [])],
    }


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_candidate_counters(handle) -> Dict[str, float]:
    """Bucket sizes the retriever recorded while building the rerank batches."""
    counters = getattr(handle, "_counters", {}) or {}
    return {
        key: _as_float(value)
        for key, value in counters.items()
        if key.startswith("rerank.candidates") or key.startswith("vector.")
    }


def report_candidate_mix(counters: Dict[str, float]) -> Dict[str, Any]:
    total = counters.get("rerank.candidates", 0.0)
    summaries = counters.get("rerank.candidates.directory_summary", 0.0)
    share = round(summaries / total, 4) if total else None
    return {
        "candidates": _as_int(total),
        "directory_summaries": _as_int(summaries),
        "directory_summary_share": share,
        "vector_searches": _as_int(counters.get("vector.searches", 0.0)),
        "vector_scored": _as_int(counters.get("vector.scored", 0.0)),
    }


def jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages-json", required=True, help="[{role, content}] as committed")
    ap.add_argument("--query", default="", help="override: use this query text verbatim")
    ap.add_argument("--target-uri", action="append", default=None)
    ap.add_argument("--limit", type=int, default=15, help="fetch width per side (production prefetch fetches exactly its limit since the L2-only fix)")
    ap.add_argument("--top", type=int, default=5, help="truncated top-n the consumer reads")
    ap.add_argument("--account", default="dever-space")
    ap.add_argument("--user", default="trae_dever")
    ap.add_argument("--json", default="plans/rerank-mini-verification/step2-replay.json")
    ap.add_argument("--dump-raw", action="store_true")
    ap.add_argument(
        "--off-only",
        action="store_true",
        help="skip the paid rerank side; report candidate composition and the rerank-off result",
    )
    args = ap.parse_args()

    from openviking.server.identity import RequestContext, Role
    from openviking.service.core import OpenVikingService
    from openviking.telemetry import OperationTelemetry, bind_telemetry
    from openviking_cli.session.user_id import UserIdentifier

    try:
        messages = json.loads(Path(args.messages_json).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read --messages-json {args.messages_json}: {exc}")
        return 2
    query = args.query or build_replay_query(messages)
    targets = args.target_uri or DEFAULT_TARGETS

    service = OpenVikingService()
    await service.initialize()
    ctx = RequestContext(
        user=UserIdentifier(args.account, args.user), role=Role(Role.ADMIN), bypass_acl=True
    )
    try:
        sides = []
        rerank_flags = (False,) if args.off_only else (True, False)
        for rerank in rerank_flags:
            # Fresh handle per side: a shared one accumulates both runs' counters
            # and misreports the candidate composition each side actually saw.
            handle = OperationTelemetry(operation="step2-replay", enabled=True)
            with bind_telemetry(handle):
                side = await run_side(service.viking_fs, query, targets, args.limit, rerank, ctx)
            side["candidate_mix"] = report_candidate_mix(read_candidate_counters(handle))
            sides.append(side)
            print(
                f"[rerank={rerank}] candidate={len(side['items'])} "
                f"mix={json.dumps(side['candidate_mix'], ensure_ascii=False)}",
                flush=True,
            )

        candidate_mix = (
            {"on": sides[0]["candidate_mix"], "off": sides[1]["candidate_mix"]}
            if len(sides) > 1
            else sides[0]["candidate_mix"]
        )
        print("candidate mix:", json.dumps(candidate_mix, ensure_ascii=False))

        if args.off_only:
            off_usable = usable_items(sides[0]["items"], args.top)
            report = {
                "query_chars": len(query),
                "targets": targets,
                "limit": args.limit,
                "top": args.top,
                "candidate_mix": candidate_mix,
                "off_usable_count": len(off_usable),
                "off_usable": off_usable,
            }
        else:
            on, off = sides[0], sides[1]
            on_usable = usable_items(on["items"], args.top)
            off_usable = usable_items(off["items"], args.top)
            on_uris = [i["uri"] for i in on_usable]
            off_uris = [i["uri"] for i in off_usable]
            common = [u for u in on_uris if u in off_uris]
            on_set, off_set = set(on_uris), set(off_uris)
            usable_overlap = on_set & off_set

            report = {
                "query_chars": len(query),
                "targets": targets,
                "limit": args.limit,
                "top": args.top,
                "candidate_mix": candidate_mix,
                "on_usable_count": len(on_uris),
                "off_usable_count": len(off_uris),
                "usable_overlap": len(usable_overlap),
                "usable_jaccard": round(jaccard(on_uris, off_uris), 4),
                "usable_coverage": (
                    round(len(usable_overlap) / min(len(on_set), len(off_set)), 4)
                    if on_set and off_set
                    else None
                ),
                "top1_same": bool(on_uris and off_uris and on_uris[0] == off_uris[0]),
                "common_at_top": common,
                "on_only_at_top": [u for u in on_uris if u not in off_set],
                "off_only_at_top": [u for u in off_uris if u not in on_set],
                "ranks": {u: {"on": on_uris.index(u) + 1, "off": off_uris.index(u) + 1} for u in common},
                "on_usable": on_usable,
                "off_usable": off_usable,
                "on_scores_at_limit": {i["uri"]: i["score"] for i in on["items"]},
                "off_scores_at_limit": {i["uri"]: i["score"] for i in off["items"]},
            }
            print(json.dumps({k: v for k, v in report.items() if k not in ("on_scores_at_limit", "off_scores_at_limit")}, ensure_ascii=False, indent=2))
        if args.dump_raw:
            for side in sides:
                print(f"--- raw keys (rerank={side['rerank']}) ---")
                print(json.dumps(side["raw"], ensure_ascii=False)[:3000])
        out = REPO_ROOT / args.json
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nwrote {out}")
    finally:
        await service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
