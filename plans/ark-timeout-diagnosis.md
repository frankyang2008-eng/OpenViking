# Ark phase2 超时根因诊断报告

日期：2026-09-28 · 范围：session_commit_phase2 自 9-27 起的批量 `ArkAPITimeout`
方法：日志全量盘点 + 配置/链路核对 + 分层网络实测 + 真实重型请求采样。禁猜测，全部原始数据在案。

---

## 结论先行

**主因不是网络问题，是端点侧模型对重型请求处理时间长（46–65s）且高负载时突破 client timeout（120s）。**
本地网络、代理软件、TLS、代理 vs 直连均已排除。超时本质是 **timeout 配置与端点实际处理时长（含大量 reasoning）不匹配**，叠加应用层最多 4 次串行重试把失败时间拉到 ~544s。

## 1. 超时日志盘点（17 条，6 簇，全量）

`~/.openviking/data/log/openviking.log.2026-09-27` + `openviking.log`，去重后 17 条，成 6 簇：

| 簇时刻 | 同时超时子系统（同一秒） |
|---|---|
| 09-27 17:24:05 | compressor_v3 / streaming_batcher / session |
| 09-27 19:48:17 | compressor_v3 / streaming_batcher / session |
| 09-27 21:14:38 | compressor_v3 / session / trajectory_analyzer |
| 09-27 23:37:36 | compressor_v3 / session |
| 09-28 11:52:14 | compressor_v3 / session / trajectory_analyzer |
| 09-28 12:10:17 | compressor_v3 / streaming_batcher / session |

- 子系统分布：compressor_v3 ×6、session(long_term) ×6、streaming_batcher ×3、trajectory_analyzer ×2
- 特征：每簇多个**独立**子系统在**同一秒**超时 → 它们是 commit 后并发的一批重型请求，同时达到各自 timeout，而非各自随机变慢
- phase2 整体（开始→报错）实测：**157s / 208s / 544s**

## 2. 链路与配置核对（源码行号）

- 端点：`https://ark.cn-beijing.volces.com/api/plan/v3`（ov.conf，plan 特殊通道）
- Ark client：`openviking/models/vlm/backends/volcengine_vlm.py:143-155`，`timeout=self.timeout`、`max_retries=0`（SDK 重试关）
- `create_optional_async_httpx_client`（`openviking/models/network.py:131`）对普通 https URL 返回 **None** → 用 SDK 默认 httpx client（trust_env=True，读 env）
- 代理来源：旧 server 进程环境含 `HTTPS_PROXY=127.0.0.1:10808`；macOS 系统代理 HTTP/HTTPS/SOCKS 全指 `127.0.0.1:10808`（`scutil --proxy`）
- xray（v2rayN）：mixed inbound 127.0.0.1:10808，TUN 模式开；路由 `domain:volces.com → direct`（实际仍直连火山）
- timeout：vlm **120s**（ov.conf，long_term 等重型步骤用此）
- 应用层重试：`openviking/session/session.py:91-93` `_MEMORY_EXTRACTION_MAX_RETRIES=3`（**共 4 次尝试**），退避 1→2→4（max 8s）
- 最坏时长校验：4×120 + (1+2+4) + prefetch 前置 ≈ **487 + ~50 ≈ 537-550s** → 与实测 **544s** 吻合

堆栈定位：`httpcore/_async/http_proxy.py` → `http11.py _receive_event`：已过代理 CONNECT、请求已发出，**卡在等待服务端响应（read）**，不是建连失败。

## 3. 网络分层实测（排除网络）

| 层 | 结果 |
|---|---|
| DNS | ark.cn-beijing.volces.com → 101.126.7.76（223.5.5.5 同），正常 |
| TCP 443（直连 ×5） | RTT **5–7ms**，全成功 |
| TLS 握手 | ~0.09s |
| 轻量 HTTP（GET models，两路径 ×12） | **24/24 成功**，total ~0.13s；代理 vs 直连无差异 |

→ 网络出口、xray、TLS、代理转发在测量时刻完全健康；轻量请求零失败。

## 4. 真实重型请求采样（定位端点慢）

payload：14,845 字符 prompt（68 条真实归档拼接），model doubao-seed-2.1-lite，max_tokens=1500；两路径各 6 次，共 12 次：

| 路径 | ttfb/total | 结局 |
|---|---|---|
| 经代理 10808 ×6 | 51.6 – 55.4s | 12/12 code=200 |
| 直连 ×6 | 46.0 – 65.0s | 12/12 code=200 |

- total ≈ ttfb：服务端处理占 99%+，网络传输仅几十毫秒
- usage（代表性）：prompt **7230** / completion **3881**，其中 **`reasoning_tokens=3029`**（可见输出仅 ~852）；model 实际版本 `doubao-seed-2-1-lite-260915`
- **模型对 7k prompt 固定做 ~3000 token 内部 reasoning，即 46–65s 的主体**；`max_tokens=1500` 不约束 reasoning tokens
- 波动 46→65s（±20s）：端点高负载/排队时再上飘，突破 120s → 当次 read timeout；应用层重试期间端点仍高负载 → 4 次尝试后 phase2 失败

## 4b. 措施可行性验证：关闭 reasoning（已实测）

同一重型 payload + `"thinking": {"type": "disabled"}`，3 次 + 1 次计时：

| 指标 | reasoning 开 | reasoning 关 |
|---|---|---|
| reasoning_tokens | 3029 | **0** |
| completion_tokens | ~3881 | **929–1052** |
| 处理时长（ttfb/total） | 46 – 65s | **15.1s（−约70%）** |
| 成功率 | 12/12 | 3/3 |

→ 后台重型步骤（long_term / streaming train / trajectory）关 reasoning 技术可行，直接消除超时（15s ≪ timeout 120s），同时省 completion token。**唯一待验证：关思考后记忆提取/训练质量是否下降**，需小样本 ON/OFF 质量对比后再上。

## 5. 为什么 9-27 起密集出现

证据支持的解释（非唯一，需端点侧数据佐证）：
1. plan/v3 通道或 doubao-seed-2.1-lite 节点自 9-27 起负载升高/排队（处理时长分布整体上移）
2. 归档消息随会话累积，prompt 变大 → reasoning 时长随之增加
本地侧无变更可解释该拐点（rerank P0 在 9-25，且已证无关）。

## 6. 建议（按性价比排序，数据驱动）

| # | 措施 | 依据 | 取舍 |
|---|---|---|---|
| 1 | **重型步骤关闭 reasoning**（payload 加 `thinking:{type:disabled}`） | **已实测可行**：46-65s→15s、reasoning→0、completion 省 ~75%；根治超时 | 需小样本对比关思考前后记忆提取/训练质量，确认无回退再上 |
| 2 | timeout 120s → **180-240s**（重型步骤单独配） | 覆盖端点 p99；先消除误杀 | 单次失败更久才感知；属缓解非根治 |
| 3 | 减小重型 prompt：归档分批更小、更激进压缩 | prompt 7230 直接驱动 reasoning 量 | 增加调用次数，需平衡 |
| 4 | 重试加抖动/端点高负载时快速熔断 | 4 次串行重试把失败拉到 544s 且加重端点负载 | 配合断路器 |

**不建议**：归因于本地网络而更换代理/重启 xray（实测两路径全通）；降级模型档位以外的网络折腾。

## 7. 成本披露

- 轻量探测 24 次（404，无 token 计费）
- 重型请求 17 次（13 reasoning 开 + 4 关），合计约 **16 万 tok**；doubao-seed-2.1-lite 按 plan 通道单价，约 **¥0.06–0.1**
