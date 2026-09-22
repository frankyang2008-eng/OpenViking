# QueueFS Worker 线程泄漏修复 —— 设计与实施计划

## Context（根因与证据，Rev 2 定稿）

**症状**：`pytest tests/server` 单进程跑到 ~33% 挂起；线程 dump 显示 2000+ 泄漏的
`_queue_worker_loop` daemon 线程 + 数千 asyncio_0 线程（run 882eb121 双 reviewer 审计 + repro 定量）。

**根因链（第一性原理：线程是进程级不可回收资源；生命周期必须配对；阻塞调用必须有界）**：

1. **主因**：`QueueManager.stop()`（`openviking/storage/queuefs/queue_manager.py:320-340`）
   对 worker `join(timeout=10)`，超时仅 warning 即 `clear()` 丢引用。
   worker 卡死点：循环内 `queue.size()/dequeue()` 经 `AsyncAGFSClient` →
   `asyncio.to_thread`（`openviking/pyagfs/async_client.py:96,103,256`）调用
   **无超时的同步 binding**，以及 `NamedQueue.dequeue` 内 handler 执行
   （`named_queue.py:308-337`，长任务无界）。卡 ≥10s → join 必超时 → 线程永续 5Hz 轮询。
2. **放大器 A**：每个 worker loop 独享默认 ThreadPoolExecutor（`asyncio.to_thread`），
   每 loop 最多 `min(32, cpu+4)` 条 asyncio_0 线程 → "数千 asyncio_0"。
3. **放大器 B**：`tests/server/conftest.py:186-200` function 级 fixture 每测试新建
   `OpenVikingService`（`core.py:171` init + `core.py:509` start，6 worker/manager），
   386 测试 × 6 ≈ 2316 ≈ dump 的 2000+。repro 实测 20 manager → 80 线程线性。
4. **结构性缺陷（兜底必要）**：`init_queue_manager()`（`queue_manager.py:52-56`）
   覆盖全局 `_instance` 不停旧实例 —— 本套件 conftest 配对 close 未触发，
   但 `core.py:542-549` close 前置步骤任一抛异常即孤儿化；且每次
   `__init__` `atexit.register(self.stop)`（`queue_manager.py:117`）从不 unregister。

**上游状态**：未修（`upstream/main` 4e3770d4 缺陷行号原样），无 issue 跟踪。
目标：本地修复 + 验证 + 提交本地分支；**暂不提 PR**（用户指令）。

## Approach（第一性原理推导的修复）

| 项 | 修复 | 第一性原理对应 |
| --- | --- | --- |
| F1' | `init_queue_manager()` 改 **stop-then-replace**：先赋新值再停旧实例（防 `_instance is self` 清空新值），init 路径 join 缩短 2s | 生命周期配对是资源管理的充分条件，不依赖调用方自觉 |
| F2' | worker 循环改 **共享单一 executor**（每 manager 一个，显式 shutdown）；dequeue/排空路径加**硬超时**（`asyncio.wait_for` 包 `queue.size()/dequeue`，排空 `gather` 加超时） | 阻塞必须有界；卡死线程必须可退出（daemon 线程无法强杀，只能让协程回到可检查 stop_event 的状态） |
| F3' | tests/server fixture teardown 加**线程数回落守卫** | 系统必须能自证不泄漏（守卫即回归测试） |

不做（YAGNI）：强杀线程（Python 无安全手段）、全局线程池重构、上游 F2 全量语义改造 —— 后续 PR 单独议。

## Files to modify

- `openviking/storage/queuefs/queue_manager.py` — F1' + F2'（init/stop/worker loop/atexit）
- `tests/storage/test_queue_manager.py` — F1'/F2' 单元测试（现有仅 2 测试，扩展）
- `tests/server/conftest.py` — F3' 守卫
- `docs/design/queuefs-worker-lifecycle-fix.md` — 设计文档（步骤 1 产出，存档）

复用：`asyncio.wait_for`（stdlib）、现有 `stop_event` 机制、`RecoverStale` at-least-once
保证（停旧实例不丢消息）、repro 脚本 `/tmp/ov-leak-review/repro.py`（改造为断言测试）。

## Steps（用户 6 项要求的映射）

- [ ] 1. 设计文档：`docs/design/queuefs-worker-lifecycle-fix.md`（根因 + 第一性原理 + F1'/F2'/F3' 细节 + 破坏面）
- [ ] 2. TDD：先写失败测试（init 覆盖不泄漏 / stop 超时不静默 / 线程数回落断言）
- [ ] 3. 实施 F1'（stop-then-replace，最小 diff）
- [ ] 4. 实施 F2'（共享 executor + wait_for 硬超时 + 排空 gather 超时 + atexit unregister）
- [ ] 5. 实施 F3'（conftest 守卫）
- [ ] 6. 多维度 review：2× reviewer subagent 对抗评审（正确性 / 破坏面 / 上游 PR 合规）
- [ ] 7. 严格验证（见 Verification）
- [ ] 8. 本地提交（不 push、不 PR）

## Verification

1. 单元：`pytest tests/storage/test_queue_manager.py -v`（新测试全绿）
2. 泄漏回归：repro 改造版 —— init 20 次 + close 失败模拟，断言 `threading.active_count()` 回落
3. 套件级：`pytest tests/server --timeout=60` **完整跑完不再挂起**（原 33% 必挂）
4. 无回归：`pytest tests/misc tests/client --no-cov -q` 失败数 ≤ 基线（32/11）
5. 上游对齐：diff 范围仅上述 4 文件；`git diff upstream/main -- openviking/storage/queuefs/` 可直接作为 PR 补丁预览

## Decisions（用户已裁决，2026-09-03）

1. **分支**：从 `ov-dev-opt` 切 `fix/queuefs-thread-leak`，全部提交落在此分支（后续上游 PR diff 干净）
2. **F2' 超时层**：仅 agfs 调用层 —— `queue.size()/dequeue()` 包 `asyncio.wait_for`；handler 不限时（与现有 Semaphore/watchdog 语义不打架）
3. **设计文档**：`docs/design/queuefs-worker-lifecycle-fix.md`（对齐仓库 RFC 惯例）
