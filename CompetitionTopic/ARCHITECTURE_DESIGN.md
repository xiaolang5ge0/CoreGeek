# 《未来战争》ARCHITECTURE_DESIGN v1.0

> 设计目标：**分层清晰、可维护、快速迭代**。策略真源见 STRATEGY_DECISIONS.md；规则真源见 RULE_ASSUMPTIONS.md。
> 本文档只定架构与边界，不含正式业务实现。

## 1. 分层总览

```
┌─────────────────────────────────────────────────────────┐
│ L0 接入层   server / serializer / ExceptionFallback      │  只搬数据，零游戏逻辑
├─────────────────────────────────────────────────────────┤
│ L1 协议层   protocol (DTO/指令构造/常量/运行时规则表)      │  只懂报文，不懂策略
├─────────────────────────────────────────────────────────┤
│ L2 世界模型层 world / feedback / rules / buildable_map    │  只维护"事实"，不做决策
├─────────────────────────────────────────────────────────┤
│ L3 策略层   phases / planners/* / threat / ttf            │  只产出"目标"，不生成指令
├─────────────────────────────────────────────────────────┤
│ L4 执行层   fsm_worker / fsm_pioneer / fire / path        │  把目标变成合法指令
├─────────────────────────────────────────────────────────┤
│ L5 基础设施 telemetry / replay                            │  横切：全层可调用，不反向依赖
└─────────────────────────────────────────────────────────┘
依赖方向只允许自上而下；L5 被所有层依赖但不依赖任何层。
```

## 2. 目录结构（开发根 = `game/CoreGeek/`，Python ≥3.11，仅标准库）

> 部署契约：**主办方扫描 `main3.py` 加载**，入口文件名必须与 Demo 一致；整体打成 tar.gz 上传（详见 §7）。

```
game/CoreGeek/
├── main3.py                    # 入口：解析端口 argv[1]、初始化、启动服务（平台扫描此文件）
├── run.sh                      # 兜底启动脚本（接口文档样例：bash run.sh port）
├── src/agent/
│   ├── server.py               # L0 HTTP 服务 + Exception Fallback
│   ├── protocol.py             # L1 Pos/Unit/Robot/Turn DTO、指令构造器、常量表
│   ├── rules.py                # L2 RuntimeRules（运行时字段覆盖静态表）+ LegalityGuard
│   ├── world.py                # L2 WorldModel / MapModel（矿、商店、任务点、敌情）
│   ├── buildable_map.py        # L2 可建造区学习（试探+反馈）
│   ├── feedback.py             # L2 FeedbackProcessor / StateDiffEngine
│   ├── phases.py               # L3 StrategyPhaseManager
│   ├── planners/
│   │   ├── layout.py           # L3 BaseLayoutPlanner（动态FRONT，墙/炮台/控制点候选格）
│   │   ├── economy.py          # L3 EconomyPlanner（选矿、卖货时机、金币预算）
│   │   ├── defense.py          # L3 DefensePlanner（修墙、升级、应急）
│   │   ├── task.py             # L3 TaskPlanner（接任务、返程预算、LLM/沙盒异步）
│   │   └── upgrade.py          # L3 UpgradePlanner（武器>墙>基地）
│   ├── threat.py               # L3 ThreatEstimator / TimeToFailureEstimator
│   ├── fsm_worker.py           # L4 WorkerFSM + GoalCommitment + TaskLock
│   ├── fsm_pioneer.py          # L4 PioneerFSM（TASK / GUARD）
│   ├── fire.py                 # L4 JointFirePlanner（3火箭轮转 + AOE落点评分）
│   ├── path.py                 # L4 PathFinder(A*) + MoveReservation + CollisionResolver
│   ├── brain.py                # 编排器：每回合串起 L2→L3→L4→ResponseBuilder
│   ├── telemetry.py            # L5 结构化日志（JSONL，每回合 request/response/trace）
│   └── replay.py               # L5 ReplayEngine（读 JSONL 复盘）
├── tests/                      # 单元测试 + 构造地图测试（不打包）
├── tools/build_package.py      # 一键打包脚本 → dist/CoreGeek.tar.gz
└── logs/                       # 本地遥测输出（不打包）
```

