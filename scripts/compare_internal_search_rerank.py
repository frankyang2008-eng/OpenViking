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

Query construction replicates the production prefetch query (user sections first,
then assistant, per-part caps 1000/500 chars, whole query capped at 5000) so an
offline run sees the same text the live prefetch saw.

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

USER_PART_MAX_CHARS = 1000
ASSISTANT_PART_MAX_CHARS = 500
QUERY_MAX_CHARS = 5000
DEFAULT_TARGETS = [
    "viking://user/trae_dever/memories/preferences",
    "viking://user/trae_dever/memories/entities",
    "viking://user/trae_dever/memories/cases",
]


def build_prefetch_query(messages: List[Dict[str, Any]]) -> str:
    """Mirror of SessionExtractContextProvider._build_prefetch_search_query."""
    primary: List[str] = []
    supporting: List[str] = []
    for msg in messages:
        role = msg.get("role", "")
        speaker = msg.get("peer_id") or role
        text = " ".join(str(msg.get("content") or "").split())
        if not text:
            continue
        cap = USER_PART_MAX_CHARS if role == "user" else ASSISTANT_PART_MAX_CHARS
        if len(text) > cap:
            text = text[: cap - 3].rstrip() + "..."
        section = f"{speaker}: {text}"
        (primary if role == "user" else supporting).append(section)
    query = "\n\n".join(primary + supporting)
    if len(query) > QUERY_MAX_CHARS:
        query = query[: QUERY_MAX_CHARS - 3].rstrip() + "..."
    return query


def optimize_top(result: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """Mirror of MemorySearchTool: drop abstract/overview files, then cut to limit."""
    items = result.get("memories") or result.get("results") or []
    out: List[Dict[str, Any]] = []
    for item in items:
        uri = str(item.get("uri") or "")
        if not uri:
            continue
        if uri.endswith(".abstract.md") or uri.endswith(".overview.md"):
            continue
        out.append({"uri": uri, "score": item.get("score")})
        if len(out) >= limit:
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
    ap.add_argument("--limit", type=int, default=15, help="over-fetched limit (production uses 5+10)")
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
    query = args.query or build_prefetch_query(messages)
    targets = args.target_uri or DEFAULT_TARGETS

    service = OpenVikingService()
    await service.initialize()
    ctx = RequestContext(
        user=UserIdentifier(args.account, args.user), role=Role(Role.ADMIN), bypass_acl=True
    )
    try:
        sides = []
        handle = OperationTelemetry(operation="step2-replay", enabled=True)
        rerank_flags = (False,) if args.off_only else (True, False)
        for rerank in rerank_flags:
            with bind_telemetry(handle):
                side = await run_side(service.viking_fs, query, targets, args.limit, rerank, ctx)
            sides.append(side)
            print(f"[rerank={rerank}] candidate={len(side['items'])}", flush=True)

        candidate_mix = report_candidate_mix(read_candidate_counters(handle))
        print("candidate mix:", json.dumps(candidate_mix, ensure_ascii=False))

        if args.off_only:
            off_top = optimize_top(sides[0]["raw"], args.top)
            report = {
                "query_chars": len(query),
                "targets": targets,
                "limit": args.limit,
                "top": args.top,
                "candidate_mix": candidate_mix,
                "off_top": off_top,
            }
        else:
            on, off = sides[0], sides[1]
            on_top = optimize_top(on["raw"], args.top)
            off_top = optimize_top(off["raw"], args.top)
            on_all = [i["uri"] for i in on["items"]]
            off_all = [i["uri"] for i in off["items"]]
            on_uris = [i["uri"] for i in on_top]
            off_uris = [i["uri"] for i in off_top]
            common = [u for u in on_uris if u in off_uris]

            report = {
                "query_chars": len(query),
                "targets": targets,
                "limit": args.limit,
                "top": args.top,
                "candidate_mix": candidate_mix,
                "jaccard_at_top": round(jaccard(on_uris, off_uris), 4),
                "jaccard_at_limit": round(jaccard(on_all, off_all), 4),
                "top1_same": bool(on_uris and off_uris and on_uris[0] == off_uris[0]),
                "common_at_top": common,
                "on_only_at_top": [u for u in on_uris if u not in off_uris],
                "off_only_at_top": [u for u in off_uris if u not in on_uris],
                "ranks": {u: {"on": on_uris.index(u) + 1, "off": off_uris.index(u) + 1} for u in common},
                "on_top": on_top,
                "off_top": off_top,
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
