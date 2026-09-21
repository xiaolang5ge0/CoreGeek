# 《未来战争》Coding Agent 启动与策略确认流程 v1.0

> 适用场景：比赛开始时直接交给 Claude Code、OpenCode、Cursor Agent 等 Coding Agent。
>
> 核心目标：**先读规则、先定策略、再定架构、最后编码。未经用户明确确认，不进入正式业务代码实现。**

## 1. 官方资料入口

比赛官方任务书、接口文档、请求响应格式以及Demo都在当前文件夹下

优先阅读当前目录下：

```text
任务书.md
接口文档.md
request.txt
response.txt
```

当前目录中还有CoreGeek-Demo可供参考，需要提供同样地Http服务接口给裁判模型调用。

## 2. 当前阶段禁止正式编码

初始阶段固定为：

```text
PHASE 0
规则理解
+
对战策略设计
+
代码架构设计
```

在我明确回复：

```text
策略确认，可以开始编码
```

之前，禁止：

```text
直接实现完整比赛程序
批量创建业务代码文件
提前写死策略逻辑
边分析边进入正式实现
```

允许：

```text
阅读资料
规则整理
策略分析
架构设计
状态机
伪代码
风险分析
测试方案
向我提问
```

## 3. 第一步：完整理解规则

重点核对：

```text
胜负条件
积分构成
昼夜回合数
机器人刷新机制
机器人攻击逻辑
地图移动规则
切比雪夫距离
Base 尺寸
Weapon 合法建造环
Wall 合法建造环
武器数量限制
三种武器攻击机制
Rocket cooldown
升级价格
升级是否回血
Worker / Pioneer 权限
矿石采集机制
矿刷新机制
Vendor / Price
Task 机制
Task cooldown
LLM / Sandbox 异步机制
上一回合反馈字段
异常响应处罚
```

不要只读 README。

## 4. 建立 RuleAssumptions

先输出：

```text
RULE_ASSUMPTIONS.md
```

每条规则标记为：

```text
CONFIRMED
官方文档明确确认

RUNTIME_OVERRIDE
Runtime Request 实时字段，应覆盖静态规则

STRONG_INFERENCE
强推断，但不是官方明确规则

UNKNOWN
需要实战验证

CONFLICT
不同资料存在冲突
```

统一优先级：

```text
Runtime Request
>
接口文档字段语义
>
任务书静态规则
>
Request / Response 示例
>
本地默认值
>
策略推断
```

如果资料冲突，必须明确说明：

```text
冲突是什么
各来源分别如何描述
建议运行时如何处理
是否需要实战验证
```

## 5. 第二步：先分析“怎么赢”

先回答：

```text
比赛如何获胜？
什么情况算输？
生存和积分是什么关系？
Gold 的真正作用是什么？
Worker 每回合的机会成本是什么？
Pioneer 的机会成本是什么？
Wall / Weapon / Base Upgrade 谁更重要？
任务什么时候值得做？
夜间采矿是否值得？
什么时候必须牺牲经济回防？
```

输出：

```text
GAME_STRATEGY_DESIGN.md
```

## 6. 策略不要直接替用户拍板

涉及明显取舍的部分，先列方案，再向用户确认。

### 6.1 经济策略

比较：

```text
保守经济
平衡经济
激进经济
```

讨论：

```text
Day1 Stone 优先级
什么时候转 Copper / Iron
夜间 Worker 是否继续采矿
矿是否采完再走
什么时候 Sell
谁负责 Sell
是否允许打断 Active Miner
```

### 6.2 防御策略

比较：

```text
20 Wall 全封
FRONT 重防
FRONT + FLANK
Rear Service Gate
动态封门
```

分析：

```text
Stone 成本
Worker 回合成本
第一夜生存能力
后续夜间经济
机器人绕后风险
```

### 6.3 武器策略

至少比较：

```text
Gatling + Railgun + Rocket
2 Rocket + 1 Railgun
3 Rocket
```

分析：

```text
Pioneer 单人值守效率
Rocket cooldown
AOE
持续火力
Boss 压力
Front Wall 被推后的火力纵深
```

### 6.4 Worker 策略

需要确认：

```text
Worker 是否固定分工
是否使用 Goal Lock
矿是否采完再走
Sell 是否只能由空闲 Worker 执行
夜间是否继续采矿
什么情况下允许召回
```

### 6.5 Task 策略

分析：

```text
Task 收益
Task 耗时
Pioneer 离开任务点风险
Task 与 Night Defense 的冲突
是否允许 Multi Submit
Task cooldown
```

## 7. Agent 必须主动询问关键策略问题

在正式确定方案前，向我提出 8~15 个真正影响实现的问题。

