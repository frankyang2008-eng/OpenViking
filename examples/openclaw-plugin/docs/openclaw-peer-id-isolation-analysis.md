# OpenClaw 多 Agent 记忆隔离：peer_id 写入路径分析

> 状态：只读分析，未修改任何代码或服务端配置  
> 日期：2026-07-03  
> 关联系统：OpenClaw (115.191.54.21) + OpenViking

## 1. 问题现象

在 OpenClaw 生产环境（`115.191.54.21`）中，15 个 Feishu agent 共用同一个 OpenViking `account/user` 空间，且 `openclaw.json` 里的 OpenViking 插件配置是全局一份：

```json
"openviking": {
  "enabled": true,
  "config": {
    "mode": "remote",
    "baseUrl": "http://127.0.0.1:2933",
    "peer_role": "assistant",
    "peer_prefix": "openclaw-agent"
  }
}
```

结果是：**所有 agent 的长期记忆都被写入 `viking://user/<user>/memories`，没有进入各自的 `peers/<peerId>/memories` 目录**，因此多个 agent 之间的记忆在写入层面并未隔离。

## 2. 根因：写入路径缺少 `peer_id`

### 2.1 OpenClaw plugin 发送了什么

`examples/openclaw-plugin/client.ts:723-789` 的 `addSessionMessage` 和 `820-857` 的 `commitSession` 都只在 HTTP header 里设置了 `X-OpenViking-Actor-Peer`，请求体中**没有 `peer_id` 字段**。

```typescript
// client.ts
await this.request<{ session_id: string }>(
  `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
  {
    method: "POST",
    body: JSON.stringify(body),  // body 里只有 role / parts / created_at / role_id
  },
  undefined,
  actorPeerId,  // 只作为 X-OpenViking-Actor-Peer 头发送
);
```

### 2.2 OpenViking server 认什么

`openviking/server/routers/sessions.py:391-417` 决定消息归属 peer 时，只看两样东西：

```python
def _resolve_message_peer_id(request, ctx):
    if request.peer_id:
        return _sanitize_peer_id(request.peer_id)
    if ctx.legacy_agent_id:
        return _sanitize_peer_id(ctx.legacy_agent_id)
    return None
```

- `request.peer_id`：来自请求 body；
- `ctx.legacy_agent_id`：来自旧头 `X-OpenViking-Agent`；
- `ctx.actor_peer_id`（来自 `X-OpenViking-Actor-Peer`）**不参与此判断**。

### 2.3 最终导致记忆提取不走 peer 目录

`openviking/session/session.py:1369-1390` 在 commit 时：

```python
extraction_scope = _resolve_memory_extraction_scope(...)
allowed_peer_ids = extraction_scope.allowed_peer_ids
return await self._session_compressor.extract_long_term_memories(
    ...,
    allowed_peer_ids=allowed_peer_ids,
)
```

而 `_resolve_memory_extraction_scope` 里的 `allowed_peer_ids` 只从 `message.peer_id` 来，`actor_peer_id` 不参与。于是 `allowed_peer_ids` 为空，`MemoryIsolationHandler` 只把记忆写到用户自身空间。

## 3. 读取行为：peerId 只隔离 `peers/` 子目录

`openviking/core/retrieval_targets.py:64-69` 显示，当请求带 `actor_peer_id` 时，默认检索范围是：

```python
[
    f"{user_root}/memories",
    f"{user_root}/peers/{ctx.actor_peer_id}/memories",
]
```

- `viking://user/<user>/memories`：**所有 peer 共享**；
- `viking://user/<user>/peers/<peerId>/memories`：**只被自己 peer 访问**，其他 peer 会被 `is_hidden_by_actor_peer_view` 拒绝。

因此，**读取时 peerId 能隔离 `peers/` 下的内容，但无法隔离实际写入到用户根目录的长期记忆**。

## 4. 能否在 `openclaw.json` 里按 agent 配置 peer_id？

**不能。**

