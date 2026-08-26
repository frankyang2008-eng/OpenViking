# 本地 Jina Rerank 部署（MLX / OpenAI 兼容）

> 本文记录 Jina rerank 模型在本地部署的完整方案：从模型选型、硬件需求、下载安装，
> 到接入 OpenViking（`ov.conf`）与自建 `server.py` / 第三方 `local-reranker` 两条
> 服务路线的利弊对比。适用 Apple Silicon Mac（M1 起，统一内存 8–16GB）。

## 背景：Jina rerank 是否支持本地部署

支持，且很完整。Jina 提供 `jina-reranker` 系列：

| 模型 | 参数 | 说明 |
| --- | --- | --- |
| jina-reranker-v1-base-en | 0.1B | 英文，早期版本 |
| jina-reranker-v2-base-multilingual | 0.3B | 多语 |
| jina-reranker-v3 | 0.6B | 旗舰，多语 listwise 重排（论文口径 15 训练语言，arXiv:2509.25085），Qwen3-0.6B 底座 |
| jina-reranker-v3.5 | ~0.6B | v3 官方后继，**本文实际部署版本** |
| jina-reranker-m0 | 2.4B | 更大的后续变体 |

Apple Silicon 有**官方 MLX 移植版**：`jinaai/jina-reranker-v3.5-mlx`（v3 亦有
`jinaai/jina-reranker-v3-mlx`）。原生 MLX 实现、不依赖 `transformers`，排名分数与原
PyTorch 版 100% 一致。~0.6B 参数，fp16 权重约 1.2GB（官方仓库只发 fp16）；4-bit 量化
约 0.4GB，可自 `mlx_lm.convert -q` 转换，或直接取社区量化版
（`underlotus/jina-reranker-v3.5-mlx-q4` / `-q8`，第三方产物需自行验证）。

### 本地资源需求（以 v3.5 为例）

| 项 | 数值 |
| --- | --- |
| 参数量 | 0.6B |
| MLX fp16 体积 | ~1.2 GB（4-bit ~0.4 GB，需自行 `mlx_lm.convert -q` 转换） |
| 内存 | 8GB Mac 可跑，16GB 舒适 |
| 长上下文 | 32K 输入时内存峰值随输入变长，建议 ≥16GB |
| 非 Apple 机器 | 走 vLLM / Xinference（Ollama 无原生 rerank 支持），NVIDIA 可，CPU 不推荐 |

> ⚠️ 许可证：该模型为 **CC-BY-NC-4.0**（模型卡已确认），个人/开发可用，
> **禁止商用**。商用需联系 Jina AI 购买商业许可，或经 AWS / Azure Marketplace
> 获取授权版本。

## 选型：自建服务 vs local-reranker

两条路线底层跑**同一个模型**（`jina-reranker-v3.5-mlx` + `MLXReranker`），推理性能与
评分完全一致，差别只在服务壳由谁维护。

| 维度 | A：自写 `server.py`（官方模型直连） | B：`local-reranker` 现成服务 |
| --- | --- | --- |
| 代码量 | ~45 行，全可控 | 零代码，一条命令 |
| 依赖 | `mlx` + `fastapi/uvicorn`，最少 | 多一层第三方服务层 |
| 控制力 | schema/截断/超时/鉴权/批处理全自定义 | 受制于项目能力 |
| 适配风险 | 已对照 OpenViking openai client，零适配 | "Jina 兼容 API"，需实测响应格式 |
| 维护 | server.py 自己养（冷启动/内存/自启） | 个人项目，更新慢、API 可能漂移 |
| 可移植 | 仅 Apple | 有 pytorch backend，可切非 Apple 机器 |
| 适合 | 单用户开发机 + 接 OpenViking | 快速试跑 / 多机统一服务 |

**结论**：单用户开发机 + 接入 OpenViking 推荐 **路线 A**；只想快速试跑选 B。

## 前置检查（本机示例）

```bash
uname -m                 # arm64
sysctl -n machdep.cpu.brand_string   # Apple M1
sysctl -n hw.memsize | awk '{printf "%.1f GB\n",$1/1073741824}'   # 16 GB
python3 --version        # 3.14
which uv brew            # 均已安装
```

MLX 仅支持 Apple Silicon（M1 起，Metal GPU + 统一内存），Intel Mac / NVIDIA 机器不可用。

## 部署步骤（路线 A）

### 第 1 步 环境准备

```bash
mkdir -p ~/jina-reranker && cd ~/jina-reranker
uv venv --python 3.12 .venv        # mlx 轮子覆盖 Python 3.10–3.14，3.12 为保守默认
source .venv/bin/activate
```

### 第 2 步 下载模型

```bash
uv pip install modelscope
modelscope download --model JinaAI/jina-reranker-v3.5-mlx \
  --local_dir ~/models/jina-reranker-v3.5-mlx
# 备选：HF 直连（需代理）或 hf-mirror
# uv pip install "huggingface_hub[cli]"
# huggingface-cli download jinaai/jina-reranker-v3.5-mlx --local-dir ~/models/jina-reranker-v3.5-mlx
```

下载完 `du -sh` 核对体积。仓库含独立的 `projector.safetensors`（该架构必需）和推理
`.py` 模块，会一起拉取。

