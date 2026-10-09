"""L4 捣乱鬼(imp)状态机 —— 32进16 新角色（M2 → 2026-10-09 全天候策略）。

用户裁决（2026-10-09）：
- imp 目标就是破坏，**不区分昼夜/天数**——常驻敌方半区毁矿，纪律只有"不死"。
- 安全三防线：
  ① 敌方角色 ≤FLEE_DIST(2) → 撤退（catch 射程 1；等速追不上，白天无武器、
     夜间敌方角色缩在基地控炮，矿区即安全区）；
  ② ≥2 个可见敌角色都在 ≤PINCER_DIST(5) → 双向包夹风险 → 弃桩撤（不赌方向）；
  ③ 机器人 ≤ROBOT_AVOID_DIST(2) → 避让（夜间威胁；机器人全图可见可提前绕开，
     机器人攻击阻挡其移动的单位——不挡路即无战）。
- 站桩被打断（撤退/避让/指令失败/不可达）→ 该矿拉黑 20 回合，换目标换方向
  （不原地反复送死循环）。
- 选矿评分 = vendor价 × remain（核心）× 同类连击加成（×1.5，10 回合窗口）——
  半区机制按"该类矿"计：连毁敌方半区同类矿 → 该类近乎枯竭 10 回合，压制翻倍。

破坏收益模型：毁一矿 = 敌方少 10 次采集 × 单价 + 该半区该类矿 10 回合不补刷。
死亡观：被抓=阵亡，但次日白天第 1 回合基地复活（v2.0），代价只是当天剩余破坏时间
→ 不过度保守：站桩前无需全图安全预检（敌角色视野仅 4 也查不到），站桩中每回合
实时盯梢、贴身即撤即可。
"""
from __future__ import annotations

from typing import Any