## 3. 每回合数据流

```
Request JSON
  → RequestParser → Turn DTO
  → FeedbackProcessor：应用 lastRoundRoleActionResults / errors / lastCmdResult / llmResp
      · 更新 BuildableMap（建造合法格学习）
      · 更新矿点耗尽、任务状态、冷却、LLM额度
  → WorldModel 刷新（昼夜判定、敌我态势、机器人列表）
  → StrategyPhaseManager：BOOTSTRAP(Day1) / ECONOMY / MIDGAME 阶段判定
  → Planners 产出目标集：layout缺口 / 经济目标 / 防御告警(TTF) / 任务计划 / 升级计划
  → FSM 层把目标落成各角色意图（受 GoalLock 约束；CRITICAL_DEFENSE 可抢占）
  → JointFirePlanner（仅夜）：轮转选冷却就绪火箭 → AOE落点评分 → attack 指令
  → PathFinder + MoveReservation：解双工人抢格/互换冲突
  → LegalityGuard 逐条校验（动作-角色-昼夜-距离-预算）
  → ResponseBuilder：roleCommandMap (+prompt / executeCmd)
  → Telemetry：落盘本回合 request + response + decision_trace
```

## 4. 模块契约（职责 / 输入 / 输出 / 依赖 / 状态 / 边界）

### L0 server
- 职责：监听端口、收 POST、返回 JSON；任何异常回落 `{"roleCommandMap":{}}`；5秒响应红线内的快速失败。
- 输入：HTTP 请求体 │ 输出：HTTP 响应体 │ 依赖：brain │ 状态：无
- 边界：**禁止**出现任何游戏规则判断；禁止阻塞调用。

### L1 protocol
- 职责：报文 ↔ DTO 双向转换；指令构造器（move/attack/build/...）；常量表（昼夜回合、造价、ID规则）。
- 输入：原始 JSON │ 输出：Turn/Pos/Unit/Robot、指令 dict │ 依赖：无 │ 状态：无
- 边界：不做合法性推断；字段缺失时给安全默认但不擅自修正语义。

### L2 rules（RuntimeRules + LegalityGuard）
- 职责：静态规则表 + **运行时覆盖**（attackRange、cooldown、vendorShopList、weaponShopList 以 Request 为准）；逐条指令合法性校验。
- 输入：Turn + 候选指令 │ 输出：合法/非法+原因 │ 依赖：protocol │ 状态：规则表（只读）
- 边界：校验不通过只退回不修改；不感知策略。

### L2 world / buildable_map / feedback
- 职责：维护"当前世界是什么"：地图元素、矿点存量跟踪、敌建筑、机器人分布、可建造格集合（CONFIRMED格/未知格/非法格三态）；消费上回合反馈做状态diff。
- 输入：Turn + lastRound* 字段 │ 输出：WorldModel 只读视图 │ 依赖：protocol │ 状态：BuildableMap、矿点采集计数、MapProfile
- 边界：**只记录事实**，不产出任何行动建议；建造区只做学习不做猜测性断言（未知格标记 UNKNOWN 交给策略层试探）。

### L3 phases
- 职责：判定当前策略阶段（BOOTSTRAP → ECONOMY → MIDGAME），决定各 Planner 的优先级与预算。
- 输入：WorldModel │ 输出：阶段 + 各Planner预算/使能 │ 依赖：world │ 状态：当前阶段
- 边界：不直接生成指令；阶段迁移条件必须可解释并写入 trace。

