"""编排器：P1 —— Day1 Bootstrap（3火箭+FRONT/FLANK墙）+ 单人夜防 + 夜间采矿。

每回合数据流：
Turn 解析 → 反馈学习(BuildableMap) → 布局(动态FRONT) →
白天：建造分配 + WorkerFSM + 开拓者归位 / 黑夜：开拓者控炮 + 工人采矿 →
LegalityGuard 过滤 → 响应
"""
from __future__ import annotations

from typing import Any

from .buildable_map import BuildableMap
from .fire import JointFirePlanner
from .fsm_pioneer import STATE_TASK_WORK, PioneerFSM
from .fsm_worker import ROLE_MINER, ROLE_REPAIRER, STONE_BATCH, WorkerFSM
from .path import next_step
from .phases import PhaseManager
from .planners.layout import BaseLayout, choose_front, compute_layout
from .planners.news import NewsEconomy
from .planners.task import TaskPlanner, TaskSession
from .planners.upgrade import WALL_MAX_HP, UpgradePlanner
from .protocol import (
    Pos,
    ROCKET,
    ROUNDS_PER_DAY,
    Turn,
    WALL,
    WEAPON_BUILD_COST,
    build_response,
    distance,
    empty_response,
    move_command,
    parse_targets,
    station_footprint,
    use_command,
)
from .rules import LegalityGuard
from .threat import SAFE, ThreatEstimator
from .wall_registry import WallRegistry

NIGHT_SAFE_DIST = 3         # 夜间矿/小贩安全半径（机器人攻击射程3；主攻基地不绕路杀工人）
SPAWN_AVOID_DIST = 4        # 历史出生点直接避让半径
CORRIDOR_WIDTH = 5          # 机器人行军走廊半宽（出生点→我方基地连线附近）——B 方案安全矿定义
CORRIDOR_BASE_MARGIN = 8    # 走廊只算距基地 > 此值 的部分（近基地处有墙/炮塔保护，不算危险）
BASE_DANGER_DIST = 4        # 机器人逼近基地此距离内 → 基地有危险（工人撤内圈而非迎面避让）
WORKER_DANGER_DIST = 3      # 工人规避半径（≈机器人攻击射程）
BUILD_TRAVEL_BUFFER = 6     # 建墙预留回程缓冲（预留回合 = 待建墙数 + 缓冲）
DUSK_AVOID_WINDOW = 12      # 白天临近入夜此回合数内，提前避开出生走廊矿/小贩
COST_PER_WALL_BASE = 2      # 每墙基础回合（采集1+建造1）
WALL_ROUNDS_PER = 5         # 单工人每墙约需回合（采集+建造+挪位）；用于判断是否需第二工人帮建
REPAIR_MARGIN = 4           # 修理工提前归位余量（距天黑 ≤ 路径 + 4）
WEAPON_L1_COST = 100        # 武器 L1→L2 券价（判断是否需要凑武器升级费）
RESERVE_GOLD = 30           # 升级预算保留金
LLM_DAILY_LIMIT = 3         # 每日 LLM 调用上限（用户：每天只有 3 次）
WALL_DANGER_RATIO = 0.8     # 城墙危险阈值：预计伤害 > 城墙总HP × 此值 → 危险
DAY1_RUSH_DEADLINE = 50     # Day1 双工人建墙冲刺截止回合（预留收尾）
DAY1_WALL_TARGET = 12       # Day1 目标墙数
PIONEER_FLEE_DIST = 1       # 机器人贴到 CP 才撤离（过早撤离=整夜哑火，实战权衡）