### 第 3 步 安装依赖 + 冒烟

```bash
uv pip install mlx mlx_lm tokenizers numpy fastapi uvicorn
python -c "import mlx; print(mlx.__version__)"   # 预期 0.32.x
```

```python
from rerank import MLXReranker
r = MLXReranker(
    model_path="~/models/jina-reranker-v3.5-mlx",
    projector_path="~/models/jina-reranker-v3.5-mlx/projector.safetensors",
)
out = r.rerank("绿茶的好处", ["Basketball is popular.", "绿茶富含儿茶素。"])
print(out)   # 按 relevance_score 降序的 dict 列表：{document, relevance_score, index, ...}
assert out[0]["index"] == 1
```

### 第 4 步 HTTP 服务（`~/jina-reranker/server.py`）

OpenViking 的 openai rerank 客户端：`POST api_base`，body
`{"model","query","documents":[...]}`，取顶层 `results[].{index, relevance_score}`，
按 index 回填分数。服务端照此格式即可无缝对接。

```python
"""jina-reranker-v3.5-mlx -> OpenAI 兼容 /v1/rerank"""
import os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

MODEL = os.environ.get("JINA_RERANK_MODEL",
                       os.path.expanduser("~/models/jina-reranker-v3.5-mlx"))
if os.path.exists(os.path.join(MODEL, "rerank.py")) and MODEL not in sys.path:
    sys.path.insert(0, MODEL)              # 推理模块在模型目录时挂进 path

app = FastAPI(title="jina-reranker-v3.5-mlx")
_reranker = None
# MLX 推理必须固定单线程：threading.Lock 只串行调用点，跨线程的 Metal 异步
# 残留仍会死锁（实测 8 并发 7 个永久卡死）；单线程执行器从线程亲和性根治
_infer_exec = ThreadPoolExecutor(max_workers=1)
_infer_gate = threading.Semaphore(4)   # 在途请求上限（1 推理 + 3 排队），防线程池耗尽

def get_reranker():                        # 单例懒加载，启动快、内存一次
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

启动：`python server.py`（可 `nohup` 或配 launchd 自启）。

### 第 5 步 服务自检

```bash
curl -s http://127.0.0.1:18080/v1/rerank -H 'Content-Type: application/json' \
  -d '{"model":"jina-reranker-v3.5","query":"绿茶","documents":["Basketball is popular.","绿茶富含儿茶素。"]}'
# 预期: {"model":..., "results":[{"index":1,"relevance_score":...},{"index":0,...}]}
```

### 第 6 步 接入 OpenViking（`ov.conf`）

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

注意：仅当 `api_base` **路径**含 `/api/v1/services/rerank`（DashScope 原生端点）时才
触发嵌套 envelope 格式，与主机名无关，本地服务不受影响；`max_input_tokens` 须为 0 或
≥128，用于压住 32K 上下文内存峰值（M1/16GB 建议 1024–2048）；`threshold` 为严格大于
语义，分数不高于它的结果被过滤；`timeout` 调大对抗冷启动。未配置 rerank 段时检索只
走向量相似度，不报错。

### 第 7 步 端到端验证

```bash
ov search "测试查询"          # 启动日志出现 Rerank enabled (provider=...) 即已生效
# 注："Reranked N documents" 为 DEBUG 级日志，需 ov.conf log.level=DEBUG 才可见
pytest tests -k rerank -v     # 或跑相关单测
```

## 备选 B：local-reranker（零代码）

```bash
uv pip install "local-reranker @ git+https://github.com/olafgeibig/local-reranker.git"
cli serve --backend mlx --model jinaai/jina-reranker-v3.5-mlx --port 18080
```

## 兜底：非 Apple 机器

首选 vLLM（新版已原生支持 jina-reranker-v3，提供 `/v1/rerank` 端点；`--task` 参数已
弃用，按 pooling runner 方式启动，详见 vLLM Scoring 文档）或 Xinference，同样接
ov.conf openai provider。注意：Ollama 无原生 rerank 支持，GGUF 路线不可行。

## 性能预期 & 故障排查

- 首次调用冷启动 1–2s（模型加载），之后 0.6B/4-bit 小批量几十 ms 级；长输入明显变慢。
- 常见坑：`timeout` 不够 → 500/超时；内存不足 → 降 `max_input_tokens` 或换 4-bit；
  HF 下载失败 → `hf-mirror.com` 镜像；端口占用 → 换端口并同步改 ov.conf。

## 参考

- [jina-reranker-v3 (jina.ai)](https://jina.ai/models/jina-reranker-v3)
- [jinaai/jina-reranker-v3.5-mlx (Hugging Face)](https://huggingface.co/jinaai/jina-reranker-v3.5-mlx)
- [jina-reranker-v3.5-mlx (ModelScope 镜像)](https://modelscope.cn/models/JinaAI/jina-reranker-v3.5-mlx)
- [jina-reranker-v3 论文 (arXiv:2509.25085)](https://arxiv.org/abs/2509.25085)
- [vLLM Scoring/Rerank 部署文档](https://docs.vllm.ai/en/latest/models/pooling_models/scoring/)
- [local-reranker (GitHub)](https://github.com/olafgeibig/local-reranker)
- 相关：`docs/en/guides/01-configuration.md`（rerank 配置段）
