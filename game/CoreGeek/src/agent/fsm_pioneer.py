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

DUSK_MARGIN = 5
RETURN_MARGIN_TASK = 2   # 任务中归位余量（更紧：任务优先，只要还能赶回基地就不早退）
EST_TASK_ROUNDS = 6      # 单任务预估耗时（接任务前时间预算用）
WEAPON_L1_COST = 100
WEAPON_L2_COST = 150


class PioneerFSM:
    def __init__(self) -> None:
        self.state = STATE_GUARD
        self.task_point: Pos | None = None
        self.accept_retries = 0
        self.failed_task_points: set = set()
        self.last_task_type: str | None = None   # 上次接取的任务类型（用于交替）
        self.upgrade_target: tuple | None = None  # (weapon Pos, level)
        self.returning = False                     # 归位粘性：一旦开始入夜前归位，不再被打断
        self.return_since = 0                      # 开始归位的回合（归位受阻超时兜底，IKI0Q8）

    # ================= 夜间 =================
    def _control_cell(self, turn: Turn, pioneer: Unit, ctx) -> Pos | None:
        """CP 不可达（被占/被墙封）→ 选一个**邻接炮塔最多**的可达格（IKI60Z）。"""
        turrets = getattr(ctx, "turret_cells", ())
        cands = set()
        for t in turrets:
            cands.update(t.neighbours())
        best, best_key = None, None
        for c in cands:
            if not turn.land(c) or c in turn.blocked(pioneer):
                continue
            adj = sum(1 for t in turrets if distance(c, t) <= 1)
            path = find_path(turn, pioneer, c, ctx.reserved) or find_path(turn, pioneer, c)
            if path is None:
                continue
            key = (-adj, len(path), c.x, c.y)
            if best is None or key < best_key:
                best, best_key = c, key
        return best

    def move_to_guard(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        if pioneer.pos == cp:
            return None
        step = next_step(turn, pioneer, cp, ctx.reserved) or next_step(turn, pioneer, cp)
        if step is None:
            alt = self._control_cell(turn, pioneer, ctx)
            if alt is not None:
                step = next_step(turn, pioneer, alt, ctx.reserved) or next_step(turn, pioneer, alt)
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
        # 仅在"任务已接取/进行中"时禁止插空升级；TASK_TRAVEL 途中仍可插空买券/用券
        # （用户：炮手做任务与买券升级互斥 → 需要任务间隙插空升级）
        in_task = self.state in (STATE_TASK_ACCEPT, STATE_TASK_WAIT_ACCEPT, STATE_TASK_WORK)
        # 4. 入夜前回归 CP（时间敏感；避免夜里还在外面）。
        #    **时间预算**（用户 2026-09-23）：任务中只留 RETURN_MARGIN_TASK=2 余量（任务优先，不早退），
        #    非任务用 DUSK_MARGIN=5；**保证能回**：travel 用 A* 实际路径，且余量不足时强制归位。
        travel = self._travel_rounds(turn, pioneer, cp, ctx)
        if turn.round_in_day == 0:
            self.returning = False       # 新的一天重置归位粘性
        margin = RETURN_MARGIN_TASK if in_task else DUSK_MARGIN
        must_return = turn.rounds_until_night <= travel + margin
        if self.returning or must_return:
            if not self.returning:
                self.return_since = turn.round_no
            self.returning = True
            if self.state in (STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE):
                self.state = STATE_GUARD
                self.upgrade_target = None
            if pioneer.pos != cp:
                # 兜底（IKI0Q8）：归位受阻 >50 回合（到不了 CP）→ 放弃归位、恢复行动，
                # 否则开拓者会永久卡在 RETURN_HOME（不升级武器/不做任务）。
                if turn.round_no - self.return_since > 50:
                    self.returning = False
                    self.state = STATE_GUARD
                else:
                    self.state = STATE_RETURN_HOME
                    self.task_point = None
                    step = next_step(turn, pioneer, cp, ctx.reserved) or next_step(turn, pioneer, cp)
                    if step is None:
                        # CP 不可达（被占/被墙封）→ 退到邻接炮塔最多的可达格（IKI60Z）
                        alt = self._control_cell(turn, pioneer, ctx)
                        if alt is not None:
                            self.state = STATE_GUARD
                            self.returning = False
                            step = (next_step(turn, pioneer, alt, ctx.reserved)
                                    or next_step(turn, pioneer, alt))
                    return move_command(step) if step is not None else None
            else:
                self.state = STATE_GUARD
                self.returning = False       # 已到 CP → 解除粘性
        else:
            # 未在归位（含跨天重置后）：若状态残留 RETURN_HOME（归位受阻/上一天遗留），
            # 必须复位为 GUARD，否则 `_weapon_upgrade_cmd` 被 `state != RETURN_HOME` 跳过、
            # `_task_flow` 又不处理该状态 → 开拓者永久发呆（IKI0Q8 根因）。
            if self.state == STATE_RETURN_HOME:
                self.state = STATE_GUARD
                self.upgrade_target = None
        # 宝藏优先（用户 2026-09-23）：计划 ready 且开启日临近（≥day-1）→ 先买祭品/去祭坛召唤
        if not in_task and self.state != STATE_RETURN_HOME:
            _tr = getattr(ctx, "treasure", None)
            if _tr is not None and getattr(_tr.plan, "ready", False) and not _tr.attempted:
                _d = getattr(_tr.plan, "day", 0) or 0
                if _d and turn.day_index >= _d - 1:
                    _cmd = self._treasure_cmd(turn, pioneer, ctx)
                    if _cmd is not None:
                        return _cmd
        # 前 2 天（用户 2026-09-23）：**任务优先**，剩余时间买券升级；
        # D3+：武器升级优先（任务间隙插空）。
        if turn.day_index <= 2:
            has_task = in_task or self._choose_task_point(turn, pioneer) is not None
            if has_task:
                return self._task_flow(turn, pioneer, cp, ctx)
            if not in_task and self.state != STATE_RETURN_HOME:
                cmd = self._weapon_upgrade_cmd(turn, pioneer, ctx)
                if cmd is not None:
                    return cmd
            return self._task_flow(turn, pioneer, cp, ctx)
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
            if self.state in (STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE):
                self.state = STATE_GUARD
            return None
        voucher = f"WeaponUpgradeVoucher{weapon.level}"
        needs = self._shopping_needs(turn, pioneer)
        # 采购：缺券就去店；在店里把当前所有缺口一次买齐（避免买了立刻回去升级再出来）
        if needs and (self.state == STATE_WEAPON_BUY or pioneer.backpack.count(voucher) < 1):
            affordable = [n for n in needs if turn.gold >= n[1]]
            if affordable:
                shop = self._nearest_shop(turn, pioneer)
                if shop is None:
                    return None
                self.state = STATE_WEAPON_BUY
                if pioneer.pos != shop and distance(pioneer.pos, shop) <= 1:
                    v, c, _want = affordable[0]
                    qty = self._buy_qty(turn, pioneer, v, c)
                    if qty <= 0:
                        return None  # 买不起：不清任务状态（防打断 TASK_TRAVEL 致震荡）
                    return buy_command(v, qty)
                step = step_toward(turn, pioneer, shop, ctx.reserved)
                return move_command(step) if step is not None else None
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
        # 没券且买不起：返回 None，但**不重置任务状态**（否则每回合重选任务点→来回震荡）
        self.upgrade_target = None
        if self.state in (STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE):
            self.state = STATE_GUARD
        return None

    def _shopping_needs(self, turn: Turn, pioneer: Unit) -> list[tuple[str, int, int]]:
        """还缺的武器券清单 [(voucher, cost, want)]：按武器等级升序，覆盖**所有**待升武器。"""
        needs: list[tuple[str, int, int]] = []
        for lvl, cost in ((1, WEAPON_L1_COST), (2, WEAPON_L2_COST)):
            cnt = sum(1 for w in turn.weapons() if w.level == lvl)
            if cnt <= 0:
                continue
            voucher = f"WeaponUpgradeVoucher{lvl}"
            # **全局统计**已持有（含维修工代买的；用户 2026-09-23：避免重复多买）
            held = sum(u.backpack.count(voucher) for u in turn.ours)
            want = cnt - held
            if want > 0:
                needs.append((voucher, cost, want))
        return needs

    def _buy_qty(self, turn: Turn, pioneer: Unit, voucher: str, cost: int) -> int:
        """批量购买：min(刚需, 背包容量, 金币//单价)；扣除已持有。买不起返回 0。"""
        held = pioneer.backpack.count(voucher)
        weapons = turn.weapons()
        lvl = 1 if voucher.endswith("1") else 2
        need = sum(1 for w in weapons if w.level == lvl)
        want = need - held
        if want <= 0:
            return 0
        room = (pioneer.capacity or 40) - len(pioneer.backpack)
        afford = turn.gold // cost if cost > 0 else 1
        return max(0, min(want, room, afford))

    def _nearest_shop(self, turn: Turn, pioneer: Unit) -> Pos | None:
        shops = turn.shop_positions()
        if not shops:
            return None
        return min(shops, key=lambda s: (distance(pioneer.pos, s), s.x, s.y))

    def _treasure_cmd(self, turn: Turn, pioneer: Unit, ctx) -> dict[str, Any] | None:
        """长上下文类（宝藏）：ready 计划才行动（最低优先，仅在无任务可接时）。"""
        planner = getattr(ctx, "treasure", None)
        if planner is None:
            return None
        return planner.cmd(turn, pioneer, self._nearest_shop(turn, pioneer), ctx)

    # ---- 任务流程 ----
    def _task_flow(self, turn: Turn, pioneer: Unit, cp: Pos, ctx) -> dict[str, Any] | None:
        if self.state == STATE_GUARD:
            target = self._choose_task_point(turn, pioneer)
            if target is None:
                # 无任务可接 → 尝试宝藏（P6，最低优先；仅在 ready 计划时行动）
                cmd = self._treasure_cmd(turn, pioneer, ctx)
                if cmd is not None:
                    return cmd
                return self.move_to_guard(turn, pioneer, cp, ctx)
            # 时间预算：来不及"去任务点+做任务+回基地"则不接，避免开了又被迫放弃
            if turn.is_day:
                to_task = distance(pioneer.pos, target)
                home = self._travel_rounds(turn, pioneer, cp, ctx)
                if turn.rounds_until_night < to_task + EST_TASK_ROUNDS + home + RETURN_MARGIN_TASK:
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
        """§12.7：两任务点交替（优先与上次不同类型）+ 最近可用点。"""
        cands = [
            t for t in turn.tasks
            if t.is_valid and t.cooldown_rounds == 0 and t.pos not in self.failed_task_points
        ]
        if not cands:
            return None
        cands.sort(key=lambda t: (
            t.task_type == self.last_task_type,     # 与上次同类型 → 排后
            distance(pioneer.pos, t.pos),
            -t.score_reward,
        ))
        chosen = cands[0]
        self.last_task_type = chosen.task_type
        return chosen.pos
