# Local Jina Rerank Deployment (MLX / OpenAI-Compatible)

> This guide documents a complete local deployment plan for Jina rerank models: model
> selection, hardware requirements, download & install, and two serving routes —
> a self-written `server.py` vs the third-party `local-reranker` — plus OpenViking
> (`ov.conf`) integration. Targets Apple Silicon Macs (M1 and later, 8–16GB unified memory).

## Background: Does Jina support local rerank?

Yes, fully. Jina ships the `jina-reranker` family:

| Model | Params | Notes |
| --- | --- | --- |
| jina-reranker-v1-base-en | 0.1B | English, earlier version |
| jina-reranker-v2-base-multilingual | 0.3B | Multilingual |
| jina-reranker-v3 | 0.6B | Flagship, multilingual listwise reranker (15 trained languages per paper, arXiv:2509.25085), Qwen3-0.6B base |
| jina-reranker-v3.5 | ~0.6B | Official v3 successor, **the version deployed in this guide** |
| jina-reranker-m0 | 2.4B | Larger later variant |

There is an **official MLX port for Apple Silicon**: `jinaai/jina-reranker-v3.5-mlx`
(v3 also ships as `jinaai/jina-reranker-v3-mlx`).
Native MLX implementation, no `transformers` dependency, and rank scores match the
original PyTorch version 100%. At ~0.6B parameters, fp16 weights are ~1.2GB (the official
repo ships fp16 only); 4-bit quantization is ~0.4GB — convert yourself via
`mlx_lm.convert -q`, or use the community quantized repos
(`underlotus/jina-reranker-v3.5-mlx-q4` / `-q8`, third-party; verify before trusting).

### Local resource requirements (v3.5 as reference)

| Item | Value |
| --- | --- |
| Params | 0.6B |
| MLX fp16 size | ~1.2 GB (4-bit ~0.4 GB, requires self-conversion via `mlx_lm.convert -q`) |
| Memory | 8GB Mac runs it, 16GB comfortable |
| Long context | memory peaks grow with input; ≥16GB recommended for 32K inputs |
| Non-Apple machines | use vLLM / Xinference (Ollama has no native rerank support); NVIDIA OK, CPU not recommended |

> ⚠️ License: this model is **CC-BY-NC-4.0** (confirmed on the model card) — personal/dev
> use is fine, **commercial use is prohibited**. Commercial use requires a commercial
> license from Jina AI, or a licensed offering via the AWS / Azure Marketplace.

## Serving Route Choice: Self-Written vs local-reranker

Both routes run the **same model** (`jina-reranker-v3.5-mlx` + `MLXReranker`) underneath,
so inference performance and scores are identical. The only difference is who maintains
the serving shell.

| Dimension | A: self-written `server.py` (official model) | B: `local-reranker` |
| --- | --- | --- |
| Code | ~45 lines, fully controllable | zero code, one command |
| Dependencies | `mlx` + `fastapi/uvicorn`, minimal | adds a third-party service layer |
| Control | schema/truncation/timeout/auth/batching fully custom | constrained by project capabilities |
| Integration risk | checked against the OpenViking openai client, zero adaptation | "Jina-compatible API"; response format must be verified |
| Maintenance | you own server.py (cold start/memory/autostart) | personal project; slow updates, API drift risk |
| Portability | Apple only | pytorch backend available, switchable to non-Apple machines |
| Best for | single-user dev machine + OpenViking | quick trial / unified multi-machine service |

**Verdict**: for a single-user dev machine integrating with OpenViking, choose **Route A**;
choose B for a quick trial.

## Preflight (example host)

```bash
uname -m                 # arm64
sysctl -n machdep.cpu.brand_string   # Apple M1
sysctl -n hw.memsize | awk '{printf "%.1f GB\n",$1/1073741824}'   # 16 GB
python3 --version        # 3.14
which uv brew            # installed
```

MLX works only on Apple Silicon (M1 and later, Metal GPU + unified memory). Intel Mac /
NVIDIA machines cannot use it.

## Deployment Steps (Route A)

### Step 1 Environment

```bash
mkdir -p ~/jina-reranker && cd ~/jina-reranker
uv venv --python 3.12 .venv        # mlx wheels cover Python 3.10–3.14; 3.12 is a conservative default
source .venv/bin/activate
```

### Step 2 Download the model

```bash
uv pip install modelscope
modelscope download --model JinaAI/jina-reranker-v3.5-mlx \
  --local_dir ~/models/jina-reranker-v3.5-mlx
# Alternatives: HF direct (needs proxy) or hf-mirror
# uv pip install "huggingface_hub[cli]"
# huggingface-cli download jinaai/jina-reranker-v3.5-mlx --local-dir ~/models/jina-reranker-v3.5-mlx
```

Verify size with `du -sh`. The repo includes a separate `projector.safetensors`
(required by this architecture) and the inference `.py` module.

### Step 3 Install dependencies + smoke test

```bash
uv pip install mlx mlx_lm tokenizers numpy fastapi uvicorn
python -c "import mlx; print(mlx.__version__)"   # expect 0.32.x
```

```python
from rerank import MLXReranker
r = MLXReranker(
    model_path="~/models/jina-reranker-v3.5-mlx",
    projector_path="~/models/jina-reranker-v3.5-mlx/projector.safetensors",
)
out = r.rerank("绿茶的好处", ["Basketball is popular.", "绿茶富含儿茶素。"])
print(out)   # list of dicts sorted by relevance_score desc: {document, relevance_score, index, ...}
assert out[0]["index"] == 1
```

