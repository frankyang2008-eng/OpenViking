# ov CLI lock 组件 Summary 恒显 unknown —— 分析与修复方案

> 状态：已修复（P3）｜日期：2026-08-08｜分支：ov-dev-opt（上游 main 同样存在）

## 一、现象

`ov status` 组件健康表中，`lock` 一行显示：

```
Component   Health     Summary
lock        unhealthy  unknown
```

- Health 列真实（由 conflicts 判定，见下）。
- Summary 列恒显 `unknown`，掩盖了 active/conflicts 计数。

## 二、根因（两个独立问题叠加）

### 2.1 Health = unhealthy：`Conflicts: 7`（真实，非活锁）

后端观察探针实测（`curl .../api/v1/observer/lock`）：

```
Active locks: 0
Waiting locks: 0
Stale locks removed: 0
Conflicts: 7
```

`openviking/service/debug_service.py:183` 判定 `has_errors = bool(conflicts) or stale > 0`，故 health=unhealthy。

但 `crates/ragfs/src/lock/metrics.rs:30` 的 `recent_conflicts` 是**最多留 32 条的滚动 ring buffer**，存的是「最近发生过的锁竞争事件」，非当前持锁。且 **active=0、waiting=0** → 当前无任何持锁/等待，不存在活锁。7 条冲突是并发操作短暂竞争（输方记一条 → stale 清理 + 重试，`crates/ragfs/src/lock/manager.rs:712` → 最终成功）的痕迹，属瞬态自愈，非故障。

**结论：lock 组件实际健康，只是把「最近 N 次正常并发竞争」误报为 unhealthy。**

### 2.2 Summary = unknown：CLI `lock_summary` 格式不匹配（真 bug，100% 触发）

后端返回纯文本多行 status（`debug_service.py:184-195`）：

```
Active locks: 0
Waiting locks: 0
Stale locks removed: 0
Conflicts: 7
```

CLI `crates/ov_cli/src/status_ui.rs:620-642` 的 `lock_summary` 却按**另一种格式**解析——用 `pipe_rows`（`:695-713`，只保留以 `|` 开头的行）找 `TOTAL (N)` 行：

- status 非 None → 跳过首分支；
- 不含 `"No active locks"`（全仓该串仅 `status_ui.rs:624` 一处，后端从不输出）；
- `pipe_rows(纯文本)` → 空 Vec → 找不到 TOTAL 行 → 返回 `"unknown"`。

**关键实锤**：全仓没有任何生产者输出 `TOTAL (N)` lock 表，该格式只存在于 `status_ui.rs:806-817` 的**单元测试夹具**中。即 `lock_summary` 是**对着虚构夹具写的，与真实后端格式脱节**，故必然显示 unknown。

**结论：CLI 摘要解析器与后端返回格式不匹配 → unknown。后端无问题。**

## 三、其他组件核查（无同类问题）

queue / vikingdb / retrieval / filesystem 的后端 observer 输出格式与各自 CLI 解析器吻合，均正常。`component_line` 的 `_ => "unknown"` 兜底分支（`status_ui.rs:441`）永不触发（组件列表硬编码于 `:123-130`）。

## 四、上游状态核查（2026-08-08）

自查 `volcengine/OpenViking` 官方 upstream/main（最新 `7b8b33e8`）：

- `debug_service.py` lock property：与本地**逐字相同**，纯文本格式；历史仅有 pathlock Rust 重构（`2f945123`/`6b538db5`/`1841dfed`），无改动 status 格式。
- `status_ui.rs` `lock_summary`：与本地**逐字相同**；最后改动为 `fd098cfd`（#3379 结构化 API 错误），与锁格式无关。
- 无任何 commit 提及 lock 格式 / TOTAL / 锁摘要显示。

**结论：上游原样带过来的既有 bug，官方未修复。本地修复不会与上游冲突；但也不会被上游自动采纳。**

## 五、严重级别：P3（中）

| 维度 | 评估 |
|---|---|
| 影响 | 仅 lock 行 Summary 显示异常，掩盖计数；Health 列由 `is_healthy` 独立判定（`status_ui.rs:486-495`）不受影响；verbose/JSON 路径可见真实数据 |
| 触发 | 100% 触发 |
| 修复成本 | 极低（单函数回退） |