### L3 planners
| Planner | 职责 | 关键输出 | 边界 |
|---|---|---|---|
| layout | 生成动态FRONT的基地布局：墙格(≈12)、3炮台格、控制点、通道；FRONT默认朝敌/图心，Night1后按遥测修正 | 建造候选格列表（含备选） | 候选格必须经 BuildableMap 过滤；不指派工人 |
| economy | 选矿（单位回合收益=矿价/(路程+采集)）、矿耗尽重选、卖货时机（顺路/包满/急用金） | 工人经济目标 | 不打断 MINE_COLLECT_LOCKED；不指挥建造 |
| defense | 墙/炮台缺口修复、TTF告警时回防与抢修、应急炸弹/眩晕评估 | 防御目标+紧急度 | 仅TTF告警可请求抢占工人 |
| upgrade | 按 武器>墙>基地 排队用券（升级=回血） | 购买/使用升级券计划 | 预算由 phases 下发，不擅自超支 |
| task | Day1起接任务；LLM/沙盒异步；**黄昏返程预算**（离开任务点=任务结束）；提交答案 | 开拓者任务意图+prompt/executeCmd | 夜间必须让位 GUARD；宝藏推断只用任务期免费LLM |

### L3 threat / ttf
- 职责：估算本夜机器人对墙/炮台/基地的伤害速率，计算预计破防回合（TTF）；输出召回阈值信号。
- 输入：WorldModel（机器人、血条、火力） │ 输出：TTF、ThreatScore │ 依赖：world │ 状态：历史波次统计
- 边界：只评估不行动；阈值参数来自 STRATEGY_DECISIONS #12。

### L4 fsm_worker
- 状态机：`ECONOMIC_FREE → MINE_SELECT → MINE_TRAVEL → MINE_COLLECT_LOCKED → MINE_DEPLETED → (SELL_TRAVEL→SELL | NEXT_MINE)`；旁路：`BUILD_ASSIGNMENT / REPAIR / CRITICAL_DEFENSE / EMERGENCY_BUILD / UNSTUCK`。
- 规则：Goal Lock——进入 MINE_COLLECT_LOCKED 后仅允许 ① 矿耗尽（zones中矿点消失/计数满10）② 三类抢占 打断；卖矿只在 FREE 态评估（顺路优先）。
- 输入：角色状态+目标 │ 输出：单角色指令意图 │ 依赖：planners、path │ 状态：每工人 FSM 实例（持久化于 brain 跨回合）
- 边界：禁止每回合全量重评分换目标（防左右横跳）；抢占必须留 trace（fallback_reason）。

### L4 fsm_pioneer
- 白天：`TASK_TRAVEL → TASK_ACCEPT → TASK_WORK(LLM/沙盒异步) → TASK_SUBMIT / TASK_ABORT_RETURN(黄昏)`；夜间：`GUARD`（站控制点）。
- 边界：GUARD 优先级夜间最高；返程时间预算 = 当前格→控制点路径回合 + 安全余量，到期强制 ABORT_RETURN。

### L4 fire（JointFirePlanner）
- 职责：每夜回合：① 从 cooldown==0 的火箭中选一座（轮转保证三门均匀使用）② 候选落点评分 = Σ(命中机器人权重×伤害)（大怪/BOSS加权，集群加分）③ 生成 attack（controllerId=开拓者）。
- 输入：WorldModel + 火箭冷却 │ 输出：attack 指令或空 │ 依赖：rules │ 状态：轮转游标
- 边界：**落点回避己方单位溅射范围**（友好伤害未明，安全默认，见 RULE_ASSUMPTIONS U10）；无价值目标宁可空仓不浪费冷却。

### L4 path
- 职责：8方向 A*（切比雪夫启发）；目标格解析为"合法邻接操作格"（矿旁可采集格/小贩旁可卖格/炮台控制点）；MoveReservation 解决双工人同格竞争与位置互换。
- 输入：起点+目标+障碍图 │ 输出：下一步格 │ 依赖：world │ 状态：本回合预留表
- 边界：只给下一步，不缓存全程（障碍每回合变）；找不到路返回 None 交 FSM 走 UNSTUCK。

### L5 telemetry / replay
- 职责：每回合 JSONL 落盘：完整 request、完整 response、decision_trace（phase、各角色 FSM 状态/目标/路径、mine_lock、TTF、weapon_choice、attack_target、aoe_score、build/upgrade 决策、last_action_result、fallback_reason）；ReplayEngine 离线重放任意回合区间。
- 输入：全层 trace 钩子 │ 输出：日志文件 + 回放报告 │ 依赖：无（被全层调用） │ 状态：文件句柄
- 边界：日志写入失败不得影响主流程（try-catch 静默降级）；不落敏感外部信息。

