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

FLEE_DIST = 2              # 敌方角色进入此距离 → 贴边游走放哨（catch 射程 1，留 1 格余量）
IMP_FLEE_DIST = 4          # **敌 imp 专用**威胁距离（IKKHUU-Q1-E：敌 imp 是唯一能秒杀
                           # 我们的单位——catch 瞬杀 500 血，等速追击逃不掉 → 更早脱离）
NIGHT_BASE_CLEAR = 8       # 夜间/黄昏距敌基地 <8 格 → 强制撤出（IKKHUU-Q1-D：敌 imp
                           # 夜间守家，r74 我方 imp 在敌基地门口被 catch 秒杀实锤）
DUSK_LEAVE_MARGIN = 6      # 入夜前 6 回合开始出圈（白天可在圈内拆矿）
ROBOT_AVOID_DIST = 3       # 机器人进入此距离 → 避让（=机器人攻击距离 3；IKKDR0-F24：
                           # 阈值 2 时序上晚一拍——请求快照时机器人已退到 3-4 格外，
                           # 结算时走近攻击，imp 在敌基地窄道 10 回合被磨死 500 血）
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
        self._foe_hist: dict[int, list] = {}       # 敌角色 unit_id → 近 4 回合位置（移动趋势判据）

    def decide(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        # 0. 机会抓捕：可见敌方捣乱鬼（=正在站桩破坏）在我邻接格 → 必抓
        for foe in turn.enemy_imps():
            if distance(imp.pos, foe.pos) <= 1:
                ctx.note(imp.unit_id, "imp_catch_opportunity")
                return catch_command(foe.pos)
        # 0.5 敌基地消失 → 不再破坏（用户裁决 IKKE6Q-Q1-C：敌基地都没了，拆矿无意义）
        if next((u for u in turn.enemy if u.kind == "station"), None) is None:
            return None
        # 1. 自保：敌角色贴身 → **贴边游走放哨**（用户裁决 IKKE6Q-Q1-D：与敌保持 3-4 格
        #    绕目标矿游走，敌一离开立即回桩——代替逃远+拉黑自锁）
        #    Q1-A：**包夹判定删除**（对手不会花大力气围捕，矿区人多≠围捕）；
        #    Q1-B：flee 打断**不拉黑**目标矿（威胁暂态，进度清零但保留目标）。
        if self._foe_threat(turn, imp):
            if self.destroy_progress > 0:
                ctx.note(imp.unit_id, "imp_threat_hold")
                self._reset_progress()          # 规则：未执行 destroy 即清零，但保留目标
            flee = self._flee_cmd(turn, imp, ctx)
            if flee is not None:
                return flee
            return None  # 无路可走 → 原地放哨
        # 2. 机器人避让（夜间兵潮/白天残留；机器人全图可见）
        if self._robot_threat(turn, imp):
            step = self._robot_evade_step(turn, imp, ctx)
            if step is not None:
                self._break_stance(turn)
                return move_command(step)
        # 2.5 夜间/黄昏敌基地禁入圈（IKKHUU-Q1-D）：敌 imp 守家，门口=秒杀区；
        #     黄昏前 6 回合即开始出圈（保证入夜时已在圈外）。进度清零但**不拉黑**目标矿
        #     （次日白天可回桩）。
        es = next((u for u in turn.enemy if u.kind == "station"), None)
        if es is not None and (turn.is_night or turn.rounds_until_night <= DUSK_LEAVE_MARGIN):
            if distance(imp.pos, es.pos) < NIGHT_BASE_CLEAR:
                step = self._leave_circle_step(turn, imp, es.pos, ctx)
                if step is not None:
                    if self.destroy_progress > 0:
                        ctx.note(imp.unit_id, "imp_base_clear_hold")
                        self._reset_progress()
                    else:
                        ctx.note(imp.unit_id, "imp_base_clear")
                    return move_command(step)
                ctx.note(imp.unit_id, "imp_base_clear_stuck")  # 无路可走 → 原地硬抗
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
        # 5. 向目标矿的邻接操作格移动（IKKIA9-Q3 用户裁决：**路线危险预检**——
        #    去拆矿的路上同时计算"敌方角色 + 敌方基地"双威胁；下一步不安全 →
        #    换安全邻格绕行；没有安全路线 → **原地不动**（不要乱跑送死）。）
        step = step_toward(turn, imp, self.destroy_target, ctx.reserved)
        if step is not None and self._pos_dangerous(turn, step, imp):
            safe = self._safe_detour(turn, imp, self.destroy_target, ctx)
            if safe is None:
                ctx.note(imp.unit_id, "imp_travel_hold")   # 无安全路线 → 原地等待
                if self.destroy_progress > 0:
                    self._reset_progress()
                return None
            step = safe
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
    def _nightish(self, turn: Turn) -> bool:
        return turn.is_night or turn.rounds_until_night <= DUSK_LEAVE_MARGIN

    def _pos_dangerous(self, turn: Turn, pos: Pos, imp: Unit) -> bool:
        """双威胁源危险判定（IKKIA9-Q3 用户裁决）：敌方角色 + 敌方基地。

        - 夜/黄昏踏入敌基地 NIGHT_BASE_CLEAR 圈 = 危险（敌 imp 守家秒杀区）；
        - 可见敌 imp 距 ≤IMP_FLEE_DIST = 危险（唯一秒杀单位，等速追击逃不掉）；
        - 其他敌角色邻接（≤1，catch 射程）= 危险（绝不走进可被抓格）。
        """
        es = next((u for u in turn.enemy if u.kind == "station"), None)
        if (
            es is not None
            and self._nightish(turn)
            and distance(pos, es.pos) < NIGHT_BASE_CLEAR
        ):
            return True
        for f in turn.enemy:
            if not f.alive or f.kind not in _THREAT_KINDS:
                continue
            if f.kind == "imp" and distance(pos, f.pos) <= IMP_FLEE_DIST:
                return True
            if f.kind != "imp" and distance(pos, f.pos) <= 1:
                return True
        return False

    def _safe_detour(self, turn: Turn, imp: Unit, target: Pos, ctx) -> Pos | None:
        """直行下一步危险 → 在邻格中找"安全且向目标推进"的绕行步；没有则 None。"""
        blocked = turn.blocked(imp)
        best, best_key = None, None
        for pos in imp.pos.neighbours():
            if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                continue
            if self._pos_dangerous(turn, pos, imp):
                continue
            key = (distance(pos, target), pos.x, pos.y)
            if best_key is None or key < best_key:
                best, best_key = pos, key
        # 绕行步必须不比原地离目标更远（否则等于乱跑）
        if best is not None and distance(best, target) <= distance(imp.pos, target):
            return best
        return None

    def _foe_threat(self, turn: Turn, imp: Unit) -> bool:
        """贴身且敌角色**在移动**才构成威胁（用户裁决 IKKE6Q-Q1-A：对手不会花大力气
        围捕——矿区静止采矿的工人不抓 imp，无需躲；移动中（巡逻/追击）的敌才贴边游走。
        位置历史按敌 unit_id 记录近 4 回合。
        IKKHUU-Q1-E：**敌 imp 例外**——唯一秒杀威胁、见我就追，距离 ≤IMP_FLEE_DIST(4)
        直接视为威胁（不看移动趋势）。"""
        for f in (u for u in turn.enemy if u.alive and u.kind in _THREAT_KINDS):
            thr = IMP_FLEE_DIST if f.kind == "imp" else FLEE_DIST
            if distance(imp.pos, f.pos) > thr:
                continue
            if f.kind == "imp":
                return True
            hist = self._foe_hist.setdefault(f.unit_id, [])
            hist.append((turn.round_no, f.pos))
            if len(hist) > 4:
                hist.pop(0)
            recent = [p for rn, p in hist if turn.round_no - rn <= 3]
            if len(set(recent)) > 1 or len(recent) < 2:
                return True     # 在移动（或历史不足）→ 视为威胁
            # 静止敌（采矿中）→ 无视
        return False

    def _robot_threat(self, turn: Turn, imp: Unit) -> bool:
        return any(
            distance(imp.pos, r.pos) <= ROBOT_AVOID_DIST
            for r in turn.robots if r.alive
        )

    # ---- 威胁期贴边游走（用户裁决 IKKE6Q-Q1-D）----
    def _flee_cmd(self, turn: Turn, imp: Unit, ctx) -> dict[str, Any] | None:
        """敌方角色贴身 → 挪到与最近敌角色 ≥3 格、且**尽量贴近目标矿**的邻格
        （放哨：绕矿保持安全距离，敌一离开立即回桩——代替逃远+拉黑自锁）。
        IKKIA9-Q3（用户裁决）：躲敌**不得闯入敌方基地禁入圈**（防"躲狼入虎口"）。
        IKKHUU-Q1-E：**敌 imp 贴身 → 逃命模式**——全力远离敌 imp 并朝我方基地撤。"""
        foes = [u for u in turn.enemy if u.alive and u.kind in _THREAT_KINDS]
        if not foes:
            return None
        blocked = turn.blocked(imp)
        es = next((u for u in turn.enemy if u.kind == "station"), None)

        def _circle(pos: Pos) -> bool:
            return (
                es is not None
                and self._nightish(turn)
                and distance(pos, es.pos) < NIGHT_BASE_CLEAR
            )

        imp_foes = [f for f in foes if f.kind == "imp"]
        if imp_foes:
            cands = []
            for pos in imp.pos.neighbours():
                if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                    continue
                if _circle(pos):
                    continue  # 远离敌 imp 不等于可以闯敌基地
                d_imp = min((distance(pos, f.pos) for f in imp_foes), default=99)
                home = 0
                st = turn.station()
                if st is not None:
                    home = distance(pos, st.pos)
                cands.append((-d_imp, home, pos.x, pos.y, pos))
            if not cands:
                # 全被堵/全是圈 → 允许闯圈保命（被 catch 必死，圈是概率死）
                for pos in imp.pos.neighbours():
                    if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                        continue
                    d_imp = min((distance(pos, f.pos) for f in imp_foes), default=99)
                    cands.append((-d_imp, 0, pos.x, pos.y, pos))
            if not cands:
                return None
            cands.sort()
            ctx.note(imp.unit_id, "imp_flee_from_imp")
            return move_command(cands[0][4])
        cands = []
        for pos in imp.pos.neighbours():
            if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                continue
            if _circle(pos):
                continue
            near = min((distance(pos, f.pos) for f in foes), default=99)
            tdist = distance(pos, self.destroy_target) if self.destroy_target else 0
            # 第一键：脱离危险（<3 格危险区）；第二键：贴近目标矿（放哨位）
            cands.append(((0 if near >= 3 else 1), tdist, -near, pos))
        if not cands:
            return None  # 无路可走（或只剩圈）→ 原地放哨
        cands.sort(key=lambda t: (t[0], t[1], t[2], t[3].x, t[3].y))
        ctx.note(imp.unit_id, "imp_sentry")
        return move_command(cands[0][3])

    def _leave_circle_step(self, turn: Turn, imp: Unit, center: Pos, ctx) -> Pos | None:
        """出敌基地禁入圈：选使距敌基地最远的可走邻格。"""
        blocked = turn.blocked(imp)
        best, best_key = None, None
        for pos in imp.pos.neighbours():
            if pos in blocked or not turn.land(pos) or pos in ctx.reserved:
                continue
            key = (distance(pos, center), pos.x, pos.y)
            if best_key is None or key > best_key:
                best, best_key = pos, key
        return best

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
        """选目标矿（用户裁决 2026-10-09 IKKDR0-Q1）：**按"离敌方基地更近"判定敌方矿**，
        替代对角线半区——矿无归属，敌方工人实际采的是他们基地附近的矿；
        对角线敌方三角里的左下角矿（如 (6,2)）离我方更近、敌方根本不会去采，不值得跑 16 格。
        评分 = 小贩价 × remain ×（同类连击加成）；跳过拉黑矿；平分取更近。
        """
        station = turn.station()
        enemy_station = next((u for u in turn.enemy if u.kind == "station"), None)
        combo_kind = None
        if (
            self.last_destroyed is not None
            and turn.round_no - self.last_destroyed[1] <= COMBO_WINDOW
        ):
            combo_kind = self.last_destroyed[0]
        best, best_key = None, None
        # 敌方矿判定：按基地距离（用户裁决 IKKDR0-Q1）；敌基地消失时 decide 已直接待命
        if enemy_station is None or station is None:
            return None
        for pos, kind in turn.zones.items():
            if kind not in MINE_TYPES:
                continue
            if distance(pos, enemy_station.pos) >= distance(pos, station.pos):
                continue  # 离我方更近/等距 → 我方经济圈，不拆
            if self.mine_blacklist.get(pos, 0) > turn.round_no:
                continue  # 站桩被打断/失败 → 短期回避
            # IKKHUU-Q1-D：黄昏/夜不选敌基地禁入圈内的矿（否则出圈后又走回去震荡）
            if (
                (turn.is_night or turn.rounds_until_night <= DUSK_LEAVE_MARGIN)
                and distance(pos, enemy_station.pos) < NIGHT_BASE_CLEAR
            ):
                continue
            price = turn.vendor_prices.get(kind, 1)
            remain = turn.mine_remain.get(pos, 10)
            score = float(price * remain)
            if combo_kind is not None and kind == combo_kind:
                score *= COMBO_BONUS
            # 用户原则 2（IKKDR0-Q1）：优先就近——距离进评分（12 格÷2.2、16 格÷2.6）
            score /= 1.0 + 0.1 * distance(imp.pos, pos)
            key = (score, -distance(imp.pos, pos))
            if best_key is None or key > best_key:
                best, best_key = pos, key
        if best is not None:
            ctx.note(imp.unit_id, f"imp_target({best.x},{best.y})")
        return best

    # ---- 状态工具 ----
    def _break_stance(self, turn: Turn) -> None:
        """站桩被打破（机器人避让）→ 拉黑该矿 + 清进度。
        （用户裁决 IKKE6Q-Q1-B：flee 打断**不拉黑**——已在 decide 内联处理。）"""
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
