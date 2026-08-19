# ov.conf 全面配置评审与优化方案（2026-08-18）

> 评审基线：ov-dev-opt @ b262b05b（v0.4.14 + #4055/#4065 合并后）
> 评审方式：三模型对抗评审 —— deepseek-v4-pro（性能/架构）、glm-5.3（安全/健壮性）、kimi-k3（schema 对齐），leader 交叉裁决
> 说明：glm-5.3 与 kimi-k3 的 subagent 执行中途截断，其评审维度由 leader 亲自取证补位完成；deepseek-v4-pro 报告完整保留，其错误结论已被对抗裁决驳回（见 §4）

---

## 1. 结论摘要

| 级别 | 数量 | 要点 |
|---|---|---|
| P1（应改） | 4 | allowFrom 空数组放行所有；allow_private_networks=true 关 SSRF 护栏；三处 max_concurrent=1 过度限流；max_retries=1 与熔断组合脆弱 |
| P2（建议） | 3 | pathlock.lock_timeout_secs 死键删除；明文密钥集中风险；text_source 与 multimodal 组合待验证 |
| 驳回 | 2 | score_propagation_alpha=1 即默认值无需改；experiences.enabled 配置键不存在 |

**最小修改集**（一次落地）：

```json
{
  "queue_workers": { "external_parse": { "max_concurrent": 2 } },
  "storage": {
    "workspace": "/Users/frankyang-mp2/.openviking/data",
    "vectordb": { "backend": "local" },
    "agfs": { "backend": "local" }
  },
  "embedding": { "max_concurrent": 4, "max_retries": 3 },
  "vlm": { "max_concurrent": 4, "max_retries": 3 },
  "bot": { "channels": [ { "...": "...", "allowFrom": ["<你的飞书 open_id>"] } ] }
}
```

---

## 2. 三方评审发现（含证据）

### 2.1 安全与健壮性（glm-5.3 视角，leader 取证）

**P1-A：飞书 allowFrom=[] 放行所有发送者**
- 证据：`bot/vikingbot/channels/base.py:127-129` —— `if not allow_list: return True`；官方文档 `bot/README.md:514` 明确 "allow_from: [] 表示不限制发送者"
- 风险：任何能找到该飞书机器人的用户都可调用 ov_tools（含 viking_forget 等破坏性工具）
- 修复：`"allowFrom": ["ou_你的openid"]`（openid 可从 vikingbot 日志获取，见 docs/en/concepts/05-channel.md:269）

**P1-B：allow_private_networks=true 关闭 SSRF 护栏**
- 证据：`openviking/utils/network_guard.py:40-44,119` —— 该开关为 True 时放行对内网地址的外部抓取
- v0.4.14 放大面：add_resource/watch 支持私有 Git 仓库与 URL 抓取，均走此护栏
- 判断：本机开发场景（127.0.0.1 服务端 + 本地 Ollama 依赖）保留 true 有实际必要，但需知晓含义；若日后暴露公网必须关掉
- 修复：保持现状，文档化风险；公网部署前改为 false

**P2-C：cors_origins=["*"] 与 /metrics 无鉴权**
- 证据：`openviking/server/config.py:288` 默认即 `["*"]`；`openviking/server/routers/metrics.py:13` 无 auth 依赖；`server/app.py:609` 直接挂载
- 缓解：server.host=127.0.0.1 仅本机监听，实际敞口极小
- 修复：不改。若未来开放远程访问，同步收紧两项

**P2-D：明文密钥**
- 证据：ark api_key、feishu appSecret、bot api_key 均明文存于 ov.conf；`openviking/models/vlm/base.py:70` 直接 `config.get("api_key")`，未发现环境变量注入支持
- 修复：保持文件权限 `chmod 600 ~/.openviking/ov.conf`；不纳入本次修改

### 2.2 性能与架构（deepseek-v4-pro 报告，经裁决）

**P0-E：三处 max_concurrent=1 过度限流**（采纳）
- 证据：代码默认 embedding=10 / vlm=32 / external_parse=4（service/core.py、queue_manager.py）
- 影响：资源导入、记忆抽取的 embedding/VLM 调用全部串行，吞吐降至默认 1/10~1/32；本机 Ark 端点限流压力远小于此
- 修复：embedding/vlm 提至 4，external_parse 提至 2（M1 保守值；dsv 建议 4，leader 裁决 external_parse 取 2，因外部解析含子进程/网络长尾）