不要问官方文档已经明确的问题。

推荐问题：

```text
1. 整体打法偏保守、平衡还是激进经济？
2. Day1 是否接受优先采 Stone，先建立最低第一夜防线？
3. Day1 是否不追求 20 Wall 全封，而只做 FRONT / FLANK？
4. 是否接受 Rear 保留 Service Gate？
5. 夜间是否默认 Pioneer 单人守家？
6. 两个 Worker 是否尽可能持续采矿？
7. Worker 一旦开始采矿，是否锁定当前矿直到采完？
8. Sell 是否只能分配给 ECONOMIC_FREE Worker？
9. 是否允许牺牲一定 Wall / Base HP 换更多采矿回合？
10. 武器优先测试 2R+1Q 还是 Triple Rocket？
11. Task 与采矿冲突时谁优先？
12. Treasure 是否属于当前主线？
13. PvP 是否暂时关闭？
14. Base HP 是否允许作为经济缓冲？
15. 对激进 Recall 阈值能接受多大风险？
```

核心问题确认后停止继续追问。

## 8. 输出最终策略确认表

用户回答后，输出：

```text
STRATEGY_DECISIONS.md
```

建议格式：

| 项目 | 最终策略 | 原因 | 状态 |
|---|---|---|---|
| Day1 | Bootstrap Defense | 保证 Night1 | CONFIRMED |
| Wall | FRONT + FLANK | 降低 Stone 成本 | CONFIRMED |
| Worker | Aggressive Economy | 最大化经济 | CONFIRMED |
| Mining | Mine Lock | 防止来回切换 | CONFIRMED |
| Sell | Free Worker Only | 不打断采矿 | CONFIRMED |
| Weapon | 2R+1Q / 3R Test | 单人防守 | EXPERIMENT |
| PvP | Disabled | 暂不投入 | CONFIRMED |

以后 `STRATEGY_DECISIONS.md` 作为策略真源。

## 9. 第三步：先设计架构，再写代码

策略确认后，先输出：

```text
ARCHITECTURE_DESIGN.md
```

建议模块：

```text
HTTP Server
RequestParser
ResponseBuilder

WorldModel
MapModel
MapProfile

RuleAssumptions
RuntimeRules
LegalityGuard

FeedbackProcessor
StateDiffEngine

StrategyPhaseManager
RoleScheduler

WorkerFSM
PioneerFSM

BootstrapPlanner
EconomyPlanner
DefensePlanner
UpgradePlanner
TaskPlanner

ThreatEstimator
TimeToFailureEstimator

RocketPlanner
RailgunPlanner
GatlingPlanner
JointFirePlanner

PathFinder
CollisionResolver
MoveReservation

GoalCommitment
ProgressMonitor
IdleWatchdog

Telemetry
ReplayEngine
```

每个模块说明：

```text
职责
输入
输出
依赖
状态
不能越权的边界
```

## 10. Worker 架构必须使用 FSM

禁止：

```text
每回合重新枚举所有行为
→ 选择最高评分
```

这种方式容易导致：

```text
反复换矿
左右横跳
采几次就走
Sell 抢占采矿
两个 Worker 抢同一路径
```

Worker 应采用：

```text
FSM
+
Goal Commitment
+
Task Lock
```

核心状态：

```text
ECONOMIC_FREE
↓
MINE_SELECT
↓
MINE_TRAVEL
↓
MINE_COLLECT_LOCKED
↓
MINE_DEPLETED
↓
ECONOMIC_FREE
↓
SELL / NEXT_MINE
```

只有：

```text
CRITICAL_DEFENSE
EMERGENCY_BUILD
UNSTUCK
```

可以强制抢占。

## 11. 路径规划必须提前设计

必须考虑：

```text
8方向移动
切比雪夫距离
建筑阻挡
角色阻挡
机器人阻挡
合法邻接操作格
两个 Worker 抢同一格
位置互换
Move Reservation
```

特别注意：

```text
Mine Position
Vendor Position
Weapon Position
```

通常不是角色最终站立格。

角色目标应是：

```text
合法 Adjacent Operation Cell
```

例如：

```text
矿旁边可 Collect 的格
Vendor 旁边可 Sell 的格
Weapon 旁边可 Attack 的格
```

## 12. 先设计日志和 Replay

每回合保存：

```text
request
response
decision_trace
strategy_phase
worker_state
worker_goal
worker_target
worker_path
mine_lock
TTF
ReturnCost
weapon_choice
attack_target
rocket_aoe_score
upgrade_choice
wall_priority
task_state
last_action_result
fallback_reason
```

目标：

> 任意一回合都能解释“为什么这个角色这样行动”。

