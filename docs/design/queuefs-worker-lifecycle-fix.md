# QueueFS Worker 线程生命周期修复设计

状态：已批准（plans/queuefs-thread-leak-fix.md，2026-09-03）
分支：`fix/queuefs-thread-leak`（基线 ov-dev-opt @ cefbea47）
上游：`volcengine/OpenViking` main 4e3770d4 缺陷原样存在，无 issue 跟踪

## 1. 问题

`pytest tests/server` 单进程跑到 ~33% 挂起。pytest-timeout 线程 dump（2026-09-03）：

- `Thread-2036 (_queue_worker_loop)` —— 2000+ 个泄漏 daemon 线程
- 数千 `asyncio_0` 线程（concurrent.futures worker）
- 基线旧提交（3cddc8ed，补齐原生 .so 后）同样挂起 → 非同步引入，历史隐性债

## 2. 根因（Rev 2，双 reviewer 审计 + 定量复现）

第一性原理三条：
1. **线程是进程级不可回收资源** —— 只能"不创建"，不能"强杀"。泄漏 = 生命周期未配对。
2. **阻塞调用必须有界** —— 无界 await 使 stop_event 失效，优雅退出不可能。
3. **系统必须能自证不泄漏** —— 无守卫的测试基建会把泄漏放大 1000 倍后才暴露。

### 2.1 主因：stop() join 例行超时（queue_manager.py:320-340）

worker 循环（`_queue_worker_loop`，:210-247）内 `queue.size()/dequeue()` 经
`AsyncAGFSClient` → `asyncio.to_thread`（`pyagfs/async_client.py:96,103,256`）调用
**无超时的同步 binding**；`NamedQueue.dequeue`（`named_queue.py:308-337`）内还内嵌
handler 执行（embedding/VLM，分钟级）。卡 ≥10s 时：

```
stop_event.set() → 协程仍停在不可中断 await → join(timeout=10) 超时
→ 仅 logger.warning 即 _queue_threads.clear() → daemon 线程永续 5Hz 轮询
```

并发路径排空的最终 `await asyncio.gather(*active_tasks)`（cancel 之后，:315 附近）
**无超时** —— `to_thread` 任务 cancel 不中断底层线程，此 gather 可永久挂死，
是 join 超时的另一来源。

证据：mock agfs 下 stop 0.3s 全回收（/tmp/ov-leak-review/repro.py）；真实套件 2000+。

### 2.2 放大器 A：每 worker 独享默认 executor

`asyncio.to_thread` 用各自 loop 的默认 executor，每 loop 最多
`min(32, cpu+4)` 条 asyncio_0 线程 → dump 中"数千 asyncio_0"。

### 2.3 放大器 B：测试基建（D3，量化实锤）

`tests/server/conftest.py:186-200` function 级 fixture 每测试新建
`OpenVikingService`（core.py:171 init + core.py:509 start，6 worker/manager）。
386 测试 × 6 ≈ 2316 ≈ dump 的 2000+。repro：20 manager → 80 线程，线性增长。

### 2.4 结构缺陷：init 覆盖不停止（D1，兜底必要）

`init_queue_manager()`（queue_manager.py:52-56）覆盖全局 `_instance` 不停旧实例。
本套件 conftest 配对 close（conftest.py:199 → core.py:549）未触发，但：
- `core.py:542-549` close 前置步骤（resource/watch/scheduler stop）任一抛异常，
  queue manager 永不 stop → 下次 init 覆盖 → 孤儿化
- 每次 `__init__` `atexit.register(self.stop)`（queue_manager.py:117）从不 unregister

## 3. 修复设计

### F1' init stop-then-replace（queue_manager.py:36-94）

```python
_init_lock = threading.Lock()

def init_queue_manager(...) -> "QueueManager":
    with _init_lock:                      # 防并发 init 误杀新生儿实例（评审 P2-1）
        global _instance
        previous = _instance
        _instance = QueueManager(...)     # 先赋新值（旧 stop() 的 `_instance is self` 判断不会清空新值）
        if previous is not None and previous is not _instance:
            try:
                previous.stop(join_timeout=2.0)   # 无条件调用：未启动实例 stop 本身是 no-op 但仍配对 atexit（评审 P2-4）
            except Exception:
                logger.warning(..., exc_info=True)
    return _instance
```

- 兼容性：全仓 grep 确认无调用方依赖"先建 A 后建 B 且 A 存活"语义；
  `get_queue_manager()` 全部消费者（session.py、resource_service、content_write、
  semantic_processor、tests conftest）均语义为"当前活跃实例"。
