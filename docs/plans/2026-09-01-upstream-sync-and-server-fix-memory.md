# OpenViking 双轮上游同步与 server 启动故障修复（2026-08-31 ~ 09-01）

> 本文档为 viking memory 的持久化兜底（会话内 viking MCP 连接因 server 重启失效）。
> viking 工具恢复后可将内容重新 remember。

## 一、两轮上游同步（volcengine/OpenViking main → ov-dev-opt）

### 第一轮 e8cedaeb → 3123e8d8（4 提交，merge a012e54f，零冲突）

| 提交 | 内容 |
|---|---|
| #4353 | **ragfs 统一 CacheRuntime 与 Redis 后端 CacheFS/QueueFS**：新增 `crates/ragfs/src/cache_runtime/`（provider 无关抽象 + redis 后端 + Lua 原子脚本注册表）；QueueFS 的 `redis_backend.rs`（1519 行）重写为 `cache_backend.rs`(711) + `cache_protocol.rs`(209)；**整包删除 5 个 crate**（mooncake / redis / yuanrong / yuanrong-sys / python-native）；ragfs-python lib.rs 重写 1313 行 |
| #4479 | temp-upload：小时桶共享上传 + 尽力清理 |
| #4465 | openclaw-plugin：OpenClaw 2026.8.1 durable-turn 契约 |
| #4469 | CI：release 镜像只构建一次 |

### 第二轮 3123e8d8 → 241af176（24 提交，merge 85cab5db，零冲突）

亮点：ACL 账户级授权开关 #4527、向量记录 ID 贯通文件系统 API #4442、web-studio 搜索模式/JSONL #4470、codex SessionEnd 提交钩子 #4429、memory 内联图片脱敏 #4456、rerank 显式 provider 凭证校验 #4443、**pi-extension session_start 不再阻塞启动 #4506**（已移植 omp 版，见第四节）。

### 缓存方案现状（#4353 之后）

- 生产可用 provider：**仅 `redis`**（standalone，pool_size / command_timeout_ms / default_ttl_seconds）
- `dynamic` provider：**已设计未实现**（配置返回 UnsupportedProvider）——`.so` 动态加载 + 版本化 C ABI `openviking_cache_provider_v1`，Mooncake/YuanRong 未来以此形态回归
- `MemoryMockProvider` 仅测试用
- 扩展点分层：Rust 内部 `CacheProvider` trait（pub(crate)）+ 第三方 C ABI

### 构建与版本

全量 build 通过（`OV_SKIP_STUDIO_BUILD=1`，本批零 studio 变更），`0.4.17.dev178 → dev211`。

## 二、server 启动故障（已修复）

- **症状**：同步后重启 server：`InvalidArgumentError: 1 validation error for AccountSettings — namespace: Extra inputs are not permitted` → `Application startup failed`
- **根因**：#4527 后 `AccountSettings` 模型 `extra="forbid"`，白名单仅 `agent_evolution` / `acl`；存量 `~/.openviking/data/viking/<account>/_system/setting.json` 含旧 **namespace 隔离键**（default / hermes-ov-space / macOS-OV-tools / openccode-ov-space 四账号，值全 false）。老进程一直没重启，同步后首次重启才暴露。**上游 BREAKING 变更**
- **修复**：4 个 setting.json 备份为 `.bak-20260901`，内容改写 `{}`（语义等价：全默认全关），重启恢复健康
- **教训**：上游 schema 收紧（extra=forbid）+ 本地存量数据 = 重启才炸；**每次同步后必须重启 server 并验证 /health**

## 三、测试套件结论（两轮同步验证方法与发现）

- 基线对照法：合并树 vs 纯 upstream main 跑同套件，FAILED 清单逐条 diff——两轮均证明**合并零引入**
- 第一轮：80 failed 全部上游遗留（逐条一致）；vectordb 236 全绿
- 第二轮：75 failed（**上游顺手修好 5 个**，含 2 个 rerank 配置测试）、新增 FAILED = 0、**112 ERROR 为上游新回归**：`tests/utils/mock_agfs.py:107` 读不到 `_system/setting.json`（与第二节同源，schema/fixture 问题），纯 main 复现
- **tests/server 全套存在测试隔离缺陷**：某测试经 `~/.openviking/ov.conf` 真实外呼方舟 API；重试金字塔 = 单次 600s 超时（vlm/base.py:75）× retry_async 4 次 × 凭证 failover N 倍（model_retry.py PrimaryBackupSwitcher 每 600s failback）→ 单点最坏小时级冻结；测试间状态污染决定谁掉坑（单跑 mcp_endpoint 152/153 仅 3 分钟，全套跑概率性冻死）。pytest faulthandler 会被 pytest 自身插件 cancel，需用 `python -c "faulthandler.dump_traceback_later(...)"` 包装 pytest.main 才有效

## 四、omp 插件（examples/omp-openviking-extension/）

- #4506 已移植（**d9c36ed2**）：session_start 改 fire-and-forget `void start(ctx).catch()`；`startPromise` 记忆化保证首轮 before_agent_start 等到同一 in-flight 链，profile 注入 + recall 零丢失；失败路径不变（connected=false 跳过注入）。远程部署启动 6.0s → 2.4s
- 验证：bun build ✓ / node --test 55/55 ✓ / `omp -p` 冒烟 ✓ / 已部署 `~/.omp/agent/extensions/openviking/`（deploy 本地 config.json 不覆盖）

## 五、遗留待办

1. 上游 issue（暂缓，用户决定）：存量 setting.json `namespace` BREAKING + mock_agfs 112 ERROR
2. viking memory 重新入库（本文档内容，待 viking MCP 恢复）
3. omp 遗留：README 残留 pi 引用、tools.ts `@earendil-works/pi-ai` 依赖、omp 原生 e2e