### Step 4 HTTP service (`~/jina-reranker/server.py`)

The OpenViking openai rerank client: `POST api_base`, body
`{"model","query","documents":[...]}`, reads top-level `results[].{index, relevance_score}`,
mapping scores back by index. Serve exactly that shape for a seamless fit.

```python
"""jina-reranker-v3.5-mlx -> OpenAI-compatible /v1/rerank"""
import os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

MODEL = os.environ.get("JINA_RERANK_MODEL",
                       os.path.expanduser("~/models/jina-reranker-v3.5-mlx"))
if os.path.exists(os.path.join(MODEL, "rerank.py")) and MODEL not in sys.path:
    sys.path.insert(0, MODEL)              # expose inference module if shipped in model dir

app = FastAPI(title="jina-reranker-v3.5-mlx")
_reranker = None
# MLX inference must stay on ONE thread: a threading.Lock only serializes call
# sites; pending async Metal work from another thread still deadlocks (observed:
# 7/8 concurrent requests stuck forever). Single-worker executor fixes by affinity.
_infer_exec = ThreadPoolExecutor(max_workers=1)
_infer_gate = threading.Semaphore(4)   # in-flight cap (1 running + 3 queued)

def get_reranker():                        # lazy singleton: fast start, load once
    global _reranker
    if _reranker is None:
        from rerank import MLXReranker
        _reranker = MLXReranker(model_path=MODEL,
                                projector_path=os.path.join(MODEL, "projector.safetensors"))
    return _reranker

class RerankRequest(BaseModel):
    model: Optional[str] = "jina-reranker-v3.5"
    query: str
    documents: List[str]

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
    uvicorn.run(app, host="127.0.0.1", port=18080)
```

Run: `python server.py` (optionally under `nohup` or a launchd agent).

### Step 5 Service self-check

```bash
curl -s http://127.0.0.1:18080/v1/rerank -H 'Content-Type: application/json' \
  -d '{"model":"jina-reranker-v3.5","query":"绿茶","documents":["Basketball is popular.","绿茶富含儿茶素。"]}'
# expect: {"model":..., "results":[{"index":1,"relevance_score":...},{"index":0,...}]}
```

### Step 6 OpenViking integration (`ov.conf`)

```json
{
  "rerank": {
    "provider": "openai",
    "api_key": "local",
    "api_base": "http://127.0.0.1:18080/v1/rerank",
    "model": "jina-reranker-v3.5",
    "timeout": 120,
    "max_input_tokens": 2048,
    "threshold": 0.1
  }
}
```

Notes: the nested-envelope format is triggered only when the `api_base` **path** contains
`/api/v1/services/rerank` (the DashScope native endpoint) — hostname is irrelevant, so a
local service is unaffected; `max_input_tokens` must be 0 or ≥128 and caps memory peaks on
32K contexts (1024–2048 suggested on M1/16GB); `threshold` uses strict greater-than
filtering; raise `timeout` to absorb cold start. Without a rerank section, retrieval uses
vector similarity only and does not error.

### Step 7 End-to-end validation

```bash
ov search "test query"        # startup log "Rerank enabled (provider=...)" confirms activation
# note: "Reranked N documents" is a DEBUG-level log; set ov.conf log.level=DEBUG to see it
pytest tests -k rerank -v     # or run relevant unit tests
```

## Route B: local-reranker (zero code)

```bash
uv pip install "local-reranker @ git+https://github.com/olafgeibig/local-reranker.git"
cli serve --backend mlx --model jinaai/jina-reranker-v3.5-mlx --port 18080
```

## Fallback: non-Apple machines

Prefer vLLM (recent versions natively support jina-reranker-v3 and expose a `/v1/rerank`
endpoint; the `--task` flag is deprecated — serve it via the pooling runner, see the vLLM
Scoring docs) or Xinference; both wire into the ov.conf openai provider. Note: Ollama has
no native rerank support, so the GGUF route is not viable.

## Performance & Troubleshooting

- First call has a 1–2s cold start (model load); after that, 0.6B/4-bit batches run in tens
  of ms; longer inputs get visibly slower.
- Common issues: `timeout` too small -> 500/timeouts; out of memory -> lower
  `max_input_tokens` or switch to 4-bit; HF download failure -> `hf-mirror.com` mirror;
  port in use -> change port and update ov.conf accordingly.

## References

- [jina-reranker-v3 (jina.ai)](https://jina.ai/models/jina-reranker-v3)
- [jinaai/jina-reranker-v3.5-mlx (Hugging Face)](https://huggingface.co/jinaai/jina-reranker-v3.5-mlx)
- [jina-reranker-v3.5-mlx (ModelScope mirror)](https://modelscope.cn/models/JinaAI/jina-reranker-v3.5-mlx)
- [jina-reranker-v3 paper (arXiv:2509.25085)](https://arxiv.org/abs/2509.25085)
- [vLLM Scoring/Rerank deployment docs](https://docs.vllm.ai/en/latest/models/pooling_models/scoring/)
- [local-reranker (GitHub)](https://github.com/olafgeibig/local-reranker)
- Related: `docs/en/guides/01-configuration.md` (rerank config section)
