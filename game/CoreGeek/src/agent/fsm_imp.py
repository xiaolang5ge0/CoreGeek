"""L4 捣乱鬼(imp)状态机 —— 32进16 新角色（M2）。

职责（P0 经济战 + 自保）：
- 白天：前往**敌方半区**高价值矿 → 站桩 destroy（连续 4 回合，进度本地跟踪，中断清零）
- 自保优先：8 邻域出现敌方角色 → 撤退（被抓=阵亡+送对方 20 金）
- 机会抓捕：可见敌方捣乱鬼进入周围一格 → catch（+20 金，除掉威胁）
- 夜间：imp 无背包无采集，躲己方防御圈内安全格待命（能否控炮待实战确认 U13）

破坏收益模型：破坏一矿 = 对方少 10 次采集 × 单价 + 半区 10 回合不补刷。
选矿评分 = vendor价格 × 剩余次数，平分时取更近者。
"""
from __future__ import annotations

from typing import Any

from .path import next_step, step_toward
from .protocol import (
    IMP,
    MINE_TYPES,
    Pos,
    Turn,
    Unit,
    catch_command,
    destroy_command,
    distance,
    move_command,
)
from .rules import LegalityGuard

FLEE_DIST = 2        # 敌方角色进入此距离 → 撤退（catch 需距离 1，留 1 格余量）
CATCH_PRIORITY = True  # 可见敌方 imp 邻接 → 优先抓捕（+20 金 + 除害）


class ImpFSM:
    """捣乱鬼决策。无跨回合硬状态（除破坏进度），每回合从 Turn 重判。"""

    def __init__(self) -> None:
        self.destroy_target: Pos | None = None
        self.destroy_progress: int = 0  # 连续 destroy 计数（4 回合满 → 矿消失）

    def decide(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        cmd = self._night_cmd(turn, imp, ctx) if turn.is_night else self._day_cmd(turn, imp, ctx)
        return cmd

    # ---- 白天 ----
    def _day_cmd(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        # 0. 机会抓捕：可见敌方捣乱鬼（=正在站桩破坏）在我邻接格 → 必抓
        for foe in turn.enemy_imps():
            if distance(imp.pos, foe.pos) <= 1:
                ctx.note(imp.unit_id, "imp_catch_opportunity")
                return catch_command(foe.pos)
        # 1. 自保：敌方角色太近 → 撤退
        flee = self._flee_cmd(turn, imp, ctx)
        if flee is not None:
            self._reset_progress()
            return flee
        # 2. 站桩破坏（目标矿仍在且邻接）
        if (
            self.destroy_target is not None
            and turn.zones.get(self.destroy_target) in MINE_TYPES
            and distance(imp.pos, self.destroy_target) <= 1
            and imp.pos != self.destroy_target
        ):
            last_ok = turn.last_action_results.get(imp.unit_id, True)
            if not last_ok:
                self._reset_progress()  # 指令非法 → 进度清零重计
            self.destroy_progress += 1
            ctx.note(imp.unit_id, f"imp_destroy_r{self.destroy_progress}")
            return destroy_command(self.destroy_target)
        # 3. 目标失效（矿被采光/被打断移动）→ 重选
        if self.destroy_target is None or turn.zones.get(self.destroy_target) not in MINE_TYPES:
            self._reset_progress()
            self.destroy_target = self._pick_target(turn, imp, ctx)
        if self.destroy_target is None:
            return None  # 无矿可破坏 → 待命
        # 4. 向目标矿的邻接操作格移动
        step = step_toward(turn, imp, self.destroy_target, ctx.reserved)
        if step is None and distance(imp.pos, self.destroy_target) <= 1:
            # 已邻接但上一步可能被打断 → 重新开始站桩
            self.destroy_progress = 1
            ctx.note(imp.unit_id, "imp_destroy_start")
            return destroy_command(self.destroy_target)
        if step is not None:
            return move_command(step)
        return None

    # ---- 黑夜 ----
    def _night_cmd(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        # 自保优先
        flee = self._flee_cmd(turn, imp, ctx)
        if flee is not None:
            return flee
        # 躲安全锚点（防御圈内圈）；已安全则待命
        anchor = ctx.safe_anchor or ctx.home_anchor
        if anchor is None or imp.pos == anchor:
            return None
        if any(distance(imp.pos, rc) <= 3 for rc in ctx.robot_cells):
            step = next_step(turn, imp, anchor, ctx.reserved)
            if step is not None:
                return move_command(step)
        return None

    # ---- 工具 ----
    def _flee_cmd(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        """8 邻域出现敌方角色（抓捕射程内风险）→ 向远离方向撤一步。"""
        foes = [u for u in turn.enemy if u.alive and u.kind not in ("wall", "station")]
        near = [f for f in foes if distance(imp.pos, f.pos) <= FLEE_DIST]
        if not near:
            return None
        blocked = turn.blocked(imp)
        cands = []
        for pos in imp.pos.neighbours():
            if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                continue
            risk = min((distance(pos, f.pos) for f in foes), default=99)
            cands.append((risk, distance(pos, self.destroy_target) if self.destroy_target else 0, pos))
        if not cands:
            return None
        cands.sort(key=lambda t: (-t[0], t[1], t[2].x, t[2].y))
        ctx.note(imp.unit_id, "imp_flee")
        return move_command(cands[0][2])

    def _pick_target(self, turn: Turn, imp: Unit, ctx) -> Pos | None:
        """选敌方半区价值最高矿：评分 = 小贩价 × 剩余次数；平分取更近。"""
        best, best_key = None, None
        for pos, kind in turn.zones.items():
            if kind not in MINE_TYPES or turn.own_half(pos):
                continue  # 只破坏对方半区
            price = turn.vendor_prices.get(kind, 1)
            remain = turn.mine_remain.get(pos, 10)
            key = (price * remain, -distance(imp.pos, pos))
            if best_key is None or key > best_key:
                best, best_key = pos, key
        if best is not None:
            ctx.note(imp.unit_id, f"imp_target({best.x},{best.y})")
        return best

    def _reset_progress(self) -> None:
        self.destroy_progress = 0


# 兜底校验：发出前再过一遍守卫（防"指令错误"异常红线）
def guarded(cmd: dict[str, Any] | None, turn: Turn, unit_id: int) -> dict[str, Any] | None:
    if cmd is None:
        return None
    return cmd if LegalityGuard(turn).check(unit_id, cmd).ok else None