- 数据安全：stop() 有 5s drain + cancel（:307-315）；未 ack 消息由
  RecoverStale 下次启动恢复（named_queue.py:271,316）→ at-least-once，无丢失。
- `stop()` 增加 `join_timeout: float = 10.0` 参数（默认值不变，向后兼容）。

### F2' worker 有界化（queue_manager.py）

1. **共享 executor**：`__init__` 创建 `self._executor = ThreadPoolExecutor(
   thread_name_prefix="qfs-agfs")`；worker loop `loop.set_default_executor(self._executor)`
   → 每 manager 的 to_thread 线程数收敛到一个池（cap 线程爆炸）；`stop()` 中
   `shutdown(wait=False)`。
2. **agfs 调用硬超时**：新增 `self._agfs_call_timeout = 30.0`；
   单消费者路径 `queue.size()` 与并发路径 `queue.size()/dequeue_raw()`
   包 `asyncio.wait_for(timeout=...)`，TimeoutError → 记日志 + `stop_event.wait(poll_interval)`
   → 循环回到 stop_event 检查点 → 线程可退出。
   **明确不限时**：单消费者路径的 `queue.dequeue()`（内嵌 handler，分钟级任务）——
   超时会导致 RecoverStale 重试风暴，与 Semaphore/watchdog 语义冲突。此处为
   ponytail 已知上限：handler 自身卡死仍会阻塞该 worker 线程，升级路径是
   handler 层超时配置化。
3. **排空最终 gather 有界**：cancel 后的 `asyncio.gather` 包
   `asyncio.wait_for(..., timeout=2.0)`（cancel 不中断 to_thread 底层线程，
   无界 gather = join 超时的隐藏来源）。
4. **异常可见性**（评审 P2-2）：并发路径 size/dequeue_raw 的非超时异常
   `logger.warning`（原先静默吞掉）；TimeoutError 静默 break（预期退避）。
5. **atexit 配对**：`stop()` 中 `atexit.unregister(self.stop)`；
   未启动实例的 stop() 也执行 unregister + executor shutdown（提前到 `_started` 检查之前）。

### F3' 测试守卫（tests/server/conftest.py）

`service` fixture teardown：`await svc.close()` 后断言
`threading.active_count() <= 进入时 + 6`（一个 manager 的 worker 配额）。
守卫即回归测试：任何再次引入的泄漏在单测试内即时爆炸，而非 386 测试后。

## 4. 破坏面清单（评审判定）

| 风险 | 判定 | 缓解 |
|---|---|---|
| stop-then-replace 杀掉在途任务 | at-least-once，RecoverStale 兜底，无丢失 | — |
| 双 worker 竞争窗口（旧卡死新已起） | 仅 prod 同目录热重载理论可达；tests 每 service 独立 temp_dir | init join 2s + warning 日志 |
| handler 限时的重试风暴 | 不做 handler 限时（设计排除） | ponytail 注释标记上限 |
| executor shutdown 与解释器退出竞争 | shutdown(wait=False) 不阻塞；行为不劣于现状 | — |
| conftest 守卫误报（legit 慢回收） | 阈值 +8 = 6 worker 配额 + 2 格 executor 退出余量 | 评审 P2-5 建议采纳 |
| dequeue_raw 超时致消息滞留 processing | 仅 agfs 调用 >30s 才可触发；RecoverStale 重启恢复；不劣于基线（修复前该调用同样不返回） | 已知上限，升级路径 = in-flight 恢复（评审 P2-3） |
| stop() 最坏 6×join_timeout 串行等待 | 默认 60s（init 路径 12s）；卡死线程本无法更快回收 | Known limitation，PR 正文标注（评审 Minor-6） |

## 5. 验证标准

1. `pytest tests/storage/test_queue_manager.py -v` 全绿（含新增 TDD 测试）
2. 泄漏回归：init 20 次 + close 失败模拟，`threading.active_count()` 回落
3. 套件级：`pytest tests/server --timeout=60` 完整跑完不再挂起（原 33% 必挂）
4. 无回归：`tests/misc` ≤32 failed、`tests/client` ≤11 failed（合并基线）
5. `git diff upstream/main -- openviking/storage/queuefs/` 可直接作为上游 PR 补丁

## 6. 上游 PR 备注（暂缓，用户指令）

补丁独立于本地其他改动；PR 需附：线程 dump、repro 数据、本文档根因链。
