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
from .fsm_worker import ORE_MONEY, ORE_STONE, STONE_BATCH, DUSK_URGENT_ROUNDS, WorkerFSM
from .path import next_step
from .phases import PhaseManager
from .planners.layout import BaseLayout, choose_front, compute_layout
from .planners.task import TaskPlanner, TaskSession
from .planners.upgrade import UpgradePlanner
from .protocol import (
    ROCKET,
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
        """矿黑名单（新闻封矿/采集失败）：解封回合前回避。"""
        release = self.mine_blacklist.get(pos)
        return release is not None and round_no < release

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

        commands: dict[int, dict[str, Any]] = {}
        ctx = _Ctx(trace)
        ctx.fsms = self.worker_fsms
        ctx.reserved = {u.pos for u in turn.controllable()}
        ctx.mine_blacklist = self.mine_blacklist
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

        # 岗位：墙没建完时 1 号工人负责石料，其余经济矿（动态调度，不绑定 ID）
        for index, worker in enumerate(workers):
            fsm = self._worker_fsm(worker)
            fsm.ore_role = ORE_STONE if (index == 0 and walls_missing) else ORE_MONEY

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
        urgent = 0 < turn.rounds_until_night <= DUSK_URGENT_ROUNDS
        for worker in workers:
            if worker.unit_id in busy:
                continue
            fsm = self._worker_fsm(worker)
            if fsm.build is not None:
                continue
            stones = worker.backpack.count("stone")
            if not walls_missing or stones < 1 or (stones < STONE_BATCH and not urgent):
                continue
            cell = min(
                (c for c in walls_missing if c not in assigned),
                key=lambda c: distance(worker.pos, c),
                default=None,
            )
            if cell is not None:
                fsm.build = (cell, WALL)
                busy.add(worker.unit_id)

        # 升级任务分配：派给空闲工人（武器>墙>基地，券费已含预算保留；同一建筑不重复派单）
        free_workers = [w for w in workers if w.unit_id not in busy]
        if free_workers:
            taken_targets = {
                fsm.upgrade[0] for fsm in self.worker_fsms.values() if fsm.upgrade
            }
            for mission in self.upgrades.plan(turn):
                if mission.target in taken_targets:
                    continue
                candidates = [
                    w for w in free_workers
                    if self._worker_fsm(w).upgrade is None
                    and self._worker_fsm(w).mine is None  # 升级不得打断采矿锁（#11）
                ]
                if not candidates:
                    break
                worker = min(candidates, key=lambda w: distance(w.pos, mission.target))
                self._worker_fsm(worker).upgrade = (mission.target, mission.kind)
                taken_targets.add(mission.target)
                ctx.trace.setdefault("upgrade_assigned", []).append(
                    {"worker": worker.unit_id, "kind": mission.kind,
                     "target": mission.target.dump(), "voucher": mission.voucher}
                )

        for worker in workers:
            cmd = self._worker_fsm(worker).decide(turn, worker, ctx)
            if cmd:
                commands[worker.unit_id] = cmd
            ctx.reserve_from(cmd)

        pioneer = turn.pioneer()
        if pioneer is not None:
            ctx.home_anchor = layout.control_point
            cmd = self.pioneer_fsm.day_cmd(turn, pioneer, layout.control_point, ctx)
            if cmd:
                commands[pioneer.unit_id] = cmd
                ctx.reserve_from(cmd)
            # 任务求解：仅驻留任务点且任务进行中（submit 不覆盖走位指令）
            if self.pioneer_fsm.state == STATE_TASK_WORK and turn.phase_task:
                out = self.task_planner.work(turn, self.task_session)
                ctx.prompt = out.prompt
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
        if report.level == "CRITICAL":
            ctx._recall_cells = self._assign_recall_cells(turn)
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
        pioneer = turn.pioneer()
        if pioneer is not None and self.layout is not None:
            cmd = self.pioneer_fsm.move_to_guard(
                turn, pioneer, self.layout.control_point, ctx
            )
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

    # ---- 机器人出生点遥测（FRONT 修正证据，RULE_ASSUMPTIONS U2）----
    def _observe_spawns(self, turn: Turn, ctx: _Ctx) -> None:
        if turn.round_in_day != 70 or not turn.robots:  # 每夜第1回合
            return
        station = turn.station()
        if station is None:
            return
        spawns = [(r.pos.x, r.pos.y) for r in turn.robots]
        self.robot_spawn_log.append({"day": turn.day_index, "spawns": spawns})
        # 主来向：相对基地的 dominant axis（累计所有夜晚）
        sx = sum(x for day in self.robot_spawn_log for x, _ in day["spawns"])
        sy = sum(y for day in self.robot_spawn_log for _, y in day["spawns"])
        n = sum(len(day["spawns"]) for day in self.robot_spawn_log)
        cx, cy = station.pos.x + 0.5, station.pos.y - 0.5
        dx, dy = sx / n - cx, sy / n - cy
        observed = ("E" if dx > 0 else "W") if abs(dx) >= abs(dy) else ("N" if dy > 0 else "S")
        ctx.trace["front_observed"] = observed
        if self.front is not None and observed != self.front:
            ctx.trace["front_recommendation"] = observed  # P4 仅记录，重建走证据驱动变更

    # ---- 召回与自救 ----
    def _assign_recall_cells(self, turn: Turn) -> dict[int, Any]:
        """CRITICAL 时工人的回防站位：基地邻域内、离前线 CP 最远的空格。"""
        station = turn.station()
        if station is None or self.layout is None:
            return {}
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
        return {
            worker.unit_id: cands[index]
            for index, worker in enumerate(turn.workers())
            if index < len(cands)
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
