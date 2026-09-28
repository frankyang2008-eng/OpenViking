# P2 跨树分叉实体分析报告

日期：2026-09-28 · 目标：评估是否需要合并 user 主树与 peer 树的重复/分叉实体（原 optimization-report O5）
方法：全量文件盘点 + basename 跨树匹配 + 文本相似度分类 + 同步机制核查 + 真实检索结果验证。禁猜测。

---

## 结论先行

**原方案「物理合并 peer 树文件」不成立，应取消；且检索层去重也暂不必要（YAGNI）。**

1. peer 树是记忆插件**活跃双向同步的数据源**（核查时刻 12:47 仍在写入）——手改/合并会被同步覆盖并可能造成冲突。
2. peer 进入检索 target 是**设计行为**（当前 actor peer 的 memories 被主动加入），不是泄漏。
3. 真实 6 次 prefetch 结果中**没有一次同实体重复进入 top-5**——现有排序机制天然压住了跨树重复，实际收益不显著。

保留为监控项：未来若实测出现同实体跨树副本挤占 top-5，再做检索层（非物理）去重。

## 1. 规模盘点

- user 主树 memories：4,596 md；peer 树合计：3,461 md；distinct basename：7,380
- 跨 ≥2 棵树出现的 basename：**78**（699 实例，相对"每树一份"多 492 个副本）
  - 涉及 user 树 65，纯 peer-peer 13
  - 每 basename 跨树数：2 树×65、3 树×9、4 树×2、6 树×1、36 树×1
- `.overview.md`（36 树 494 实例）、`.abstract.md` 是 **L1/L0 系统目录摘要**，非记忆实体，已排除

## 2. 相似度分类（78 basename，组内最大两两相似度）

| 类别 | 阈值 | 数量 | 含义 |
|---|---|---|---|
| dup | ≥0.85 | 11 | 近完全重复 |
| forked | 0.45–0.85 | 11 | 同实体已分叉（eza/git/superpowers/ov-dev-opt 分支 等） |
| distinct | <0.45 | 56 | 同名不同实体（不同项目/视角，不可合） |

distinct 抽查准确：如 `codebase_memory_mcp.md` 在 tools/entities/peer 是不同视角记录。

## 3. peer 同步机制（为什么不能物理合并）

- 当前 cwd（openviking repo）actor_peer_id = `github.com-frankyang2008-eng-openviking`
- 该 peer 目录文件在核查时持续更新（`peers/.../memories/events/2026/09/28/*` mtime = 12:47），由记忆插件在本机工作时双向同步
- server 检索 target 解析（`openviking/core/retrieval_targets.py`）：ContextType.MEMORY 且存在 actor_peer_id 时，默认 target = `user/memories` + `user/peers/<actor>/memories` —— **主动纳入当前 peer，属设计**
- 因此 peer 文件是受同步管理的数据源：物理删除/合并 → 下次同步恢复或冲突；且 peer 树按 workspace 隔离有其语义（跨设备/项目召回）

## 4. 真实检索是否受害（用数据验证，而非假设）

服务恢复后 6 次真实 prefetch（11:44–12:52），target 均同时含 user + 当前 peer 树：

- 30 个返回槽位中，peer 仅 1 个（3%）
- **无一次出现同 basename 实体重复进入 top-5**
- 之前 step-3 的 2 个 peer rerank 副本为单次现象，且双盲评分相关（3/3），不是噪声

→ 跨树重复在最终 top-5 被现有排序天然收敛为一份，未观测到槽位挤占，O5 假设的实际伤害不显著。

## 5. 裁决与后续

| 动作 | 结论 |
|---|---|
| 物理合并/删除 peer 文件 | **不做**（同步数据源，危险且不持久） |
| 检索层实体去重 | **暂不做**（无实测伤害，YAGNI） |
| dup/forked 清单 | 存档（`/tmp/p2_rows.json`）备查 |
| 监控触发条件 | 未来 prefetch 出现同 basename 跨树副本同时进 top-5 并挤掉相关项时，再在检索结果归并层去重（按 basename/标题保留最高分一份），不动物理数据 |

## 6. 与 P1 关系

P1（working_memory UPDATE content 规范化）是独立的真实代码缺陷修复，已完成并有回归测试；本 P2 结论不影响 P1。