## 六、修复方案

### 首选（CLI 侧，推荐）

改 `crates/ov_cli/src/status_ui.rs` `lock_summary`（`:620-642`）：TOTAL 分支失败后加回退——按行精确解析 `"Active locks: N"` 与 `"Conflicts: M"`（用 `strip_prefix` + `parse_u64`，避免 `parse_u64` 吞非数字字符导致取错），命中则返回 `"{n} active lock(s), {c} conflict(s)"`；仍失败则回退显示原始 status 首行而非 "unknown"。同步把 `:806-817` 测试夹具改为后端真实纯文本格式，并新增一条纯文本 lock status 的回归测试。

改动面：仅 `lock_summary` 单函数，不影响其他五组件解析器；保留 `"No active locks"` 分支无害。

### 备选（后端侧，不推荐）

改 `debug_service.py:184-195` 让 status 输出 `TOTAL (N)` 管道表。不推荐：该字符串还被 `observer.py:76` 直出、`SystemStatus.__str__`（`:50-59`）拼接、`metrics/datasources/observer_state.py:50` 引用，改动面更大、向后端其他消费者扩散。

## 七、相关文件与行号

| 位置 | 说明 |
|---|---|
| `crates/ov_cli/src/status_ui.rs:620-642` | `lock_summary`，TOTAL 格式解析器（bug 所在） |
| `crates/ov_cli/src/status_ui.rs:695-713` | `pipe_rows`，只认 `\|` 开头行 |
| `crates/ov_cli/src/status_ui.rs:806-817` | 虚构 TOTAL 夹具（唯一来源） |
| `crates/ov_cli/src/status_ui.rs:441` | `_ => "unknown"` 兜底分支 |
| `openviking/service/debug_service.py:166-195` | lock observer，纯文本 status 输出 |
| `crates/ragfs/src/lock/metrics.rs:30,35-41` | `recent_conflicts` ring buffer |
| `crates/ragfs/src/lock/manager.rs:712` | 冲突 → stale 清理 → 重试 |
| `openviking/server/routers/observer.py:75` | `/api/v1/observer/lock` |

## 八、待办（已完成）

- [x] 确认修复方向：本地 ov-dev-opt 直接修
- [x] 按首选方案实现 CLI 回退 + 修夹具 + 回归测试

### 实施记录（2026-08-08）

- **`lock_summary`**（`status_ui.rs`）：TOTAL 分支失败后新增纯文本回退——按行严格解析 `"Active locks: N"` 与 `"Conflicts: M"`（`strip_prefix` + 取首个空白 token + `parse::<u64>()`，避免 `parse_u64` 吞非数字字符导致取错），四臂 `match`：
  - 双命中 → `"{a} active {lock(s)}, {c} {conflict(s)}"`
  - 仅 active → `"{a} active {lock(s)}"`；仅 conflicts → `"{c} {conflict(s)}"`
  - 都无 → 回退显示 status 首非空行，空则 `unknown`
  - 保留 `TOTAL` 分支与 `"No active locks"` 分支（低成本保险，降低未来上游 merge 冲突）
- **新增 `lock_count` 助手**：严格按 token 解析，`"Active locks: 0 (2 waiting)"` 得 0 而非 02。
- **新增回归测试** `lock_summary_parses_plain_text_observer_status`：覆盖双命中/单 active/单冲突/非数字回退/None/空/TOTAL 旧格式。
- **健康渲染测试** `healthy_payload_renders_selected_status_sections` 补 lock 断言（`lock healthy 1 active lock`），消除「bug 不可见」根因。
- **顺带修复既有测试编译坏**：upstream #3708（OIDC/LDAP）给 `Config` 加 `auth_mode`/`ldap_username`/`ldap_password`/`oidc_token` 字段但未更新 `main.rs` 3 处测试初始化 → 补 `None`。既有问题，非 lock bug 范围，但阻塞测试目标编译。
- **验证**：`cargo test -p ov_cli` 454 通过（含新测试）；clippy 未安装跳过。