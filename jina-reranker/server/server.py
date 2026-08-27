"""jina-reranker-v3.5-mlx -> OpenAI-compatible /v1/rerank"""
import argparse, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "jina-reranker-v3.5-mlx"

app = FastAPI(title="jina-reranker-v3.5-mlx")
_reranker = None
# All MLX calls pinned to ONE thread: a plain lock only serializes call sites;
# pending async Metal work from another thread still deadlocks (observed: 7/8
# concurrent requests stuck forever). Single-worker executor fixes by affinity.
_infer_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx-infer")
# gates in-flight requests (1 running + N-1 queued); unbounded queuing would exhaust
# anyio's 40-thread pool and keep inferring for clients that already timed out
_infer_gate = threading.Semaphore(4)

def get_reranker():
    global _reranker
    if _reranker is None:
        model = os.environ.get("JINA_RERANK_MODEL", str(DEFAULT_MODEL))
        if os.path.exists(os.path.join(model, "rerank.py")) and model not in sys.path:
            sys.path.insert(0, model)
        from rerank import MLXReranker
        _reranker = MLXReranker(model_path=model,
                                projector_path=os.path.join(model, "projector.safetensors"))
    return _reranker

class RerankRequest(BaseModel):
    model: Optional[str] = "jina-reranker-v3.5"
    query: str
    documents: List[str]

_PROBE_TIMEOUT = 45   # cold load measured up to ~32s under machine load
_MAX_FAILS = 2        # hysteresis: don't restart on one slow-but-legit batch

def _health_monitor(interval: int):
    """Probe the executor with a real tiny inference; execv-restart after
    _MAX_FAILS consecutive failures. Detects the sleep/wake Metal wedge where
    the port stays LISTEN but every inference hangs with ~0 CPU."""
    fails = 0
    while True:
        time.sleep(interval)
        try:
            fut = _infer_exec.submit(lambda: get_reranker().rerank("health", ["alive"]))
            fut.result(timeout=_PROBE_TIMEOUT)
            if fails:
                print(f"[watchdog] probe recovered", file=sys.stderr, flush=True)
            fails = 0
        except Exception as e:
            fails += 1
            print(f"[watchdog] probe failed {fails}/{_MAX_FAILS}: {type(e).__name__}",
                  file=sys.stderr, flush=True)
            if fails >= _MAX_FAILS:
                print("[watchdog] restarting process (execv)", file=sys.stderr, flush=True)
                os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])

@app.post("/v1/rerank")
def rerank(req: RerankRequest):
    if not req.documents:
        return {"model": req.model, "results": []}
    try:
        with _infer_gate:
            out = _infer_exec.submit(
                lambda: get_reranker().rerank(req.query, req.documents)
            ).result()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    results = [{"index": r["index"], "relevance_score": float(r["relevance_score"])} for r in out]
    results.sort(key=lambda x: x["relevance_score"], reverse=True)
    return {"model": req.model, "results": results}

if __name__ == "__main__":
    import uvicorn
    p = argparse.ArgumentParser(description="Jina rerank MLX server (OpenAI-compatible)")
    p.add_argument("--model", default=str(DEFAULT_MODEL), help="model directory (contains rerank.py)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--workers", type=int, default=1,
                   help="uvicorn worker processes; each loads its own model copy (~1.2GB RAM each)")
    p.add_argument("--watchdog-interval", type=int, default=180,
                   help="in-process health probe interval seconds; 0 disables")
    args = p.parse_args()
    os.environ["JINA_RERANK_MODEL"] = os.path.expanduser(args.model)
    if args.watchdog_interval > 0:
        threading.Thread(
            target=_health_monitor, args=(args.watchdog_interval,),
            daemon=True, name="watchdog",
        ).start()
    if args.workers > 1:
        uvicorn.run("server:app", host=args.host, port=args.port, workers=args.workers)
    else:
        uvicorn.run(app, host=args.host, port=args.port)