class _Ctx:
    """单回合执行上下文：移动预留 + 决策痕迹。"""

    def __init__(self, trace: dict[str, Any]):
        self.trace = trace
        self.reserved: set = set()
        self.fsms: dict[int, WorkerFSM] = {}
        self.threat_level: str = SAFE
        self.home_anchor = None
        self.mine_blacklist: dict = {}
        self._recall_cells: dict[int, Any] = {}
        self.trace.setdefault("workers", {})

    def recall_cell(self, unit_id: int):
        return self._recall_cells.get(unit_id)

    def is_mine_blocked(self, pos, round_no: int) -> bool:
        """矿黑名单（新闻封矿/采集失败/采集死循环）：解封回合前回避。"""
        release = self.mine_blacklist.get(pos)
        return release is not None and round_no < release

    def block_mine(self, pos, release_round: int) -> None:
        self.mine_blacklist[pos] = release_round

    # 以下由 brain 每回合注入
    danger_workers: set = set()
    safe_anchor = None
    repair_worker: int | None = None
    share_mines: bool = False
    robot_cells: tuple = ()
    spawn_cells: tuple = ()   # 历史夜间出生点（全局固定，首夜起累积）
    corridor_cells: tuple = ()  # 出生点→我方基地 的行军走廊采样格（B 方案）
    base_in_danger: bool = False  # 机器人逼近基地 → 工人撤内圈；否则直接避让
    dusk_avoid: bool = False  # 夜间 或 白天临近入夜 → 提前避开出生走廊
    walls_left: int = 0       # 剩余待建墙数（石料岗按此收敛采集量，防过量）
    repair_triggered: bool = False   # 修理工夜间/临近天黑归位触发
    wall_danger: bool = False        # 城墙危险（问题4：预计伤害超阈值）
    night_now: bool = False          # 当前是否夜间（影响 mine_unsafe）
    need_weapon_gold: bool = False   # 有武器未 L2 且金不足 → 挖矿工去卖钱
    gunner_upgrade = None            # 炮手武器升级计划 (Pos, "weapon")
    price_boost_map: dict = {}       # 新闻预测：矿种 → 售卖加权
    wall_registry = None             # L2 围墙状态表（跨回合，供修理工按需修复/升级）

    def price_boost(self, kind: str) -> float:
        return float(self.price_boost_map.get(kind, 0.0))

    def mine_unsafe(self, pos) -> bool:
        """位置是否危险：活机器人近旁（昼夜都算）；走廊/出生点仅**白天黄昏窗口**算（夜间只看活机器人）。"""
        from .protocol import distance as _d
        if any(_d(pos, rc) <= NIGHT_SAFE_DIST for rc in self.robot_cells):
            return True
        if getattr(self, "night_now", False):
            return False  # 夜间：走廊是白天归位用的，夜里只看活机器人（问题1）
        if any(_d(pos, sc) <= SPAWN_AVOID_DIST for sc in self.spawn_cells):
            return True
        return any(_d(pos, cc) <= CORRIDOR_WIDTH for cc in self.corridor_cells)

    def note(self, unit_id: int, msg: str) -> None:
        self.trace["workers"].setdefault(str(unit_id), {}).setdefault("notes", []).append(msg)

    def note_pioneer(self, msg: str) -> None:
        self.trace.setdefault("pioneer", {}).setdefault("notes", []).append(msg)

    def reserve_from(self, cmd: dict[str, Any] | None) -> None:
        if cmd and cmd.get("action") == "move":
            targets = parse_targets(cmd.get("targetPos"))
            if targets:
                self.reserved.add(targets[0])

    def other_mine_locks(self, unit_id: int) -> set:
        return {
            fsm.mine
            for uid, fsm in self.fsms.items()
            if uid != unit_id and fsm.mine is not None
        }