## 5. 关键设计点对应用户策略

| 用户策略 | 架构落点 |
|---|---|
| 一人控3炮轮转 | fsm_pioneer.GUARD 固定站控制点 + fire.py 轮转游标（attack 按武器ID下发，controllerId=开拓者） |
| 动态FRONT环形布局 | planners/layout.py：FRONT 为参数（默认朝敌/图心，Night1后遥测修正），坐标一律由布局器生成，代码中零硬编码坐标 |
| 矿采完自主辨别 | feedback 跟踪矿点采集计数+zones消失事件 → MINE_DEPLETED |
| 机会性卖货 | economy.py 仅对 ECONOMIC_FREE 工人评估，成本函数含矿↔小贩↔下一矿相对位置 |
| 结构化打印每回合Req/Resp | telemetry.py：JSONL 全量落盘 + stdout 摘要 |
| 分层清晰/快速迭代 | L0~L5 单向依赖；策略参数集中于 STRATEGY_DECISIONS 对应的配置区，改策略不改结构 |

## 6. 分阶段编码计划（授权后执行）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| P0 | server / protocol / rules / telemetry / ExceptionFallback / 单测 | 任意畸形输入返回合法空响应；日志完整 |
| P1 | Night1 生存：world、layout（动态FRONT）、path、Day1 双工人(石+经济矿)、3火箭+12墙建造 | 构造地图测试：Night1 前 3炮+墙就位 |
| P2 | 工人经济闭环：WorkerFSM 全状态、Mine Lock、耗尽识别、机会性卖货 | 不采半跑路、不卖矿抢占、不原地打转 |
| P3 | 单人夜防：GUARD 控制点、3火箭轮转、AOE评分、TTF、工人召回 | 构造夜潮回放：持续火力无空转、TTF告警可触发回防 |
| P4 | 中期：upgrade 排队（武器>墙>基地）、修墙、FRONT遥测修正 | 升级即回血生效；布局按观测修正 |
| P5 | 任务：PioneerFSM 任务流、LLM/沙盒异步、黄昏返程预算、提交答案 | Day1 可接任务且 Night1 前归队 |
| P6 | 宝藏推断（后期主线）、Replay 驱动调参、3R vs 2R+1Q 实测 | 证据驱动报告 |

每阶段按 实现→单测→构造地图测试→日志检查→Replay→汇报 的流程闭环后才进入下一阶段。

## 7. 部署与打包

| 约束 | 决策 | 依据 |
|---|---|---|
| 入口 | `main3.py`（与 Demo 同名），`python main3.py <port>`，监听 `0.0.0.0:port` | 用户明确：主办方扫描 main3.py 加载；接口文档：port 由系统传入 |
| 语言/版本 | Python ≥ 3.11 | Demo `pyproject.toml` requires-python=">=3.11" |
| 依赖 | **仅用标准库**（http.server/json/logging/heapq/dataclasses…），零第三方包 | Demo dependencies 为空；对战沙盒无法保证 pip 环境 |
| 代码位置 | 开发统一在 `game/CoreGeek/` 下 | 用户明确 |
| 打包产物 | `dist/CoreGeek.tar.gz`，**tar 根层直接含 `main3.py`、`run.sh`、`src/`**（平台扫描得到入口） | 用户明确：平台上传 tar.gz 直接加载 |
| 打包排除 | `tests/`、`logs/`、`dist/`、`__pycache__`、`*.pyc`、`.idea/` | 减小体积、避免无关文件 |
| 打包方式 | `python tools/build_package.py` 一键产出，P0 阶段先行验证打包-解包-启动链路 | 防止临赛打包翻车 |
| 日志 | 遥测 JSONL 写相对路径 `logs/`，写失败静默降级 | 平台只读文件系统风险兜底 |