from .path import next_step, step_toward
from .protocol import (
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

FLEE_DIST = 2              # 敌方角色进入此距离 → 撤退（catch 需距离 1，留 1 格余量）
PINCER_FOE_N = 2           # 可见敌角色数 ≥ 此值 且全在 ≤PINCER_DIST → 包夹 → 弃桩撤
PINCER_DIST = 5
ROBOT_AVOID_DIST = 2       # 机器人进入此距离 → 避让（攻击射程 3，留 1 格余量）
MINE_BLACKLIST_ROUNDS = 20  # 站桩被打断/失败 → 该矿短期回避回合数
DESTROY_ROUNDS = 4         # 连续 destroy 回合数（任务书：满 4 矿消失）
COMBO_WINDOW = 10          # 同类矿连击加成窗口（回合）
COMBO_BONUS = 1.5          # 同类连击乘数（半区 10 回合不补刷 → 连毁同类压制翻倍）
CATCH_PRIORITY = True      # 可见敌方 imp 邻接 → 优先抓捕（+20 金 + 除害）

# 视为抓捕威胁的敌方单位（含敌方 imp：imp 之间可互抓）
_THREAT_KINDS = ("pioneer", "worker", "imp")


class ImpFSM:
    """捣乱鬼决策。跨回合状态：破坏进度 + 矿拉黑表 + 最近毁矿记录（连击）。"""

    def __init__(self) -> None:
        self.destroy_target: Pos | None = None
        self.destroy_progress: int = 0  # 连续 destroy 计数（4 回合满 → 矿消失）
        self.mine_blacklist: dict[Pos, int] = {}   # pos → 解禁回合
        self.last_destroyed: tuple[str, int] | None = None  # (矿种, 回合) → 连击加成

    def decide(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        # 0. 机会抓捕：可见敌方捣乱鬼（=正在站桩破坏）在我邻接格 → 必抓
        for foe in turn.enemy_imps():
            if distance(imp.pos, foe.pos) <= 1:
                ctx.note(imp.unit_id, "imp_catch_opportunity")
                return catch_command(foe.pos)
        # 1. 自保：敌角色贴身 / 双向包夹 → 弃桩撤退（站桩中即拉黑该矿）
        if self._foe_threat(turn, imp):
            flee = self._flee_cmd(turn, imp, ctx)
            self._break_stance(turn)
            if flee is not None:
                return flee
            return None  # 无路可退 → 原地待命（不站桩送进度）
        # 2. 机器人避让（夜间兵潮/白天残留；机器人全图可见）
        if self._robot_threat(turn, imp):
            step = self._robot_evade_step(turn, imp, ctx)
            if step is not None:
                self._break_stance(turn)
                return move_command(step)
        # 3. 站桩破坏（目标矿仍在且邻接）
        if (
            self.destroy_target is not None
            and turn.zones.get(self.destroy_target) in MINE_TYPES
            and distance(imp.pos, self.destroy_target) <= 1
            and imp.pos != self.destroy_target
        ):
            last_ok = turn.last_action_results.get(imp.unit_id, True)
            if not last_ok:
                # 指令失败（矿被采光竞态/非法）→ 拉黑换目标，不硬打循环
                self._blacklist(turn, self.destroy_target)
                self.destroy_target = None
                self._reset_progress()
                ctx.note(imp.unit_id, "imp_destroy_illegal")
                return None
            target = self.destroy_target
            self.destroy_progress += 1
            if self.destroy_progress >= DESTROY_ROUNDS:
                # 第 4 发：本回合发出（完成破坏），记连击，下回合重选目标
                kind = turn.zones.get(target)
                self.last_destroyed = (str(kind), turn.round_no)
                self.destroy_target = None
                self._reset_progress()
                ctx.note(imp.unit_id, f"imp_destroy_done({kind})")
            else:
                ctx.note(imp.unit_id, f"imp_destroy_r{self.destroy_progress}")
            return destroy_command(target)
        # 4. 目标缺失/失效（矿被采光/被打断移动）→ 重选（跳过拉黑矿）
        if self.destroy_target is None or turn.zones.get(self.destroy_target) not in MINE_TYPES:
            self._reset_progress()
            self.destroy_target = self._pick_target(turn, imp, ctx)
        if self.destroy_target is None:
            return None  # 无矿可破坏 → 待命
        # 5. 向目标矿的邻接操作格移动
        step = step_toward(turn, imp, self.destroy_target, ctx.reserved)
        if step is None:
            if distance(imp.pos, self.destroy_target) <= 1 and imp.pos != self.destroy_target:
                # 已邻接但上一步可能被打断 → 重新开始站桩
                self.destroy_progress = 1
                ctx.note(imp.unit_id, "imp_destroy_start")
                return destroy_command(self.destroy_target)
            # 不可达（被墙/单位围死）→ 拉黑换目标
            self._blacklist(turn, self.destroy_target)
            ctx.note(imp.unit_id, "imp_target_unreachable")
            self.destroy_target = None
            self._reset_progress()
            return None
        return move_command(step)

    # ---- 威胁判定 ----
    def _foe_threat(self, turn: Turn, imp: Unit) -> bool:
        foes = [u for u in turn.enemy if u.alive and u.kind in _THREAT_KINDS]
        near = [f for f in foes if distance(imp.pos, f.pos) <= FLEE_DIST]
        if near:
            return True
        # 包夹：≥2 个可见敌角色都在 ≤PINCER_DIST → 撤退方向可能被封 → 弃桩
        visible = [f for f in foes if distance(imp.pos, f.pos) <= PINCER_DIST]
        return len(visible) >= PINCER_FOE_N

    def _robot_threat(self, turn: Turn, imp: Unit) -> bool:
        return any(
            distance(imp.pos, r.pos) <= ROBOT_AVOID_DIST
            for r in turn.robots if r.alive
        )

    # ---- 撤退（敌方角色）----
    def _flee_cmd(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        """敌方角色贴身/包夹 → 向"离所有敌角色最远"的可走格撤一步。"""
        foes = [u for u in turn.enemy if u.alive and u.kind in _THREAT_KINDS]
        if not foes:
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

    def _robot_evade_step(self, turn: Turn, imp: Unit, ctx) -> Pos | None:
        """机器人避让：向"离最近机器人最远"的可走格挪一步；无路/挪不开 → 原地。"""
        robots = [r for r in turn.robots if r.alive]
        cur = min((distance(imp.pos, r.pos) for r in robots), default=99)
        blocked = turn.blocked(imp)
        best, best_key = None, None
        for pos in imp.pos.neighbours():
            if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                continue
            rmin = min((distance(pos, r.pos) for r in robots), default=99)
            tdist = distance(pos, self.destroy_target) if self.destroy_target else 0
            key = (-rmin, tdist, pos.x, pos.y)
            if best_key is None or key < best_key:
                best, best_key = pos, key
        if best is None:
            return None
        # 挪了也没拉开距离 → 不如原地等机器人走（机器人主攻基地不恋战）
        best_rmin = -best_key[0]
        if best_rmin <= cur:
            return None
        return best

    # ---- 选矿 ----
    def _pick_target(self, turn: Turn, imp: Unit, ctx) -> Pos | None:
        """选敌方半区价值最高矿：评分 = 小贩价 × remain ×（同类连击加成）；
        跳过我方半区矿与拉黑矿；平分取更近。"""
        combo_kind = None
        if (
            self.last_destroyed is not None
            and turn.round_no - self.last_destroyed[1] <= COMBO_WINDOW
        ):
            combo_kind = self.last_destroyed[0]
        best, best_key = None, None
        for pos, kind in turn.zones.items():
            if kind not in MINE_TYPES or turn.own_half(pos):
                continue  # 只破坏对方半区
            if self.mine_blacklist.get(pos, 0) > turn.round_no:
                continue  # 站桩被打断/失败 → 短期回避
            price = turn.vendor_prices.get(kind, 1)
            remain = turn.mine_remain.get(pos, 10)
            score = float(price * remain)
            if combo_kind is not None and kind == combo_kind:
                score *= COMBO_BONUS
            key = (score, -distance(imp.pos, pos))
            if best_key is None or key > best_key:
                best, best_key = pos, key
        if best is not None:
            ctx.note(imp.unit_id, f"imp_target({best.x},{best.y})")
        return best

    # ---- 状态工具 ----
    def _break_stance(self, turn: Turn) -> None:
        """站桩被打破（撤退/避让）→ 拉黑该矿 + 清进度（敌方蹲守该矿=已暴露，别回头送）。"""
        if self.destroy_progress > 0 and self.destroy_target is not None:
            self._blacklist(turn, self.destroy_target)
            self.destroy_target = None
            self._reset_progress()

    def _blacklist(self, turn: Turn, pos: Pos) -> None:
        self.mine_blacklist[pos] = turn.round_no + MINE_BLACKLIST_ROUNDS

    def _reset_progress(self) -> None:
        self.destroy_progress = 0


# 兜底校验：发出前再过一遍守卫（防"指令错误"异常红线）
def guarded(cmd: dict[str, Any] | None, turn: Turn, unit_id: int) -> dict[str, Any] | None:
    if cmd is None:
        return None
    return cmd if LegalityGuard(turn).check(unit_id, cmd).ok else None
