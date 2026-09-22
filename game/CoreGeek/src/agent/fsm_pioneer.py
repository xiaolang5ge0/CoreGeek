"""L4 PioneerFSM（炮手）：白天任务/买武器券/升级武器，入夜前归位炮台操控位。

优先级（用户指定）：
1. 血量低时吃药
2. 有武器升级计划 → 买武器升级券（批量：min(刚需, 背包容量, 金币//单价)）
3. 有武器待升级且身上有券 → 去升级
4. 入夜前回归炮台操控位（CP）
5. 正在任务状态 → 任务流程
6. 不在任务状态但有任务可接 → 去接任务
7. 寻宝就绪 → 寻宝
"""
from __future__ import annotations

from typing import Any

from .path import find_path, next_step, step_toward
from .protocol import (
    Pos,
    Turn,
    Unit,
    accept_task_command,
    buy_command,
    distance,
    move_command,
    use_command,
)

STATE_GUARD = "GUARD"
STATE_TASK_TRAVEL = "TASK_TRAVEL"
STATE_TASK_ACCEPT = "TASK_ACCEPT"
STATE_TASK_WAIT_ACCEPT = "TASK_WAIT_ACCEPT"
STATE_TASK_WORK = "TASK_WORK"
STATE_RETURN_HOME = "RETURN_HOME"
STATE_WEAPON_BUY = "WEAPON_BUY"
STATE_WEAPON_UPGRADE = "WEAPON_UPGRADE"

DUSK_MARGIN = 3
WEAPON_L1_COST = 100
WEAPON_L2_COST = 150