- `openclaw.json` 里的 OpenViking 配置是全局的，位于 `plugins.entries.openviking.config`；
- `agents.list` 每个 agent 条目没有插件配置覆盖字段；
- `examples/openclaw-plugin/config.ts:373-434` 的 schema 允许 key 列表里只有 `peer_role`、`peer_prefix`、`accountId`、`userId` 等，**没有 `peer_id` 或 per-agent peer 映射**；
- `peer_id` 是运行时由 `examples/openclaw-plugin/routing/identity-routing.ts:137-210` 用 `peer_prefix + rawAgentId` 推导出来的，例如 `openclaw-agent_feishu_liuli`。

所以即使想让某个 agent 用固定 peer_id，当前配置体系也不支持。

## 5. 官方多租户测试报告的结论与缺口

官方报告 [`openclaw-multi-tenant-test-report.md`](https://github.com/volcengine/OpenViking/blob/main/examples/openclaw-plugin/openclaw-multi-tenant-test-report.md) 验证的是：

- `X-OpenViking-Actor-Peer` 头能正确生成；
- `runtimeContext.senderId -> role_id` 在 `afterTurn` 路径正确；
- `requesterSenderId -> role_id` 在 `memory_store` 工具路径正确；
- 命名空间策略 `ff/tf/tt` 在检索层面能按预期命中。

**但官方报告没有验证 session commit 的长期记忆提取是否进入 peer 目录。** 也就是说：

> 官方证明了“身份透传链路是对的”，但没有证明“基于 peer 的长期记忆写入隔离已经生效”。

## 6. 可行的修复方向（待官方决策）

当前先不修改代码，仅记录可行方案，后续跟踪官方更新。

### 方案 A：改 OpenClaw plugin（推荐，改动最小）

在 `addSessionMessage` / `commitSession` 的请求体里显式带上 `peer_id`：

```typescript
const body = {
  role,
  parts,
  peer_id: actorPeerId,  // 新增
};
```

这样 OpenViking 的 `_resolve_message_peer_id` 就能拿到值，`allowed_peer_ids` 非空，长期记忆会写入 `peers/<peerId>/memories`。

### 方案 B：改 OpenViking server

在 `openviking/server/routers/sessions.py:_resolve_message_peer_id` 中，当 `request.peer_id` 为空时，回退到 `ctx.actor_peer_id`：

```python
def _resolve_message_peer_id(request, ctx):
    if request.peer_id:
        return _sanitize_peer_id(request.peer_id)
    if ctx.actor_peer_id:
        return _sanitize_peer_id(ctx.actor_peer_id)
    if ctx.legacy_agent_id:
        return _sanitize_peer_id(ctx.legacy_agent_id)
    return None
```

这样插件无需改动，但会改变服务端语义，需要官方评估是否安全。

### 方案 C：最强隔离（需要 OpenClaw 支持 per-agent 配置）

给每个 Feishu agent 分配不同的 OpenViking `accountId`/`userId`。当前 `openclaw.json` 不支持 agent 级插件配置覆盖，因此需要 OpenClaw 侧先扩展配置模型。

## 7. 当前结论

- **读取**：`peers/<peerId>/memories` 按 peerId 严格隔离；`viking://user/<user>/memories` 是所有 peer 共享的公共空间。
- **写入**：当前 OpenClaw plugin 的长期记忆提取不会进入 `peers/<peerId>/memories`，全部落在用户公共空间。
- **配置**：`openclaw.json` 不能按 agent 配置 `peer_id`，插件 schema 里也没有这个字段。
- **官方报告**：验证了身份透传与命名空间命中，但未覆盖 session commit 的 peer 写入隔离。

## 8. 待跟踪事项

- [ ] 关注 `examples/openclaw-plugin/client.ts` 是否在 `addSessionMessage` / `commitSession` body 中加入 `peer_id`；
- [ ] 关注 `openviking/server/routers/sessions.py` 是否把 `ctx.actor_peer_id` 纳入 `_resolve_message_peer_id` 回退逻辑；
- [ ] 关注 `openclaw.plugin.json` / `config.ts` schema 是否新增 per-agent peer 映射能力；
- [ ] 关注官方 `openclaw-multi-tenant-test-report.md` 是否补充 session commit 的 peer 写入隔离用例。