class Brain:
    def __init__(self) -> None:
        self.buildable = BuildableMap()
        self.fire = JointFirePlanner()
        self.phases = PhaseManager()
        self.threat = ThreatEstimator()
        self.upgrades = UpgradePlanner()
        self.pioneer_fsm = PioneerFSM()
        self.task_planner = TaskPlanner()
        self.task_session = TaskSession()
        self.mine_blacklist: dict = {}
        self.robot_spawn_log: list = []
        self.news_log: list = []  # 每回合 worldNews 存档（宝藏推断用）
        self.news_economy = NewsEconomy()
        self.wall_registry = WallRegistry()
        self.llm_day: int = 0
        self.llm_calls_today: int = 0
        self.worker_fsms: dict[int, WorkerFSM] = {}
        self.layout: BaseLayout | None = None
        self.front: str | None = None
        self.last_commands: dict[int, dict[str, Any]] = {}
        self.last_turn: Turn | None = None

    # ---- 主入口 ----
    def decide(self, payload: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        trace: dict[str, Any] = {"code_phase": "P1"}
        if not isinstance(payload, dict):
            trace["fatal"] = "payload_not_dict"
            return empty_response(), trace
        try:
            turn = Turn.load(payload)
        except Exception as exc:
            trace["fatal"] = f"turn_load:{exc!r}"
            return empty_response(), trace

        self._learn(turn, trace)
        if turn.station() is not None:
            if self.layout is None:
                self.front = choose_front(turn)
                self.layout = compute_layout(turn, self.front, self.buildable)
                if self.layout is not None:
                    trace["layout"] = {
                        "front": self.front,
                        "turrets": [p.dump() for p in self.layout.turret_cells],
                        "cp": self.layout.control_point.dump(),
                        "walls": len(self.layout.wall_cells),
                    }
            # 围墙状态表刷新（识别攻破/补建，供修理工按需修复/升级）
            if self.layout is not None:
                self.wall_registry.sync(turn, self.layout)
                rebuilt = [
                    s for s in self.wall_registry.recently_rebuilt()
                    if turn.round_no - s.rebuilt_round <= ROUNDS_PER_DAY
                ]
                if rebuilt:
                    trace["walls_rebuilt"] = [s.pos.dump() for s in rebuilt]

        commands: dict[int, dict[str, Any]] = {}
        ctx = _Ctx(trace)
        ctx.fsms = self.worker_fsms
        ctx.wall_registry = self.wall_registry
        ctx.reserved = {u.pos for u in turn.controllable()}
        ctx.mine_blacklist = self.mine_blacklist
        # 历史出生走廊（首夜起累积，全局固定）→ 夜间选矿/卖货避让
        ctx.spawn_cells = tuple(
            Pos(x, y) for entry in self.robot_spawn_log for (x, y) in entry["spawns"]
        )
        # B 方案安全矿定义：出生点→我方基地的行军走廊
        ctx.corridor_cells = self._corridor_cells(turn)
        # 白天临近入夜也提前避开出生走廊（用户要求：接近晚上时避开历史出生位置）
        ctx.dusk_avoid = turn.is_night or (0 < turn.rounds_until_night <= DUSK_AVOID_WINDOW)
        ctx.night_now = turn.is_night
        self._record_news(turn, trace)
        ctx.price_boost_map = self.news_economy.boosts(turn.day_index)
        ctx.prompt = ""
        ctx.execute_cmd = ""
        if turn.is_day:
            self._day(turn, commands, ctx)
        else:
            self._night(turn, commands, ctx)

        commands = LegalityGuard(turn).filter_commands(commands, trace)
        self.last_commands = dict(commands)
        self.last_turn = turn
        trace.update(day=turn.day_index, is_day=turn.is_day, gold=turn.gold)
        return build_response(commands, prompt=ctx.prompt, execute_cmd=ctx.execute_cmd), trace

    # ---- 反馈学习 ----
    def _learn(self, turn: Turn, trace: dict[str, Any]) -> None:
        relearn = False
        for role_id, ok in turn.last_action_results.items():
            cmd = self.last_commands.get(role_id)
            if not cmd:
                continue
            action = cmd.get("action")
            targets = parse_targets(cmd.get("targetPos"))
            if action == "build" and targets:
                self.buildable.record(targets[0], str(cmd.get("name") or ""), ok)
                if not ok:
                    relearn = True
                    trace.setdefault("build_illegal", []).append(
                        {"pos": targets[0].dump(), "name": cmd.get("name")}
                    )
            elif action == "collect" and not ok and targets:
                # 采集失败（新闻封矿等）→ 拉黑 10 回合，再犯加重
                pos = targets[0]
                previous = self.mine_blacklist.get(pos, 0)
                penalty = 10 if previous <= turn.round_no else 40
                self.mine_blacklist[pos] = turn.round_no + penalty
                trace.setdefault("mine_blocked", []).append(pos.dump())
        if relearn and self.layout is not None and turn.station() is not None:
            self.layout = compute_layout(turn, self.front, self.buildable)
            trace["layout_recomputed"] = True
        # 矿点消失则移出黑名单
        gone = [pos for pos in self.mine_blacklist if pos not in turn.mines()]
        for pos in gone:
            del self.mine_blacklist[pos]

    # ---- 白天 ----
    def _day(self, turn: Turn, commands: dict[int, dict[str, Any]], ctx: _Ctx) -> None:
        workers = turn.workers()
        layout = self.layout
        if layout is None:
            for worker in workers:
                cmd = self._worker_fsm(worker).decide(turn, worker, ctx)
                if cmd:
                    commands[worker.unit_id] = cmd
                ctx.reserve_from(cmd)
            return

        existing_weapons = {w.pos for w in turn.weapons()}
        existing_walls = {w.pos for w in turn.walls()}
        occupied = turn.occupied_cells()
        turrets_missing = [
            c for c in layout.turret_cells if c not in existing_weapons and c not in occupied
        ]
        walls_missing = [
            c for c in layout.wall_cells if c not in existing_walls and c not in occupied
        ]
        phase = self.phases.phase(turn, not (turrets_missing or walls_missing))
        ctx.trace.update(
            phase=phase, turrets_missing=len(turrets_missing), walls_missing=len(walls_missing)
        )
        ctx.walls_missing = bool(walls_missing)
        ctx.need_gold = bool(turrets_missing) and turn.gold < WEAPON_BUILD_COST

        # 岗位：Day1——**1 号工人专职石料+建墙，2 号工人采矿+卖钱**（用户指定）。
        # 仅当"按进度一个工人建不完 14 墙"时，2 号才临时帮建（用户：可两个一起建）。
        walls_left = len(walls_missing)
        ctx.walls_left = walls_left
        stone_mines = turn.mines("stone")
        # 单工人建墙产能：每墙约 WALL_ROUNDS_PER 回合（采集+建造+挪位）；不共享矿 → 各采各的更快
        capacity = max(0.0, turn.rounds_until_night - BUILD_TRAVEL_BUFFER) / WALL_ROUNDS_PER
        stone_finishable = walls_left <= capacity
        day1_helper = (
            turn.day_index == 1
            and walls_missing
            and not stone_finishable
            and turn.round_in_day < DAY1_RUSH_DEADLINE
            and bool(stone_mines)
        )
        ctx.share_mines = False  # 不共享矿：双工人各选各的（共享=互相抢同一矿，更慢）
        # 角色固化：worker[0]=修理工，worker[1]=挖矿工（按 ID 升序）
        sorted_workers = sorted(workers, key=lambda w: w.unit_id)
        for index, worker in enumerate(sorted_workers):
            fsm = self._worker_fsm(worker)
            fsm.role = ROLE_REPAIRER if index == 0 else ROLE_MINER
        # 修理工触发（夜间/临近天黑归位）：D1-D2 不触发；D3+ 距天黑≤路径+4 触发；夜间有威胁触发
        ctx.repair_triggered = self._repair_triggered(turn, sorted_workers)
        # 需要凑武器升级费（挖矿工去卖钱的信号）
        ctx.need_weapon_gold = self._need_weapon_gold(turn)

        # 建造分配：武器优先于墙；已被认领的格/工人不重复分配
        assigned = {
            fsm.build[0] for fsm in self.worker_fsms.values() if fsm.build is not None
        }
        busy = set()
        gold_left = turn.gold
        for cell in turrets_missing:
            if gold_left < WEAPON_BUILD_COST:
                break
            if cell in assigned:
                gold_left -= WEAPON_BUILD_COST
                continue
            free = [w for w in workers if w.unit_id not in busy]
            if not free:
                break
            worker = min(free, key=lambda w: distance(w.pos, cell))
            self._worker_fsm(worker).build = (cell, ROCKET)
            busy.add(worker.unit_id)
            gold_left -= WEAPON_BUILD_COST
        walls_left = len(walls_missing)
        # 预留回合：入夜前剩余 ≤ 待建墙数 + 回程缓冲 → 必须立刻开建
        urgent = 0 < turn.rounds_until_night <= walls_left + BUILD_TRAVEL_BUFFER
        for worker in workers:
            if worker.unit_id in busy:
                continue
            fsm = self._worker_fsm(worker)
            if fsm.build is not None:
                continue
            stones = worker.backpack.count("stone")
            # 只有修理工负责建墙（若修理工阵亡，则由其余工人接手）；
            # 挖矿工仅在"第一天石头>20 且 墙<14"时紧急建墙
            repairer_alive = any(
                self._worker_fsm(w).role == ROLE_REPAIRER for w in workers
            )
            if fsm.role != ROLE_REPAIRER and repairer_alive:
                if not (turn.day_index == 1 and stones > 20 and walls_missing):
                    continue
            # 批量建造：采够一批进入 build_phase 后连续建完该批（不采一个建一个）；
            # 批量随剩余墙数收敛（只剩 2 墙就采 2 块，不采满 6，防过量采集）
            batch = min(STONE_BATCH, max(1, walls_left))
            if stones >= batch:
                fsm.build_phase = True
            if stones == 0:
                fsm.build_phase = False
            can_build = stones >= 1 and (fsm.build_phase or urgent or stones >= walls_left)
            if not walls_missing or not can_build:
                continue
            # 按布局优先级派单（正面迎敌侧优先），不按离工人远近
            cell = next(
                (c for c in layout.wall_cells if c in walls_missing and c not in assigned),
                None,
            )
            if cell is not None:
                fsm.build = (cell, WALL)
                assigned.add(cell)
                busy.add(worker.unit_id)

        # 升级任务分配（仅墙/备货 → 只派修理工；武器升级由炮手 pioneer 处理）
        # 白天只**采购**（allow_stock=True）；实际升级/修复留到夜间（用户：白天最大化采集）
        self._assign_repair_mission(turn, layout, ctx, workers, allow_stock=True)
        # 炮手武器升级计划（由 fsm_pioneer 执行）
        ctx.gunner_upgrade = self._gunner_upgrade_plan(turn)

        for worker in workers:
            cmd = self._worker_fsm(worker).decide(turn, worker, ctx)
            if cmd is None:
                cmd = self._vacate_layout_cell(turn, worker, ctx)
            if cmd:
                commands[worker.unit_id] = cmd
            ctx.reserve_from(cmd)

        pioneer = turn.pioneer()
        if pioneer is not None:
            ctx.home_anchor = layout.control_point
            prev_state = self.pioneer_fsm.state
            cmd = self.pioneer_fsm.day_cmd(turn, pioneer, layout.control_point, ctx)
            if cmd:
                commands[pioneer.unit_id] = cmd
                ctx.reserve_from(cmd)
            # 新任务确认 → 重置求解会话（同文本任务重复接取也必须全新开始）
            if self.pioneer_fsm.state == STATE_TASK_WORK and prev_state != STATE_TASK_WORK:
                self.task_session.reset()
            # 任务求解：仅驻留任务点且任务进行中（submit 不覆盖走位指令）
            if self.pioneer_fsm.state == STATE_TASK_WORK and turn.phase_task:
                out = self.task_planner.work(turn, self.task_session)
                # 规则（接口文档 errorCode=5）：自进化任务执行期间 LLM 不限次且不占每日额度
                if out.prompt:
                    ctx.prompt = out.prompt
                    ctx.trace["llm_task"] = True
                ctx.execute_cmd = out.execute_cmd
                if out.submit is not None and pioneer.unit_id not in commands:
                    commands[pioneer.unit_id] = out.submit
                    ctx.trace.setdefault("task", {})["submit"] = True
        self._maybe_medicine(turn, commands)

    # ---- 黑夜 ----
    def _night(self, turn: Turn, commands: dict[int, dict[str, Any]], ctx: _Ctx) -> None:
        ctx.trace["phase"] = "NIGHT_DEFENSE"
        self._observe_spawns(turn, ctx)
        report = self.threat.evaluate(turn)
        ctx.threat_level = report.level
        ctx.trace["threat"] = report.dump()
        if self.layout is not None:
            ctx.home_anchor = self.layout.control_point
        # 机器人危险圈 + 内圈安全锚点（实战教训：夜采/夜卖被兵潮打死）
        ctx.robot_cells = tuple(r.pos for r in turn.robots if r.alive)
        interior = self._interior_cells(turn)
        ctx.safe_anchor = interior[0] if interior else ctx.home_anchor
        ctx.danger_workers = {
            w.unit_id for w in turn.workers()
            if any(distance(w.pos, rc) <= WORKER_DANGER_DIST for rc in ctx.robot_cells)
        }
        # 基地是否有危险（机器人逼近基地）→ 决定工人是"撤内圈"还是"直接避让"
        station = turn.station()
        ctx.base_in_danger = bool(
            station is not None and any(
                distance(r.pos, cell) <= BASE_DANGER_DIST
                for r in turn.robots if r.alive
                for cell in station_footprint(station.pos)
            )
        )
        # 城墙危险阈值（问题4）：预计本夜机器人总伤害 > 城墙总HP×阈值 → 危险
        ctx.wall_danger = self._wall_danger(turn)
        # 修理工触发（问题2 修复：夜间也要计算，此前只在 _day 算 → 夜里恒为 False）
        ctx.repair_triggered = self._repair_triggered(
            turn, sorted(turn.workers(), key=lambda w: w.unit_id)
        )
        ctx.trace["wall_danger"] = ctx.wall_danger
        # 修墙岗仅限：Day4+（BOSS 夜）或 墙严重受损（≥3 面掉血 或 有墙<50%）。
        # Night1-3 相对轻松（事实：机器人主攻基地、顺路才杀工人）→ 不设岗，双工人全力采矿。
        hurt_walls = [
            w for w in turn.walls()
            if w.health < WALL_MAX_HP[min(max(w.level, 1), 3) - 1]
        ]
        severe = len(hurt_walls) >= 3 or any(
            w.health < 0.5 * WALL_MAX_HP[min(max(w.level, 1), 3) - 1] for w in hurt_walls
        )
        if turn.day_index >= 4 or severe:
            workers_sorted = sorted(turn.workers(), key=lambda w: w.unit_id)
            if workers_sorted:
                ctx.repair_worker = workers_sorted[0].unit_id
        # 仅对处于危险圈的工人分配召回格（不再群体召回）
        if ctx.danger_workers:
            danger_list = [w for w in turn.workers() if w.unit_id in ctx.danger_workers]
            ctx._recall_cells = {
                w.unit_id: cell for w, cell in zip(danger_list, interior)
            }
        if self.layout is not None:
            existing_walls = {w.pos for w in turn.walls()}
            existing_weapons = {w.pos for w in turn.weapons()}
            ctx.walls_missing = any(
                c not in existing_walls for c in self.layout.wall_cells
            )
            ctx.need_gold = (
                any(c not in existing_weapons for c in self.layout.turret_cells)
                and turn.gold < WEAPON_BUILD_COST
            )
        else:
            ctx.walls_missing = False
            ctx.need_gold = False
        # 夜间升级任务分配：修理工用白天采购的券升级/修复墙（升级=回血，省修复包）
        self._assign_repair_mission(
            turn, self.layout, ctx, sorted(turn.workers(), key=lambda w: w.unit_id),
            allow_stock=False,
        )
        pioneer = turn.pioneer()
        if pioneer is not None and self.layout is not None:
            cp = self.layout.control_point
            robots_alive = [r for r in turn.robots if r.alive]
            # 兵潮已清 + 天亮前有余量 → 开拓者出行动（任务/买券），不再蹲守
            if not robots_alive and turn.rounds_until_dawn > 30:
                prev = self.pioneer_fsm.state
                cmd = self.pioneer_fsm.day_cmd(turn, pioneer, cp, ctx)
                if cmd:
                    commands[pioneer.unit_id] = cmd
                    ctx.reserve_from(cmd)
                if self.pioneer_fsm.state == STATE_TASK_WORK and prev != STATE_TASK_WORK:
                    self.task_session.reset()
                if self.pioneer_fsm.state == STATE_TASK_WORK and turn.phase_task:
                    out = self.task_planner.work(turn, self.task_session)
                    # 任务执行期间 LLM 不限次且不占每日额度（接口文档 errorCode=5）
                    if out.prompt:
                        ctx.prompt = out.prompt
                        ctx.trace["llm_task"] = True
                    ctx.execute_cmd = out.execute_cmd
                    if out.submit is not None and pioneer.unit_id not in commands:
                        commands[pioneer.unit_id] = out.submit
                if pioneer.unit_id in commands:
                    ctx.reserve_from(commands[pioneer.unit_id])
            else:
                cp_danger = any(
                    distance(rc, cp) <= PIONEER_FLEE_DIST for rc in ctx.robot_cells
                )
                goal = ctx.safe_anchor if (cp_danger and ctx.safe_anchor) else cp
                cmd = self.pioneer_fsm.move_to_guard(turn, pioneer, goal, ctx)
                if cmd:
                    commands[pioneer.unit_id] = cmd
                    ctx.reserve_from(cmd)
        # 操控占用动作（已确认）：移动中不能控炮 → 仅当开拓者无指令时才开火
        if pioneer is not None and pioneer.unit_id not in commands:
            self.fire.plan(turn, pioneer, commands, ctx.trace)
        for worker in turn.workers():
            cmd = self._worker_fsm(worker).decide(turn, worker, ctx)
            if cmd:
                commands[worker.unit_id] = cmd
            ctx.reserve_from(cmd)
        self._maybe_medicine(turn, commands)

    def _corridor_cells(self, turn: Turn) -> tuple:
        """出生点质心 → 我方基地 的行军走廊采样格（B 方案安全矿定义）。

        只取**远离我方基地**的部分（距基地 > CORRIDOR_BASE_MARGIN）——近基地处有墙/炮塔保护，
        否则基地附近的矿会被误判为"走廊内"而无矿可采。
        """
        if not self.robot_spawn_log or turn.station() is None:
            return ()
        spawns = [Pos(x, y) for entry in self.robot_spawn_log for (x, y) in entry["spawns"]]
        if not spawns:
            return ()
        sx = round(sum(p.x for p in spawns) / len(spawns))
        sy = round(sum(p.y for p in spawns) / len(spawns))
        base = turn.station().pos
        bx, by = base.x, base.y
        steps = max(abs(bx - sx), abs(by - sy))
        cells = []
        for i in range(steps + 1):
            px = round(sx + (bx - sx) * i / max(1, steps))
            py = round(sy + (by - sy) * i / max(1, steps))
            if distance(Pos(px, py), base) > CORRIDOR_BASE_MARGIN:
                cells.append(Pos(px, py))
        return tuple(cells)

    def _wall_danger(self, turn: Turn) -> bool:
        """城墙危险阈值（问题4）：预计本夜机器人总伤害 > 城墙总HP×WALL_DANGER_RATIO → 危险。
        预计伤害 = 机器人总攻击力 × 清场回合数（清场回合 = 机器人总HP / 我方DPS，封顶 60）。"""
        robots = [r for r in turn.robots if r.alive]
        if not robots:
            return False
        from .threat import ROBOT_ATK
        total_atk = sum(ROBOT_ATK.get(r.kind, 5) for r in robots)
        total_hp = sum(r.health for r in robots)
        dps = sum(20 * max(1, w.level) for w in turn.weapons()) / 3.0
        rounds = min(total_hp / dps, 60.0) if dps > 0 else 60.0
        damage = total_atk * rounds
        wall_hp = sum(w.health for w in turn.walls())
        return damage > wall_hp * WALL_DANGER_RATIO

    def _repair_triggered(self, turn: Turn, workers) -> bool:
        """修理工触发：D1-D2 不触发；D3+ 距天黑≤路径+4 触发；夜间有威胁/城墙危险触发。"""
        if turn.day_index <= 2:
            return False
        if turn.is_night:
            station = turn.station()
            near = bool(
                station is not None and any(
                    distance(r.pos, cell) <= 6
                    for r in turn.robots if r.alive
                    for cell in station_footprint(station.pos)
                )
            )
            hurt = any(
                w.health < WALL_MAX_HP[min(max(w.level, 1), 3) - 1] for w in turn.walls()
            )
            return near or hurt or self._wall_danger(turn)
        repairer = next(
            (w for w in workers if self._worker_fsm(w).role == ROLE_REPAIRER), None
        )
        home = self.layout.control_point if self.layout else None
        if repairer is None or home is None:
            return False
        return 0 < turn.rounds_until_night <= distance(repairer.pos, home) + REPAIR_MARGIN

    def _assign_repair_mission(self, turn: Turn, layout, ctx, workers, *, allow_stock: bool) -> None:
        """把 1 个墙升级/备货任务派给修理工（每回合最多 1 个）。

        - allow_stock=True（白天）：可派 WallFixer 备货任务（白天采购）。
        - allow_stock=False（夜间）：只派墙升级任务（夜间执行，升级=回血）。
        """
        repair_worker = next(
            (w for w in workers if self._worker_fsm(w).role == ROLE_REPAIRER), None
        )
        if repair_worker is None or layout is None:
            return
        rfsm = self._worker_fsm(repair_worker)
        if rfsm.upgrade is not None or rfsm.build is not None:
            return
        taken_targets = {
            fsm.upgrade[0] for fsm in self.worker_fsms.values()
            if fsm.upgrade and hasattr(fsm.upgrade[0], "dump")
        }
        for mission in self.upgrades.plan(
            turn, cp=layout.control_point, registry=self.wall_registry
        ):
            if mission.kind not in ("wall", "stock"):
                continue  # 武器/基地升级不派给工人
            if mission.kind == "stock" and not allow_stock:
                continue  # 夜间不采购
            if mission.target is not None and mission.target in taken_targets:
                continue
            if mission.kind == "stock":
                rfsm.upgrade = (mission.voucher, "stock", mission.qty)
            else:
                rfsm.upgrade = (mission.target, mission.kind)
                taken_targets.add(mission.target)
            ctx.trace.setdefault("upgrade_assigned", []).append(
                {"worker": repair_worker.unit_id, "kind": mission.kind,
                 "target": mission.target.dump() if mission.target is not None else None,
                 "voucher": mission.voucher}
            )
            break  # 每回合最多 1 个

    def _llm_budget_ok(self, turn: Turn) -> bool:
        """每日 LLM 上限 3 次（跨天重置）。

        注意：**仅用于非任务期 LLM**（如宝藏推断）。自进化任务执行期间 LLM 不限次且
        不占额度（接口文档 errorCode=5），任务求解不调用本函数。
        """
        if turn.day_index != self.llm_day:
            self.llm_day = turn.day_index
            self.llm_calls_today = 0
        return self.llm_calls_today < LLM_DAILY_LIMIT

    def _need_weapon_gold(self, turn: Turn) -> bool:
        """有武器未到 L2 且金币不足 → 挖矿工去卖钱凑升级费。"""
        weapons = turn.weapons()
        if not weapons or all(w.level >= 2 for w in weapons):
            return False
        return turn.gold < WEAPON_L1_COST + RESERVE_GOLD

    def _gunner_upgrade_plan(self, turn: Turn):
        """炮手的武器升级计划 → (目标Pos, "weapon") 或 None。优先 L1→L2，再 L2→L3。"""
        weapons = sorted(turn.weapons(), key=lambda w: (w.level, w.unit_id))
        for w in weapons:
            if w.level == 1:
                return (w.pos, "weapon")
        for w in weapons:
            if w.level == 2:
                return (w.pos, "weapon")
        return None

    def _record_news(self, turn: Turn, trace: dict[str, Any]) -> None:
        """每回合存档 worldNews（官方消息+民间传闻），并喂给新闻经济模块。"""
        official = (turn.official_news or "").strip()
        folk = (turn.folk_legends or "").strip()
        if official:
            self.news_economy.update(official, turn.day_index)
        if not official and not folk:
            return
        entry = {"round": turn.round_no, "day": turn.day_index,
                 "official": official, "folk": folk}
        # 去重：与上一条完全相同则跳过
        if self.news_log and self.news_log[-1]["official"] == official and self.news_log[-1]["folk"] == folk:
            return
        self.news_log.append(entry)
        trace["news"] = {"official": official[:80], "folk": folk[:80]}

    def _vacate_layout_cell(self, turn: Turn, worker, ctx: _Ctx) -> dict[str, Any] | None:
        """空闲工人站在炮台/控制点上时主动让位（否则该格永远无法建造/归位）。"""
        if self.layout is None:
            return None
        hot = set(self.layout.turret_cells) | {self.layout.control_point}
        if worker.pos not in hot:
            return None
        blocked = turn.blocked(worker)
        cands = [
            pos for pos in worker.pos.neighbours()
            if pos not in hot and turn.land(pos) and pos not in blocked and pos not in ctx.reserved
        ]
        if not cands:
            return None
        station = turn.station()
        anchor = station.pos if station is not None else worker.pos
        cands.sort(key=lambda p: (distance(p, anchor), p.x, p.y))
        ctx.note(worker.unit_id, "vacate_layout_cell")
        return move_command(cands[0])

    # ---- 机器人出生点遥测（FRONT 修正证据，RULE_ASSUMPTIONS U2）----
    def _observe_spawns(self, turn: Turn, ctx: _Ctx) -> None:
        if turn.round_in_day != 70 or not turn.robots:  # 每夜第1回合
            return
        station = turn.station()
        if station is None:
            return
        spawns = [(r.pos.x, r.pos.y) for r in turn.robots]
        self.robot_spawn_log.append({"day": turn.day_index, "spawns": spawns})
        # 主来向：相对基地的 dominant axis（累计所有夜晚）；墙环应朝向来向（开口=其反向）
        sx = sum(x for day in self.robot_spawn_log for x, _ in day["spawns"])
        sy = sum(y for day in self.robot_spawn_log for _, y in day["spawns"])
        n = sum(len(day["spawns"]) for day in self.robot_spawn_log)
        cx, cy = station.pos.x + 0.5, station.pos.y - 0.5
        dx, dy = sx / n - cx, sy / n - cy
        observed = ("E" if dx > 0 else "W") if abs(dx) >= abs(dy) else ("N" if dy > 0 else "S")
        opposite = {"W": "E", "E": "W", "N": "S", "S": "N"}
        ctx.trace["robot_approach"] = observed
        walls_facing = opposite[self.front] if self.front else None
        if walls_facing is not None and observed != walls_facing:
            ctx.trace["front_recommendation"] = opposite[observed]  # 重建走证据驱动变更

    # ---- 召回与自救 ----
    def _interior_cells(self, turn: Turn) -> list:
        """基地邻域内圈格（离前线 CP 最远优先）——召回站位/安全锚点。"""
        station = turn.station()
        if station is None or self.layout is None:
            return []
        base = set(station_footprint(station.pos))
        occupied = turn.occupied_cells()
        cp = self.layout.control_point
        cands: list = []
        for cell in base:
            for pos in cell.neighbours():
                if (
                    turn.land(pos)
                    and pos not in base
                    and pos not in occupied
                    and pos not in cands
                ):
                    cands.append(pos)
        cands.sort(key=lambda p: (-distance(p, cp), p.x, p.y))
        return cands

    def _assign_recall_cells(self, turn: Turn) -> dict[int, Any]:
        interior = self._interior_cells(turn)
        return {
            worker.unit_id: interior[index]
            for index, worker in enumerate(turn.workers())
            if index < len(interior)
        }

    def _maybe_medicine(self, turn: Turn, commands: dict[int, dict[str, Any]]) -> None:
        """开拓者低血量且有药时使用（不覆盖已有指令，且本回合未在控炮）。"""
        pioneer = turn.pioneer()
        if pioneer is None or pioneer.unit_id in commands:
            return
        for cmd in commands.values():  # 操控占用：控炮回合不能再用药
            if cmd.get("action") == "attack" and cmd.get("controllerId") == str(pioneer.unit_id):
                return
        if pioneer.health > 120:
            return
        medicine = next(
            (item for item in pioneer.backpack if item.lower() == "medicine"), None
        )
        if medicine is not None:
            commands[pioneer.unit_id] = use_command(medicine)

    def _worker_fsm(self, worker) -> WorkerFSM:
        fsm = self.worker_fsms.get(worker.unit_id)
        if fsm is None:
            fsm = WorkerFSM(worker.unit_id)
            self.worker_fsms[worker.unit_id] = fsm
        return fsm


_BRAIN = Brain()


def decide(payload: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    return _BRAIN.decide(payload)
