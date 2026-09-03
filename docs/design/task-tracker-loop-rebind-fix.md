# TaskTracker OwnerLoop 重绑修复设计

状态：已批准（用户指令 2026-09-03：设计文档 → 实施计划 → review → 验证 → 本地提交）
分支：`fix/task-tracker-loop-binding`（自 `fix/queuefs-thread-leak` @ 7446d463 切出，堆叠 —— 验证依赖套件越过 queuefs 挂点）
上游：缺陷在上游 main 原样存在；fable 2026-08-06 对抗审查已 flag（LOW"建议启动时显式 bind"）

## 1. 问题

`tests/server/test_http_client_sdk.py`（套件 ~70%，queuefs 修复后首个新暴露挂点）：

```
running_server fixture 起同进程 uvicorn server
  └─ app.py:353 start_cleanup_loop → task_tracker.py:241 bind_current_loop()
      → RuntimeError: owner event loop is already bound (task_tracker_concurrency.py:76)
          → "Application startup failed" → server 没起来
  └─ fixture 健康轮询耗尽后静默跌落（conftest.py:325 区域）→ 失败形态劣化为错误/挂起
```

基线 `cefbea47`（无 queuefs 改动）同现 → 上游缺陷。

## 2. 根因（第一性原理：绑定是"owner 死亡后可转移"的租约，不是永久焊死）

1. **单例 tracker + 一次性绑定**：`TaskTracker` 进程级单例（`get_task_tracker`），
   `OwnerLoopDispatcher._owner_loop` 首次绑定后永久拒绝重绑（:75-76 raise）。
2. **每个 `service.initialize()` 都重新 bind**：`attach_work_index`
   （task_tracker.py:206，由 core.py:501 调用）无条件 `bind_current_loop()`。
   同进程第二个 service（新 loop）= 必炸。
3. **生产正常路径受时序保护**（收敛声明）：lifespan 内 `core.py:500-501`
   `prepare_task_tracking` 在队列 worker 启动（core.py:509）**之前**在主 loop
   完成 bind → worker 首次 dispatch 走跨 loop 路径，不会抢绑。
   真实风险面 = **同进程多个"准主 loop"**：测试（pytest-asyncio 每函数一 loop）、
   嵌入式脚本多次 initialize。此时前 owner loop 已 `close()`，但绑定仍拒绝。

## 3. 修复设计

### F1' bind = 所有权转移（task_tracker_concurrency.py:70-83）

```python
def bind_current_loop(self) -> asyncio.AbstractEventLoop:
    """Bind the calling loop as the dispatcher owner (ownership transfer)."""
    current_loop = asyncio.get_running_loop()
    with self._bind_lock:
        self._owner_loop = current_loop
        return self._owner_loop
```

- **实施中修订（两轮）**：第一版仅"closed owner 可重绑"，但实测
  test_http_client_sdk 仍挂 —— pytest-asyncio 的 fixture loop 在 fixture 期间
  **存活**，closed-check 覆盖不到。语义升级：**显式 bind 即所有权声明**，
  无条件转移；旧 owner 上在途任务由 future 自持引用照常完成，新派发走新 owner。
- `run()` 不变：跨 loop 派发仍对所有人可用（这是它的职责）；
  worker loop 不会经由 run() 抢 owner（仅 owner=None 时惰性绑定，而
  prod 正常启动时 prepare_task_tracking 先在主 loop bind）。
- **为何安全**：raise 的原意是防误派发，但 bind 的调用点全部是
  "声明自己为 owner"（attach_work_index / start_cleanup_loop），不是误用；
  误用防护由 run() 的 owner.is_running/closed 检查承担。

### F2' running_server 轮询耗尽必须显式失败（tests/server/conftest.py）

```python
for _ in range(50):
    ...health poll...
else:
    raise RuntimeError(f"SDK server did not become ready on 127.0.0.1:{port}")
```

- 轮询本有界（50×），但耗尽后跌落 → 错误推迟到 api_key_manager 段，
  报错与根因脱节。显式 raise + 端口号，失败 30s 内定位。
- 不改轮询时长（YAGNI）。

### 不做（YAGNI / 已排除）

- dispatcher 全局重构 / per-service tracker（单例是上游架构决策）
- worker-loop 抢绑防护（run() 保持 fail-fast，无新增窗口）
- `bind_current_loop` 改为永不 raise（掩盖真实跨 loop 误用）

## 4. 破坏面

| 风险 | 判定 |
|---|---|
| 旧 owner 上在途任务 | future 自持引用，在旧 loop 照常完成；新派发走新 owner —— 转移而非丢弃 |
| worker 抢绑 | run() 仅在 owner=None 时惰性绑定；prod 启动序 prepare_task_tracking 先绑主 loop |
| conftest raise 改变现有"静默跌落"路径 | 该路径本就注定失败（api_key_manager 段必炸），只是提前 + 带根因 |
| StoreIOLimiter/KeyedAsyncLockPool 原语 loop 绑定（评审 Important） | 转移后另一 loop 首次 store_io acquire **响亮报错**（非静默污染）；修复前同场景直接启动崩溃 —— 严格改善。已知上限，升级路径 = 按 running loop 分实例（仓库先例 vikingbot openviking_hooks.py:36-44） |
| test_http_client_sdk 10 个 TypeError | .venv SDK wheel 缺 add_resource(reason=) —— 测试与 wheel 版本漂移，环境遗留（重建 binding 家族），与本修复无关 |

## 5. 验证标准

1. 新单元测试：所有权转移（open loop / closed loop）+ 同 loop 幂等（先 RED 后 GREEN）
2. `pytest tests/storage tests/unit tests/service -q` 无回归
3. **`pytest tests/server/test_http_client_sdk.py` 隔离 55s 跑完不再挂死**（修复前无限挂）—— 核心验收 ✓（14 passed + 10 个环境遗留 TypeError）
4. `pytest tests/server` 全量 ≤ 40 failed + 53 errors 基线，0 超时（本欢**不再 exclude** test_http_client_sdk）
5. diff 仅 4 文件：task_tracker_concurrency.py、tests/server/conftest.py、tests/service/test_owner_loop_dispatcher.py + 本文档

## 6. 上游 PR 备注

补丁独立、<20 行 + 测试；与 queuefs 修复（7446d463）可拆分提交。