**P1-F：max_retries=1 + failure_threshold=5 组合脆弱**（采纳）
- 证据：`openviking/models/vlm/backends/volcengine_vlm.py:224` 重试条件 `attempt < max_retries`，=1 即零重试；单次偶发超时直接计失败，5 次即熔断 60s
- 修复：`max_retries: 3`（代码默认值即 3，见 vlm/base.py:73）

**P2-G：text_source=content_only 与 embedding.input=multimodal 组合**（待验证）
- dsv 主张两者冲突导致多模态输入被截断；leader 未能完全证实截断路径
- 修复：暂不改。下次资源导入多模态文件效果不佳时再验证

**其余 dsv 发现见 §4 驳回项。**

### 2.3 Schema 对齐（kimi-k3 视角，leader 取证）

**P2-H：storage.agfs.pathlock.lock_timeout_secs 是死键**
- 证据：`openviking_cli/utils/config/agfs_config.py` `AGFSPathLockConfig.ignore_deprecated_lock_timeout` —— 检测到该键即告警并强制归零；`openviking_cli/utils/config/transaction_config.py:9` 注释 "Prefer storage.agfs.pathlock only for active expiry configuration"
- 修复：从 ov.conf 删除整个 `pathlock` 段

**有效键确认（无需改）**：
- `memory.extraction_enabled`：仍被消费（`openviking/session/session.py:2521`），dsv/kimi 关于"已更名 memory_extraction_enabled"的假设不成立（那是局部变量名）
- `memory.session_skill_extraction_enabled` / `link_enabled` / `eager_prefetch` / `prefetch_search_topn`：均被消费（compressor_v3.py:727 等）
- log 段全部键与 `openviking_cli/utils/config/log_config.py` 字段一一对应
- `retrieval.hotness_alpha=0.15`：有效用户覆盖（默认 0.0，0 为禁用热度加成）
- `server.agent_evolution.enabled=true`：被 compressor_v3.py 消费，控制 trajectories/cases 派生记忆生成
- `embedding.circuit_breaker` 三键：有效

---

## 3. 对抗裁决记录

| 主张 | 提出方 | 裁决 | 理由 |
|---|---|---|---|
| score_propagation_alpha=1 应改 0.5 | deepseek-v4-pro | **驳回** | `retrieval_config.py` 默认值即 1.0，语义为"子节点自身分数权重"，用户配置=默认，无漂移 |
| 新增 experiences.enabled / trajectories.enabled | deepseek-v4-pro | **驳回** | 全仓 grep 无此配置键；experiences/cases/trajectories 由 agent_evolution.enabled + memory type 控制，且相关配置模型多为 extra:"forbid"，加了会报错 |
| extraction_enabled 已更名 | deepseek-v4-pro | **驳回** | session.py:2521 直接读 `ov_config.memory.extraction_enabled` |
| query_planner 需换多模态模型 | deepseek-v4-pro | **驳回** | query planner 处理文本查询规划，无 vision 需求；seed-2.0-mini 定位正确（低成本快响应） |
| external_parse 提至 4 | deepseek-v4-pro | **改判 2** | 外部解析含长尾网络/子进程，M1 上 2 更稳 |

---

## 4. 最终落地修改集

对 `~/.openviking/ov.conf` 的完整 diff（5 处）：

1. **删**：`storage.agfs.pathlock` 整段（死键，启动时告警）
2. **改**：`embedding.max_concurrent` 1 → 4；`embedding.max_retries` 1 → 3
3. **改**：`vlm.max_concurrent` 1 → 4；`vlm.max_retries` 1 → 3
4. **改**：`queue_workers.external_parse.max_concurrent` 1 → 2
5. **改**：`bot.channels[0].allowFrom` [] → `["<你的飞书 open_id>"]`（需先从 vikingbot 日志拿到 openid；拿到前保持现状并知晓风险）

**不改（有理由）**：allow_private_networks（本机开发必要）、cors_origins/metrics（仅 127.0.0.1）、retrieval 两参数（=默认或有效覆盖）、log 段（全部有效）、模型分层（定位正确）、明文密钥（chmod 600 即可）。

**验证方式**：改后重启 `openviking-server`，确认启动日志无 pathlock 告警；跑一次 add_resource 观察 embedding/VLM 并发日志；飞书发一条消息确认 allowFrom 白名单生效。

---

## 5. 遗留观察项

- text_source=content_only × input=multimodal 组合（P2-G）：下次多模态资源导入效果异常时优先验证
- 上游若新增配置键（watch 调度、reindex 标签等），随下次 merge 例行复查
- 明文密钥无 env 注入支持，可作为回馈上游的 feature request