同时设计：

```text
ReplayEngine
```

后续策略优化流程：

```text
先 Replay
再实战
```

## 13. 正式编码需要明确授权

只有我明确回复：

```text
策略确认，可以开始编码
```

Agent 才允许进入：

```text
PHASE 1：正式开发
```

## 14. 编码必须分阶段

### P0：协议与合法性

```text
HTTP Server
Request Parser
Response Builder
LegalityGuard
Exception Fallback
Telemetry
```

### P1：Night1 生存

```text
Map Geometry
PathFinding
Stone Mining
Weapon Build
FRONT / FLANK Build
Bootstrap Ready
```

目标：

```text
先稳定活过第一夜
```

### P2：Worker 经济闭环

```text
MINE_TRAVEL
MINE_COLLECT_LOCKED
MINE_DEPLETION
ECONOMIC_FREE
SELL
Goal Lock
Progress Monitor
```

验证：

```text
不会采一半跑路
不会 Sell 抢占 Active Miner
不会原地打转
```

### P3：单人夜防

```text
Pioneer Gunner
Rocket Planner
Railgun Planner
ThreatScore
TTF
Worker Recall
```

优先：

```text
2R+1Q
```

再测试：

```text
3R
```

### P4：中期升级

```text
Weapon Upgrade
Base Upgrade
Hot Wall Upgrade
World News
Dynamic Service Gate
```

### P5：Task

```text
Task FSM
LLM Async
Sandbox Async
Multi Submit
Task Memory
```

### P6：高级策略

```text
Treasure
MapProfile Reuse
Advanced Replay
Self Optimization
```

## 15. 每阶段完成后必须自测

流程：

```text
实现
↓
单元测试
↓
构造地图测试
↓
日志检查
↓
Replay
↓
问题分析
↓
向用户汇报
↓
进入下一阶段
```

禁止：

```text
P1 尚未验证
→ 直接进入 P4 / P5
```

## 16. 策略修改必须走证据驱动流程

不要：

```text
输一局
→ 大改全部策略
```

而应该：

```text
Observe
↓
Diagnose
↓
Hypothesis
↓
Minimal Change
↓
Test
↓
Measure
↓
KEEP / REVERT / INCONCLUSIVE
```

修改前必须回答：

```text
问题是什么？
证据是什么？
是规则错误、策略错误还是工程错误？
哪个模块负责？
能不能更小改动？
怎么测试？
怎么回滚？
会不会破坏 Day1 Bootstrap？
会不会破坏 Mine Lock？
```

## 17. Agent 第一轮具体任务

启动后严格按：

```text
STEP 1
完整阅读 GitHub 官方资料

STEP 2
输出比赛规则摘要

STEP 3
输出 RuleAssumptions / Conflict / Unknown

STEP 4
分析胜负条件与核心资源交换关系

STEP 5
给出 2~3 套总体打法

STEP 6
给出推荐初步策略

STEP 7
设计第一版代码架构

STEP 8
向我提出 8~15 个需要我拍板的策略问题

STEP 9
停止
```

最后必须明确输出：

```text
当前尚未开始正式编码。
等待你回答策略问题并确认最终方案。
```

## 18. 编码闸门

整个流程最重要的一条：

```text
IF user_has_not_confirmed_strategy:
    DO_NOT_IMPLEMENT_PRODUCTION_CODE
```

只有我明确说：

```text
策略确认，可以开始编码
```

才允许正式开发。

## 19. 给 Agent 的核心提醒

> **不要把“会写代码”当成第一目标。第一目标是先理解比赛规则和资源交换关系，第二目标是和用户确定对战策略，第三目标是设计能支持策略迭代的架构，第四目标才是编码。未经策略确认，不得把任何重大策略选择写死进代码。**

## 20. 最终流程

```text
官方规则
↓
RuleAssumptions
↓
胜负分析
↓
策略方案
↓
用户问答
↓
STRATEGY_DECISIONS
↓
ARCHITECTURE_DESIGN
↓
用户明确授权
↓
分阶段编码
↓
Telemetry
↓
Replay
↓
实战
↓
证据驱动迭代
```

## 21. 一句话版本

> **比赛开始后先让 Agent 完整阅读 `https://github.com/Tymonxiong/Competition` 的任务书、接口和示例，建立 RuleAssumptions，分析胜负和资源交换，给出多套策略并主动询问用户关键取舍；用户确认策略后再设计代码框架，只有收到“策略确认，可以开始编码”这一明确指令后才进入分阶段实现，并从 Day1 生存、Worker 经济闭环和单人夜防逐步扩展，而不是一开始就写完整比赛代码。**