class PioneerFSM:
    def __init__(self) -> None:
        self.state = STATE_GUARD
        self.task_point: Pos | None = None
        self.accept_retries = 0
        self.failed_task_points: set = set()
        self.upgrade_target: tuple | None = None  # (weapon Pos, level)

    # ================= 夜间 =================
    def move_to_guard(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        if pioneer.pos == cp:
            return None
        step = next_step(turn, pioneer, cp, ctx.reserved)
        return move_command(step) if step is not None else None

    # ================= 白天 =================
    def day_cmd(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        cmd = self._day_cmd(turn, pioneer, cp, ctx)
        ctx.trace["pioneer"] = {
            "state": self.state,
            "task_point": self.task_point.dump() if self.task_point else None,
            "upgrade": [self.upgrade_target[0].dump(), self.upgrade_target[1]] if self.upgrade_target else None,
            "cmd": cmd.get("action") if cmd else None,
        }
        return cmd

    def _day_cmd(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        in_task = self.state in (
            STATE_TASK_TRAVEL, STATE_TASK_ACCEPT, STATE_TASK_WAIT_ACCEPT, STATE_TASK_WORK
        )
        # 4. 入夜前回归 CP（时间敏感，优先于升级/任务；避免夜里还在外面）
        travel = self._travel_rounds(turn, pioneer, cp, ctx)
        if turn.rounds_until_night <= travel + DUSK_MARGIN:
            if self.state in (STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE):
                self.state = STATE_GUARD
                self.upgrade_target = None
            if pioneer.pos != cp:
                self.state = STATE_RETURN_HOME
                self.task_point = None
                step = next_step(turn, pioneer, cp, ctx.reserved) or next_step(turn, pioneer, cp)
                return move_command(step) if step is not None else None
            self.state = STATE_GUARD
        # 2/3. 武器升级计划（买券 / 用券）——仅在未进行任务且非归位时
        if not in_task and self.state != STATE_RETURN_HOME:
            cmd = self._weapon_upgrade_cmd(turn, pioneer, ctx)
            if cmd is not None:
                return cmd
        return self._task_flow(turn, pioneer, cp, ctx)

    # ---- 武器升级 ----
    def _weapon_upgrade_cmd(self, turn: Turn, pioneer: Unit, ctx) -> dict[str, Any] | None:
        plan = getattr(ctx, "gunner_upgrade", None)
        if plan is None:
            if self.state in (STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE):
                self.state = STATE_GUARD
            self.upgrade_target = None
            return None
        target, _kind = plan
        weapon = next((w for w in turn.weapons() if w.pos == target), None)
        if weapon is None or weapon.level >= 3:
            self.upgrade_target = None
            self.state = STATE_GUARD
            return None
        voucher = f"WeaponUpgradeVoucher{weapon.level}"
        cost = WEAPON_L1_COST if weapon.level == 1 else WEAPON_L2_COST
        # 券已入手 → 去武器处升级
        if pioneer.backpack.count(voucher) >= 1:
            self.state = STATE_WEAPON_UPGRADE
            self.upgrade_target = (target, weapon.level)
            if pioneer.pos != target and distance(pioneer.pos, target) <= 1:
                self.upgrade_target = None
                self.state = STATE_GUARD
                return use_command(voucher, target)
            step = step_toward(turn, pioneer, target, ctx.reserved)
            return move_command(step) if step is not None else None
        # 没券 → 去商店买（批量）
        if turn.gold < cost:
            self.upgrade_target = None
            self.state = STATE_GUARD
            return None
        shop = self._nearest_shop(turn, pioneer)
        if shop is None:
            return None
        self.state = STATE_WEAPON_BUY
        if pioneer.pos != shop and distance(pioneer.pos, shop) <= 1:
            qty = self._buy_qty(turn, pioneer, voucher, cost)
            return buy_command(voucher, qty)
        step = step_toward(turn, pioneer, shop, ctx.reserved)
        return move_command(step) if step is not None else None

    def _buy_qty(self, turn: Turn, pioneer: Unit, voucher: str, cost: int) -> int:
        """批量购买：min(刚需, 背包容量, 金币//单价)；扣除已持有。"""
        held = pioneer.backpack.count(voucher)
        weapons = turn.weapons()
        # 刚需 = 还差几张券（同等级武器数 - 已持有）
        lvl = 1 if voucher.endswith("1") else 2
        need = sum(1 for w in weapons if w.level == lvl)
        want = max(0, need - held)
        if want <= 0:
            return 1
        room = (pioneer.capacity or 40) - len(pioneer.backpack)
        afford = turn.gold // cost if cost > 0 else 1
        return max(1, min(want, room, afford))

    def _nearest_shop(self, turn: Turn, pioneer: Unit) -> Pos | None:
        shops = turn.shop_positions()
        if not shops:
            return None
        return min(shops, key=lambda s: (distance(pioneer.pos, s), s.x, s.y))

    # ---- 任务流程 ----
    def _task_flow(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        if self.state == STATE_GUARD:
            target = self._choose_task_point(turn, pioneer)
            if target is None:
                return self.move_to_guard(turn, pioneer, cp, ctx)
            self.task_point = target
            self.state = STATE_TASK_TRAVEL
        if self.state == STATE_TASK_TRAVEL:
            task = next((t for t in turn.tasks if t.pos == self.task_point), None)
            if task is None or not task.is_valid or task.cooldown_rounds > 0:
                ctx.note_pioneer("task_point_invalid_abort")
                self.state = STATE_GUARD
                self.task_point = None
                return None
            if distance(pioneer.pos, self.task_point) <= 1:
                self.state = STATE_TASK_ACCEPT
            else:
                step = step_toward(turn, pioneer, self.task_point, ctx.reserved)
                if step is None:
                    ctx.note_pioneer("task_point_unreachable")
                    self.state = STATE_GUARD
                    self.task_point = None
                    return None
                return move_command(step)
        if self.state == STATE_TASK_ACCEPT:
            self.state = STATE_TASK_WAIT_ACCEPT
            self.accept_retries = 0
            return accept_task_command()
        if self.state == STATE_TASK_WAIT_ACCEPT:
            if turn.phase_task:
                self.state = STATE_TASK_WORK
                return None
            if turn.last_action_results.get(pioneer.unit_id, True) is False:
                ctx.note_pioneer("accept_fail_no_retry")
                self.failed_task_points.add(self.task_point)
                self.state = STATE_GUARD
                self.task_point = None
                return None
            self.accept_retries += 1
            if self.accept_retries > 1:
                ctx.note_pioneer("accept_no_confirm_abort")
                self.failed_task_points.add(self.task_point)
                self.state = STATE_GUARD
                self.task_point = None
                return None
            return accept_task_command()
        if self.state == STATE_TASK_WORK:
            if not turn.phase_task:
                ctx.note_pioneer("task_end")
                self.state = STATE_GUARD
                self.task_point = None
            return None  # 驻留任务点；求解由 TaskPlanner 推进
        if self.state == STATE_RETURN_HOME:
            if pioneer.pos == cp:
                self.state = STATE_GUARD
                return None
            step = next_step(turn, pioneer, cp, ctx.reserved) or next_step(turn, pioneer, cp)
            return move_command(step) if step is not None else None
        return None

    def _travel_rounds(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> int:
        path = find_path(turn, pioneer, cp, ctx.reserved)
        return len(path) - 1 if path else distance(pioneer.pos, cp)

    def _choose_task_point(self, turn: Turn, pioneer: Unit) -> Pos | None:
        cands = [
            t for t in turn.tasks
            if t.is_valid and t.cooldown_rounds == 0 and t.pos not in self.failed_task_points
        ]
        if not cands:
            return None
        cands.sort(key=lambda t: (distance(pioneer.pos, t.pos), -t.score_reward))
        return cands[0].pos
