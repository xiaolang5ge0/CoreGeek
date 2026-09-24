"""L4 WorkerFSM：角色固化（修理工 / 挖矿工）+ FSM + Goal Commitment。

角色（由 brain 按 ID 固定分配）：
- 修理工 ROLE_REPAIRER = worker[0]（ID 最小）：
    D1-D2 自由采石+建墙；D3+ 白天买墙升级券/维修券、升级墙；夜间留墙内维修/升级。
    夜间触发条件：D1-D2 不触发；D3+ 且 距天黑 ≤ 路径+4 → 提前归位；夜间有威胁→触发；无威胁→可外出。
- 挖矿工 ROLE_MINER = worker[1]：全天 采矿→卖钱→返回 循环。
    紧急：第一天石头>20 且 墙<14 → 临时建墙。

通用：血量低且背包有药 → 立即吃药（最高优先）。
"""
from __future__ import annotations

from typing import Any

from .path import find_path, next_step, step_toward
from .planners.upgrade import (
    WALL_CORNER,
    WALL_FRONT,
    WALL_MAX_HP,
    WALL_VOUCHER_BATCH,
    WEAPON_L1_COST,
    voucher_for,
    wall_rank,
    wall_target_level,
)
from .protocol import (
    DAY_ROUNDS,
    MINE_TYPES,
    Pos,
    ROUNDS_PER_DAY,
    STATION,
    TOWER_TYPES,
    Turn,
    Unit,
    WALL,
    WEAPON_MAX_HP,
    build_command,
    buy_command,
    collect_command,
    distance,
    move_command,
    sell_command,
    use_command,
)
from .wall_registry import REPAIR_HP_RATIO

# ---- 角色 ----
ROLE_REPAIRER = "repairer"
ROLE_MINER = "miner"

# ---- 状态 ----
STATE_FREE = "ECONOMIC_FREE"
STATE_BUILD = "BUILD"
STATE_MINE_LOCKED = "MINE_COLLECT_LOCKED"
STATE_SELL = "SELL"
STATE_EVADE = "EVADE"
STATE_REPAIR = "NIGHT_REPAIR"
STATE_UPGRADE = "UPGRADE"
STATE_RETURN = "RETURN_HOME"
STATE_CRITICAL = "CRITICAL_DEFENSE"

# ---- 常量 ----
STONE_BATCH = 6
DUSK_URGENT_ROUNDS = 12
SELL_NEAR_VENDOR_DIST = 2
SELL_NEAR_MIN_VALUE = 30    # 路过小贩也要凑够一批再卖（用户 2026-09-23：别采一点就卖）
SELL_RICH_MIN_VALUE = 150   # 专程卖门槛（用户 2026-09-23：屯够一大批再跑）
SELL_RICH_MAX_DIST = 20
SELL_FULL_RATIO = 0.85      # 背包近满才强制卖（约 30-40/容量；用户 2026-09-23 保险值）
DAY_FORCE_SELL_ROUND = 50   # 白天到第 50 回合强制变现（入夜前清货；用户 2026-09-23）
STUCK_LIMIT = 5
STUCK_COLLECT_LIMIT = 3
WORKER_DANGER_DIST = 3      # 机器人贴脸(≤)才规避；脱离 +2 恢复
EVADE_STICKY = 5            # 规避粘性：进入危险距离后持续规避 N 回合，防 evade↔采矿 反复横跳（IKHZM0）
MAX_MINE_DIST = 16
REPAIR_MARGIN = 3           # 修理工归位余量（距天黑 ≤ 路径 + 3）
REPAIR_STONE_KEEP = 5       # 修理工常备石头（修墙用），低于此不卖
RETURN_STICKY_DAY = 3       # D3+ 修理工回防粘性：一旦开始返回，跨昼夜持续到进墙
RETURN_MARGIN = 6           # 矿工返程 deadline 余量（当前回合 + 归程 + 6 ≥ 白天/夜间截止）
MINER_RETURN_DAY = 9        # 挖矿工从第 9 夜起才考虑夜间回防（用户 2026-09-23）
MINER_NO_RETURN_DAY = 3     # 前 3 天完全不回防（激进挖矿，只躲机器人；用户 2026-09-23）
# 紧急抢修阈值（用户 IKI8HA 2026-09-24）：墙血 < 35% 满血 → 最优先抢修（**含满级 L3**：
# L3 无法用升级券，必须降级用 WallFixer，否则修理工只升 L1/L2 墙、看着 L3 墙被打掉）
CRITICAL_REPAIR_RATIO = 0.35
DUSK_CORRIDOR_RADIUS = 1    # 黄昏走出出兵走廊的判定半径（贴着走廊格也算；用户 2026-09-24）


class WorkerFSM:
    def __init__(self, unit_id: int):
        self.unit_id = unit_id
        self.role = ROLE_MINER
        self.state = STATE_FREE
        self.mine: Pos | None = None
        self.build: tuple[Pos, str] | None = None
        self.sell_vendor: Pos | None = None
        self.upgrade: tuple | None = None  # (目标格|券名, 种类)
        self._stuck_moves = 0
        self._stuck_collects = 0
        self._last_pos: Pos | None = None
        self._last_bag = -1
        self._evading = False
        self._evade_until = 0        # 规避粘性截止回合（IKHZM0：防 evade↔采矿 横跳）
        self.build_phase = False
        self.returning = False       # 回防粘性：D4+ 一旦开始返回，持续到进墙
        self.return_since = 0        # 开始返回的回合（防死循环兜底）
        self._pos_hist: list = []    # 最近位置历史（检测 A→B→A 两格震荡）
        self._turn = None            # 当前 turn（震荡逃生用）
        self._unit = None            # 当前 unit（震荡逃生用）

    # ================= 主入口 =================
    def decide(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        self._turn = turn
        self._unit = unit
        if turn.is_day:
            self._evading = False
        if unit.pos != self._last_pos:
            self._stuck_moves = 0
        if len(unit.backpack) != self._last_bag:
            self._stuck_collects = 0
        self._last_pos = unit.pos
        self._last_bag = len(unit.backpack)
        # 位置历史（最多 6）：检测两格震荡（A→B→A，位置每回合都变，旧 stuck 检测不到）
        self._pos_hist.append(unit.pos)
        if len(self._pos_hist) > 6:
            self._pos_hist.pop(0)

        # 0. 吃药（所有角色最高优先）
        cmd = self._medicine_cmd(turn, unit)
        if cmd is None:
            if self.role == ROLE_REPAIRER:
                cmd = self._repairer(turn, unit, ctx)
            else:
                cmd = self._miner(turn, unit, ctx)
        self._trace(ctx, cmd)
        return cmd

    def _oscillating(self) -> bool:
        """A→B→A→B 两格震荡：当前位置 == 2 步前 且 上一步 == 3 步前。"""
        h = self._pos_hist
        return (
            len(h) >= 4
            and h[-1] == h[-3]
            and h[-2] == h[-4]
            and h[-1] != h[-2]
        )

    def _trace(self, ctx, cmd) -> None:
        up = self.upgrade
        up_repr = None
        if up:
            tgt = up[0]
            up_repr = [tgt.dump() if hasattr(tgt, "dump") else tgt, up[1]]
        ctx.trace["workers"][str(self.unit_id)] = {
            "role": self.role,
            "state": self.state,
            "mine": self.mine.dump() if self.mine else None,
            "build": [self.build[0].dump(), self.build[1]] if self.build else None,
            "sell": self.sell_vendor.dump() if self.sell_vendor else None,
            "upgrade": up_repr,
            "cmd": cmd.get("action") if cmd else None,
        }

    # ================= 通用 =================
    def _medicine_cmd(self, turn: Turn, unit: Unit) -> dict[str, Any] | None:
        cap = 220 if unit.kind == "worker" else 200
        if unit.health > 0.60 * cap:   # 用户 2026-09-23：阈值 55% → 60%
            return None
        med = next((i for i in unit.backpack if i.lower() == "medicine"), None)
        if med:
            self.state = STATE_FREE
            return use_command(med)
        return None

    def note_move_failure(self) -> None:
        """反馈：上回合移动失败（目标被占/争夺）→ 加速解卡看门狗。"""
        self._stuck_moves += 1

    def _move(self, step: Pos, ctx) -> dict[str, Any] | None:
        self._stuck_moves += 1
        # 卡住（原地不动）或 两格震荡（A→B→A）→ 震荡逃生 + 清目标重来
        if self._stuck_moves >= STUCK_LIMIT or self._oscillating():
            self._stuck_moves = 0
            esc = self._escape_step(ctx)   # 远离最近历史位置（IKHYD3 震荡逃生）
            self._pos_hist.clear()
            ctx.note(self.unit_id, "unstuck")
            self.mine = None
            self.build = None
            self.sell_vendor = None
            self.upgrade = None
            self.returning = False
            self.state = STATE_FREE
            return move_command(esc) if esc is not None else None
        return move_command(step)

    def _escape_step(self, ctx) -> Pos | None:
        """震荡逃生（IKHYD3）：远离最近 8 步的历史位置，且不贴地图边缘。"""
        turn, unit = self._turn, self._unit
        if turn is None or unit is None:
            return None
        blocked = turn.blocked(unit)
        hist = list(self._pos_hist)
        best, best_key = None, None
        for nb in unit.pos.neighbours():
            if not turn.land(nb) or nb in blocked or nb in ctx.reserved:
                continue
            away = min((distance(nb, h) for h in hist), default=0)
            edge = min(nb.x, nb.y, turn.width - 1 - nb.x, turn.height - 1 - nb.y)
            key = (-away, edge, nb.x, nb.y)   # 远离历史优先；其次不贴边
            if best is None or key < best_key:
                best, best_key = nb, key
        return best

    # ================= 修理工 =================
    def _repairer(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        # 修理工的家 = repair_post（内圈邻墙格），不是 CP（issue#26：D4+ 夜到修理位就位）
        anchor = getattr(ctx, "repair_anchor", None) or ctx.home_anchor
        # 回防粘性（D4+）：一旦开始返回，跨昼夜持续到进墙（A* 绕墙，不穿墙）
        if self.returning:
            cmd = self._go_home(turn, unit, ctx, anchor)
            if cmd is not None:
                if turn.round_no - self.return_since > 40:  # 兜底：长期无法进墙则放弃
                    self.returning = False
                return cmd
            self.returning = False  # 已进墙/受阻 → 继续正常流程
        # 夜间优先：升级（=回血，省修复包）→ 抢修 → D3+ 就位 repair_post（机器人清空前不回外）
        if turn.is_night:
            # 墙破躲避（IKHYD3 / 用户 2026-09-23）：本回合有墙被攻破 → 先逃向墙内安全格，
            # 不站在缺口送死（下一回合再恢复修复）。
            reg = getattr(ctx, "wall_registry", None)
            if reg is not None and any(
                s.breached_round == turn.round_no for s in reg.walls.values()
            ):
                cmd = self._go_home(turn, unit, ctx, anchor)
                if cmd is not None:
                    ctx.note(self.unit_id, "wall_breached_flee")
                    return cmd
            # 夜间**不采购**（用户 2026-09-23：夜里买来不及——开拓者控炮、一工人修墙、一工人采资源；
            # 修复包必须白天提前备足）。夜间只使用已购券/修复包。
            # **紧急抢修最优先**（用户 IKI8HA 2026-09-24）：L3 满级墙无法升级 → 必须用 WallFixer；
            # 否则会被"升 L1/L2 墙"的任务占满整夜，关键 L3 墙被打掉、修理工也被打死。
            cmd = self._critical_repair(turn, unit, ctx)
            if cmd is not None:
                ctx.note(self.unit_id, "critical_repair")
                return cmd
            cmd = self._upgrade_flow(turn, unit, ctx, allow_use=True, allow_buy=False)
            if cmd is not None:
                return cmd
            cmd = self._repair_cmd(turn, unit, ctx)
            if cmd is not None:
                return cmd
            robots_alive = bool(getattr(ctx, "robot_cells", ()))
            if turn.day_index >= RETURN_STICKY_DAY and robots_alive:
                # D3+ 且机器人未清空 → 就位 repair_post 守内圈（用户：清空前不外出采矿）
                return self._go_home(turn, unit, ctx, anchor)
            return self._miner(turn, unit, ctx)
        # 白天
        if turn.day_index <= 1:
            # D1 全力石料 + 建墙
            return self._build_mine(turn, unit, ctx, prefer="stone")
        # D2+：建墙优先（无缺口）→ 采购 → 回防预留 → 采矿(铜/铁)
        near_dusk = (
            0 < turn.rounds_until_night
            <= self._path_home_len(turn, unit, ctx, anchor) + REPAIR_MARGIN
        )
        if near_dusk:
            if turn.day_index >= RETURN_STICKY_DAY:
                self.returning = True
                self.return_since = turn.round_no
            cmd = self._go_home(turn, unit, ctx, anchor)
            if cmd is not None:
                return cmd
            self.returning = False
            return self._build_mine(turn, unit, ctx, prefer="money")  # 已归位/受阻 → 就近
        # 缺口补建优先（brain 派单）→ 紧急抢修 → 再升级（白天也允许，用户 2026-09-23）→ 再采矿
        if self.build is not None:
            self.state = STATE_BUILD
            return self._build_cmd(turn, unit, ctx)
        cmd = self._critical_repair(turn, unit, ctx)
        if cmd is not None:
            return cmd
        cmd = self._upgrade_flow(turn, unit, ctx, allow_use=True, allow_buy=True)
        if cmd is not None:
            return cmd
        return self._build_mine(turn, unit, ctx, prefer="money")

    def _build_mine(
        self, turn: Turn, unit: Unit, ctx, *, prefer: str = "stone"
    ) -> dict[str, Any] | None:
        """建墙优先（保证无缺口），否则采矿（D1 prefer=stone，D2+ prefer=money）。"""
        # 建墙任务优先
        if self.build is not None:
            self.state = STATE_BUILD
            return self._build_cmd(turn, unit, ctx)
        # 有石头 → 若还能建墙则建
        stones = unit.backpack.count("stone")
        if stones and ctx.walls_left > 0 and (self.build_phase or stones >= ctx.walls_left):
            return None  # 由 brain 派单
        # D2/D3 有缺口且石头不够 → 改采石补缺口（用户：D2/D3 白天也检索缺口）
        if ctx.walls_left > 0 and stones < ctx.walls_left:
            prefer = "stone"
        # 墙环建完后（D3+），修理工常备 ≥5 石头（用户 IKI0RT：备用补墙，防"夜里墙被打掉、白天无石料补"）
        elif (
            turn.day_index >= 3
            and ctx.walls_left == 0
            and self.role == ROLE_REPAIRER
            and stones < REPAIR_STONE_KEEP
        ):
            prefer = "stone"
        return self._mine_flow(turn, unit, ctx, prefer=prefer)

    # ================= 挖矿工 =================
    def _miner(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        # 紧急建墙（第一天 石头>20 且 墙<14）：brain 派单后由本流程执行
        if self.build is not None:
            self.state = STATE_BUILD
            return self._build_cmd(turn, unit, ctx)
        if (
            turn.day_index == 1
            and unit.backpack.count("stone") > 20
            and ctx.walls_left > 0
        ):
            self.state = STATE_BUILD
            return None  # 等 brain 派建墙单
        # 临近入夜：站在历史出兵走廊/出生点里 → 先走出来（用户 2026-09-24）
        cmd = self._dusk_corridor_escape(turn, unit, ctx)
        if cmd is not None:
            return cmd
        # 危险规避（粘性窗口，修 IKHZM0 横跳）
        cmd = self._evade_cmd(turn, unit, ctx)
        if cmd is not None or self._evading:
            return cmd
        # 决策 D（用户 2026-09-23）：矿工整夜"安全"外采（仅第 9 夜起考虑回防）。
        # 旧"D6 原地待命"会在机器人在基地 6 格内时把矿工钉在基地旁整夜不采矿 → 已移除。
        # 返程 deadline（用户 2026-09-23）：**前 3 天不回防**（激进挖矿，只躲机器人）；
        # D4+ 白天入夜前回基地附近；夜间默认不回防，仅第 9 夜起考虑
        if (
            turn.day_index > MINER_NO_RETURN_DAY
            and (turn.is_day or turn.day_index >= MINER_RETURN_DAY)
            and self._past_return_deadline(turn, unit, ctx)
        ):
            return self._go_home(
                turn, unit, ctx,
                getattr(ctx, "safe_anchor", None) or ctx.home_anchor,
            )
        cmd = self._mine_flow(turn, unit, ctx, prefer="money")
        if cmd is not None:
            return cmd
        # 兜底（IKI1T2：矿工 50 回合空转）：无可用矿（耗尽/被封/不安全）→
        # 移动到**最近的矿点**待命（矿会刷新），而不是原地发呆；无矿则回基地附近。
        mines = turn.mines()
        if mines:
            nearest = min(mines, key=lambda p: (distance(unit.pos, p), p.x, p.y))
            if distance(unit.pos, nearest) > 1:
                step = step_toward(turn, unit, nearest, ctx.reserved)
                if step is not None:
                    self.state = STATE_RETURN
                    return self._move(step, ctx)
            self.state = STATE_RETURN
            return None  # 已在矿旁等待刷新
        return self._go_home(
            turn, unit, ctx,
            getattr(ctx, "safe_anchor", None) or ctx.home_anchor,
        )

    def _past_return_deadline(self, turn: Turn, unit: Unit, ctx) -> bool:
        """D6 返程 deadline：白天截止 70、夜间截止 130（round_in_day），留 RETURN_MARGIN 余量。"""
        anchor = getattr(ctx, "safe_anchor", None) or ctx.home_anchor
        if anchor is None or unit.pos == anchor:
            return False
        deadline = DAY_ROUNDS if turn.is_day else ROUNDS_PER_DAY
        path = find_path(turn, unit, anchor, ctx.reserved)
        ret = len(path) - 1 if path else distance(unit.pos, anchor)
        return turn.round_in_day + ret + RETURN_MARGIN >= deadline

    # ================= 采矿+卖货 循环 =================
    def _mine_flow(self, turn: Turn, unit: Unit, ctx, *, prefer: str) -> dict[str, Any] | None:
        # 背包满/批量阈值 → 卖
        if self._batch_full(unit):
            if self.mine is not None:
                ctx.note(self.unit_id, "release_lock_batch_full")
                self.mine = None
            return self._sell_chain(turn, unit, ctx, mandatory=True)
        # 矿锁
        if self.mine is not None:
            # 矿锁与当前偏好不符（issue#26：修理工 Day1 建墙锁了石矿，入夜该采钱却仍走远石矿）
            # prefer=money 且锁的是石矿 → 释放（改采铜/铁）；prefer=stone 且锁的是铜/铁 → 释放。
            locked_kind = turn.zones.get(self.mine)
            # 锁矿不跳变（IKHYD3）：矿工选定后整个外出周期不变；仅修理工做 prefer 释放
            if (
                locked_kind is not None
                and self.role == ROLE_REPAIRER
                and (
                    (prefer == "money" and locked_kind == "stone")
                    or (prefer == "stone" and locked_kind != "stone")
                )
            ):
                ctx.note(self.unit_id, "mine_prefer_release")
                self.mine = None
            elif self.mine not in turn.mines():
                ctx.note(self.unit_id, "mine_depleted")
                self.mine = None
            elif ctx.is_mine_blocked(self.mine, turn.round_no):
                ctx.note(self.unit_id, "mine_blocked_news")
                self.mine = None
            elif ctx.dusk_avoid and ctx.mine_unsafe(self.mine):
                ctx.note(self.unit_id, "mine_unsafe_release")
                self.mine = None
            else:
                self.state = STATE_MINE_LOCKED
                cmd = self._mine_cmd(turn, unit, ctx)
                if cmd is not None:
                    return cmd
                # 移动卡死/不可达 → 拉黑该矿 20 回合，防止"清矿→立刻重选同一矿"死循环
                ctx.note(self.unit_id, "mine_unreachable")
                ctx.block_mine(self.mine, turn.round_no + 20)
                self.mine = None
        # FREE：机会性卖货（需要凑武器升级费 / 路过 / 货值够）
        self.state = STATE_FREE
        if self._should_sell(turn, unit, ctx):
            return self._sell_chain(turn, unit, ctx, mandatory=False)
        # 选矿
        self.mine = self._select_mine(turn, unit, ctx, prefer=prefer)
        if self.mine is None:
            ctx.note(self.unit_id, "no_mine")
            return None
        self.state = STATE_MINE_LOCKED
        cmd = self._mine_cmd(turn, unit, ctx)
        if cmd is None:
            # 移动卡死/不可达 → 拉黑该矿，防止反复重选同一矿死循环
            ctx.block_mine(self.mine, turn.round_no + 20)
            self.mine = None
        return cmd

    def _batch_full(self, unit: Unit) -> bool:
        return bool(
            unit.capacity and len(unit.backpack) >= SELL_FULL_RATIO * unit.capacity
        ) or unit.backpack_full

    def _mine_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if unit.pos != self.mine and distance(unit.pos, self.mine) <= 1:
            self._stuck_collects += 1
            if self._stuck_collects >= STUCK_COLLECT_LIMIT:
                self._stuck_collects = 0
                ctx.note(self.unit_id, "collect_stuck_release")
                ctx.block_mine(self.mine, turn.round_no + 20)
                self.mine = None
                self.state = STATE_FREE
                return None
            return collect_command(self.mine)
        self._stuck_collects = 0
        step = step_toward(
            turn, unit, self.mine,
            ctx.reserved | getattr(ctx, "miner_avoid", frozenset()),
        )
        if step is None:
            return None
        return self._move(step, ctx)

    def _select_mine(self, turn: Turn, unit: Unit, ctx, *, prefer: str) -> Pos | None:
        # 采石补缺口（prefer=stone）时，修理工对石矿有**优先权**（忽略他人锁），
        # 避免"矿工抢走唯一石矿 → 修理工拿不到石料 → 中间墙整天补不上"（IKHZOS）。
        other_locks = (
            () if (ctx.share_mines or prefer == "stone")
            else ctx.other_mine_locks(self.unit_id)
        )
        station = turn.station()
        enemy_station = next((u for u in turn.enemy if u.kind == "station"), None)
        primary: list = []
        fallback: list = []
        for pos in turn.mines():
            if pos in other_locks:
                continue
            if ctx.is_mine_blocked(pos, turn.round_no):
                continue
            if ctx.dusk_avoid and ctx.mine_unsafe(pos):
                continue
            kind = turn.zones.get(pos)
            our_dist = distance(station.pos, pos) if station is not None else distance(unit.pos, pos)
            entry = (pos, kind, distance(unit.pos, pos), our_dist)
            if prefer == "stone":
                if kind == "stone":
                    primary.append(entry)
            else:  # money：优先铜/铁，石矿兜底
                if kind == "stone":
                    fallback.append(entry)
                else:
                    primary.append(entry)
        valid = primary or fallback
        if not valid:
            return None
        near = [m for m in valid if m[3] <= MAX_MINE_DIST]
        if near:
            pool = near
        else:
            pool = [
                m for m in valid
                if enemy_station is None or distance(enemy_station.pos, m[0]) >= m[3]
            ]
        if not pool:
            return None
        best, best_key = None, None
        vendors = turn.vendor_positions()
        for pos, kind, dist, our_dist in pool:
            side = 12.0 if (enemy_station is not None
                            and distance(enemy_station.pos, pos) < our_dist) else 0.0
            if prefer == "stone":
                key = (dist + our_dist * 0.6 + side, dist, pos.x, pos.y)
            else:
                price = turn.vendor_prices.get(kind, 1)
                # 新闻囤货：被预测涨价的矿种优先级提高
                boost = ctx.price_boost(kind) if hasattr(ctx, "price_boost") else 0.0
                # 单位回合收益（含"矿→小贩"返程，用户 2026-09-23）：
                #   rate = 一矿收益 / 往返；**加大去矿距离权重**（近矿优先，减少长途空跑，用户 2026-09-23）。
                vendor_dist = min((distance(pos, v) for v in vendors), default=0)
                rate = (price * 10.0 + boost) / (1.7 * dist + vendor_dist + 6.0)
                key = (-rate, our_dist * 0.1 + side, dist, pos.x, pos.y)
            if best is None or key < best_key:
                best, best_key = pos, key
        return best

    # ---- 卖货 ----
    def _should_sell(self, turn: Turn, unit: Unit, ctx) -> bool:
        value = self._sellable_value(turn, unit, ctx)
        if value <= 0:
            return False
        vendor = self._nearest_vendor(turn, unit)
        if vendor is None:
            return False
        if ctx.dusk_avoid and ctx.mine_unsafe(vendor):
            return False
        # 需要凑武器升级费 → 去卖
        if getattr(ctx, "need_weapon_gold", False):
            return True
        if self._batch_full(unit):
            return True
        # 白天后半段主动卖货（IKHYD3 / 用户 2026-09-23）：白天第 50 回合后 → 入夜前变现
        if turn.is_day and turn.round_in_day >= DAY_FORCE_SELL_ROUND:
            return True
        dist = distance(unit.pos, vendor)
        if dist <= SELL_NEAR_VENDOR_DIST and value >= SELL_NEAR_MIN_VALUE:
            return True
        if value >= SELL_RICH_MIN_VALUE and dist <= SELL_RICH_MAX_DIST:
            return True
        return False

    def _stone_keep(self) -> int:
        """修理工常备石头（修墙用），低于此不卖；其余角色不留。"""
        return REPAIR_STONE_KEEP if self.role == ROLE_REPAIRER else 0

    def _sellable_value(self, turn: Turn, unit: Unit, ctx) -> int:
        value = 0
        for ore in MINE_TYPES:
            count = unit.backpack.count(ore)
            if ore == "stone":
                # 只保留"够建剩余墙 + 常备 5"的石头，多余可卖（防背包被石头塞满，
                # 导致没空间备 WallFixer/升级券 —— IKI1T4 根因）
                keep = max(self._stone_keep(), ctx.walls_left)
                count = max(0, count - keep)
            value += count * turn.vendor_prices.get(ore, 1)
        return value

    def _sell_chain(self, turn: Turn, unit: Unit, ctx, *, mandatory: bool) -> dict[str, Any] | None:
        vendor = self._nearest_vendor(turn, unit)
        if vendor is None:
            ctx.note(self.unit_id, "no_vendor")
            return None
        self.sell_vendor = vendor
        if unit.pos != vendor and distance(unit.pos, vendor) <= 1:
            cmd = self._sell_best(turn, unit, ctx)
            self.state = STATE_SELL if cmd else STATE_FREE
            if cmd is None:
                self.sell_vendor = None
            return cmd
        step = step_toward(turn, unit, vendor, ctx.reserved)
        if step is None:
            ctx.note(self.unit_id, "vendor_unreachable")
            return None
        self.state = STATE_SELL
        return self._move(step, ctx)

    def _sell_best(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        # 优先卖"新闻预测高价"的矿种，否则卖 数量×单价 最高
        best = None
        for ore in MINE_TYPES:
            count = unit.backpack.count(ore)
            if ore == "stone":
                keep = max(self._stone_keep(), ctx.walls_left)
                count = max(0, count - keep)
            if count == 0:
                continue
            value = count * turn.vendor_prices.get(ore, 1)
            if hasattr(ctx, "price_boost"):
                value += ctx.price_boost(ore)  # 囤货到期 → 优先卖
            if best is None or value > best[0]:
                best = (value, ore, count)
        if best is None:
            return None
        return sell_command(best[1], best[2])

    def _nearest_vendor(self, turn: Turn, unit: Unit) -> Pos | None:
        vendors = turn.vendor_positions()
        if not vendors:
            return None
        return min(vendors, key=lambda v: (distance(unit.pos, v), v.x, v.y))

    # ---- 危险规避 ----
    def _dusk_corridor_escape(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """临近入夜：若站在历史出兵走廊/出生点附近 → 先走出来（用户 2026-09-24）。

        白天黄昏窗口生效（夜间由 `miner_avoid` 路径绕行处理）；朝基地/安全锚点走。
        """
        if turn.is_night or not getattr(ctx, "dusk_avoid", False):
            return None
        spawns = getattr(ctx, "spawn_cells", ())
        corridor = getattr(ctx, "corridor_cells", ())
        if not spawns and not corridor:
            return None
        inside = (
            any(distance(unit.pos, c) <= DUSK_CORRIDOR_RADIUS for c in corridor)
            or any(distance(unit.pos, c) <= DUSK_CORRIDOR_RADIUS for c in spawns)
        )
        if not inside:
            return None
        goal = getattr(ctx, "safe_anchor", None) or ctx.home_anchor
        if goal is None:
            return None
        step = next_step(turn, unit, goal, ctx.reserved) or next_step(turn, unit, goal)
        if step is None:
            return None
        ctx.note(self.unit_id, "dusk_corridor_escape")
        return self._move(step, ctx)

    def _evade_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if not turn.is_night:
            self._evading = False
            self._evade_until = 0
            return None
        nearest = min(
            (distance(unit.pos, r.pos) for r in turn.robots
             if r.alive and r.target_team in ("", turn.team_type)),
            default=99,
        )
        # 规避粘性（修 IKHZM0 两格震荡）：一旦进入危险距离，持续规避 EVADE_STICKY 回合，
        # 期间不因机器人抖动立刻切回采矿（否则 evade↔mine 每回合横跳、原地打转）。
        if nearest <= WORKER_DANGER_DIST:
            self._evade_until = turn.round_no + EVADE_STICKY
        if turn.round_no >= self._evade_until:
            self._evading = False
            return None
        self._evading = True
        # 局部远离最近机器人；无法改善则原地不动（回防/走位交由上层角色流程处理）
        step = self._flee_step(turn, unit, ctx)
        self.state = STATE_EVADE
        return self._move(step, ctx) if step is not None else None

    def _flee_step(self, turn: Turn, unit: Unit, ctx) -> Pos | None:
        robots = [r.pos for r in turn.robots if r.alive and r.target_team in ("", turn.team_type)]
        if not robots:
            return None
        blocked = turn.blocked(unit)
        cur = min(distance(unit.pos, rp) for rp in robots)
        best, best_gap = None, cur
        for nb in unit.pos.neighbours():
            if not turn.land(nb) or nb in blocked or nb in ctx.reserved:
                continue
            gap = min(distance(nb, rp) for rp in robots)
            if gap > best_gap:
                best, best_gap = nb, gap
        return best

    # ---- 建造 ----
    def _build_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        cell, name = self.build
        if unit.pos != cell and distance(unit.pos, cell) <= 1:
            self.build = None
            self.state = STATE_FREE
            return build_command(cell, name)
        step = step_toward(turn, unit, cell, ctx.reserved)
        if step is not None:
            return self._move(step, ctx)
        ctx.note(self.unit_id, "build_unreachable")
        self.build = None
        self.state = STATE_FREE
        return None

    # ---- 升级 ----
    def _upgrade_flow(
        self, turn: Turn, unit: Unit, ctx, *, allow_use: bool = True, allow_buy: bool = True
    ) -> dict[str, Any] | None:
        """执行已分配的升级/备货任务。

        白天：allow_use=False, allow_buy=True → 只采购（不占用白天采集）。
        夜间：allow_use=True, allow_buy=False → 只使用（升级=回血，省修复包）。
        """
        if self.upgrade is None:
            return None
        return self._upgrade_cmd(turn, unit, ctx, allow_use=allow_use, allow_buy=allow_buy)

    def _upgrade_cmd(
        self, turn: Turn, unit: Unit, ctx, *, allow_use: bool = True, allow_buy: bool = True
    ) -> dict[str, Any] | None:
        target, kind = self.upgrade[0], self.upgrade[1]
        qty = self.upgrade[2] if len(self.upgrade) > 2 else 1
        if kind == "stock":
            item = target if isinstance(target, str) else "WallFixer"
            if unit.backpack.count(item) >= qty:
                self.upgrade = None
                self.state = STATE_FREE
                return None
            if not allow_buy:
                return None
            want = self._stock_qty(turn, unit, ctx, item, qty)
            if want <= 0:
                # 买不起/没空间 → **清掉任务**，别死占修理工（IKI9X2：备货任务卡死 318 回合，
                # 阻断所有墙升级）。规划器金币不足时不会再下发该备货任务 → 修理工转做墙升级。
                self.upgrade = None
                self.state = STATE_FREE
                return None
            return self._buy_item(turn, unit, ctx, item, want)
        building = next(
            (u for u in turn.ours if u.pos == target and u.kind in (*TOWER_TYPES, WALL, STATION)),
            None,
        )
        if building is None or building.level >= 3:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        entry = voucher_for(kind, building.level)
        if entry is None:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        voucher, cost = entry
        if unit.backpack.count(voucher) < 1:
            if not allow_buy:
                return None  # 夜间不采购；留待白天买
            if turn.gold < cost:
                # 金币不足 → **保留任务**（不清掉），由采矿/卖货筹钱后再买；
                # 清任务会导致"每回合重派同一墙、永远完不成"（IKI9X2 复现）。
                return None
            want = self._voucher_qty(turn, unit, ctx, voucher, cost, kind)
            if want <= 0:
                return None
            return self._buy_item(turn, unit, ctx, voucher, want)
        if not allow_use:
            return None  # 白天只采购，升级/修复留到夜间
        if unit.pos != target and distance(unit.pos, target) <= 1:
            self.upgrade = None
            self.state = STATE_FREE
            return use_command(voucher, target)
        step = step_toward(turn, unit, target, ctx.reserved)
        if step is None:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        self.state = STATE_UPGRADE
        return self._move(step, ctx)

    def _stock_qty(self, turn: Turn, unit: Unit, ctx, item: str, want: int = 1) -> int:
        price = turn.shop_prices.get(item, 10)
        if price <= 0:
            return 0
        held = unit.backpack.count(item)
        need = max(0, want - held)
        if need <= 0:
            return 0
        room = (unit.capacity or 100) - len(unit.backpack)
        # 武器未 L2 时，修复包/墙券为武器券预留金币
        reserve = WEAPON_L1_COST if getattr(ctx, "weapon_need_l2", False) else 0
        afford = max(0, (turn.gold - reserve) // price)
        return max(0, min(need, room, afford))

    def _voucher_qty(self, turn: Turn, unit: Unit, ctx, voucher: str, cost: int, kind: str) -> int:
        """批量购买：min(刚需, 背包容量, 金币//单价)，扣除已持有（不多买）。"""
        lvl = 1 if voucher.endswith("1") else 2
        if kind == "wall":
            # 墙券只按**优先墙（正面+拐角）**的实际需求买（用户 2026-09-23：
            # "正面>拐角>侧面"，按升级需求，不多买无意义的券）
            _anchor = getattr(ctx, "layout_anchor", None)
            _front = getattr(ctx, "layout_front", None)
            if _anchor is not None:
                _prio = [
                    w for w in turn.walls()
                    if wall_rank(w.pos, _anchor, _front) in (WALL_FRONT, WALL_CORNER)
                ]
                need = min(WALL_VOUCHER_BATCH, sum(1 for w in _prio if w.level == lvl))
            else:
                need = min(WALL_VOUCHER_BATCH, sum(1 for w in turn.walls() if w.level == lvl))
        else:
            need = sum(1 for w in turn.weapons() if w.level == lvl)
        if kind == "wall":
            # 墙券由**修理工自己**使用 → 只算自己持有，否则开拓者/他人身上的券会让修理工
            # 以为"已有券"而不买、却用不了（IKI9XF：3 张券在别人身上、修理工干看着）
            held = unit.backpack.count(voucher)
        else:
            # 武器券共享（开拓者代买/使用）→ 全局统计，避免重复购买
            held = sum(u.backpack.count(voucher) for u in turn.ours)
        want = max(0, need - held)
        if want <= 0:
            return 0
        room = (unit.capacity or 100) - len(unit.backpack)
        # 武器未 L2 时，为武器券预留金币（不能因墙券导致炮台不升级）
        reserve = WEAPON_L1_COST if (kind == "wall" and getattr(ctx, "weapon_need_l2", False)) else 0
        afford = max(0, (turn.gold - reserve) // cost) if cost > 0 else 1
        return max(0, min(want, room, afford))

    def _buy_item(self, turn: Turn, unit: Unit, ctx, name: str, num: int) -> dict[str, Any] | None:
        shop = self._nearest_shop(turn, unit)
        if shop is None:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        if unit.pos != shop and distance(unit.pos, shop) <= 1:
            return buy_command(name, max(1, num))
        step = step_toward(turn, unit, shop, ctx.reserved)
        if step is None:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        return self._move(step, ctx)

    def _nearest_shop(self, turn: Turn, unit: Unit) -> Pos | None:
        shops = turn.shop_positions()
        if not shops:
            return None
        return min(shops, key=lambda s: (distance(unit.pos, s), s.x, s.y))

    # ---- 夜间修墙 ----
    def _upgrade_targets(self, turn: Turn, ctx) -> list:
        """夜间需升级的建筑（用户 2026-09-23）：
        墙（低于目标等级）+ 炮台（<L3）；**上一回合受损最重优先**。
        **另含满级/达标但紧急受损（<35%）的 L2+ 墙**——它们无法再升级，只能靠 WallFixer 回血
        （用户 IKI8HA 2026-09-24：L3 墙也要修）。
        返回 [(pos, level, ratio, is_weapon, target_level)]。
        """
        anchor = getattr(ctx, "layout_anchor", None)
        front = getattr(ctx, "layout_front", None)
        out: list = []
        for w in turn.walls():
            tgt = wall_target_level(w.pos, anchor, front)
            maxhp = WALL_MAX_HP[min(max(w.level, 1), 3) - 1]
            ratio = w.health / maxhp
            if w.level < tgt:
                out.append((w.pos, w.level, ratio, False, tgt))
            elif w.level >= 2 and ratio < CRITICAL_REPAIR_RATIO:
                out.append((w.pos, w.level, ratio, False, w.level))   # 只能 WallFixer
        for w in turn.weapons():
            if w.kind == "rocket" and w.level < 3:   # 只升火箭（用户 2026-09-23）
                maxhp = WEAPON_MAX_HP[min(max(w.level, 1), 3) - 1]
                out.append((w.pos, w.level, w.health / maxhp, True, 3))
        out.sort(key=lambda t: (t[2], 1 if t[3] else 0, t[0].x, t[0].y))
        return out

    def _critical_repair(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """紧急抢修（用户 IKI8HA 2026-09-24）：墙血 < 35% 满血的 **L2+ 墙（含满级 L3）** 最优先。

        用户："围墙已经 3 级了，不可以用升级券，就要降级用围墙修复包才对。"
        → 有可升级的券先用券（升级=回满血且升一级），否则用 WallFixer；只从**内侧**接近。
        """
        cands: list = []
        for w in turn.walls():
            if w.level < 2:
                continue   # L1 墙不用修复包（白天重建/用 Voucher1 升级即可）
            maxhp = WALL_MAX_HP[min(max(w.level, 1), 3) - 1]
            ratio = w.health / maxhp
            if ratio < CRITICAL_REPAIR_RATIO:
                cands.append((ratio, w.pos, w.level))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0], c[1].x, c[1].y))   # 不能直接比较 Pos（未定义排序）
        for _ratio, pos, level in cands:
            voucher = f"WallUpgradeVoucher{level}"
            if voucher in unit.backpack:
                item = voucher
            elif "WallFixer" in unit.backpack:
                item = "WallFixer"
            else:
                continue
            cmd = self._go_use(turn, unit, ctx, item, pos, inside_only=True)
            if cmd is not None:
                return cmd
        return None

    def _repair_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """夜间修复/升级（用户 2026-09-23 顺序）：

        第1步 `_upgrade_targets()` 取所有需升级的墙（受损最重优先）；
        第2步 优先用券：WallUpgradeVoucher → WeaponUpgradeVoucher；
        第3步 无对应升级券 → 才 fallback 到 WallFixer（仅墙）。
        炮台/目标围墙全满后 → 夜里尽量用 WallFixer 修受损墙。
        """
        for pos, level, _ratio, is_weapon, _tgt in self._upgrade_targets(turn, ctx):
            item = None
            if is_weapon:
                v = f"WeaponUpgradeVoucher{level}"
                if v in unit.backpack:
                    item = v
            else:
                v = f"WallUpgradeVoucher{level}"
                if v in unit.backpack:
                    item = v
            if item is None and not is_weapon and level >= 2 and "WallFixer" in unit.backpack:
                # 仅对**已受损(<50%)**的 L2+ 墙用包（用户 2026-09-23：满血的待升级墙不要用包，
                # 否则浪费经济、导致正面关键墙没包可修）
                if _ratio < REPAIR_HP_RATIO:
                    item = "WallFixer"
            if item is None:
                continue
            cmd = self._go_use(turn, unit, ctx, item, pos, inside_only=True)
            if cmd is not None:
                return cmd
        return self._wallfixer_repair(turn, unit, ctx)

    def _wallfixer_repair(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """受损墙（含满级 L3）→ WallFixer 修复。

        用户 2026-09-23：**L1 墙不用 WallFixer**（L1 被打掉白天重建 / 用 Voucher1 升级即可），
        只对 L2+ 的受损墙用包（回血），避免浪费。
        """
        if "WallFixer" not in unit.backpack:
            return None
        registry = getattr(ctx, "wall_registry", None)
        cands: list = []
        if registry is not None:
            for s in registry.damaged():
                if s.level >= 2:   # 跳过 L1
                    cands.append((s.pos, s.ratio, s.is_front))
        else:
            for w in turn.walls():
                if w.level < 2:
                    continue
                maxhp = WALL_MAX_HP[min(max(w.level, 1), 3) - 1]
                r = w.health / maxhp
                if r < REPAIR_HP_RATIO:
                    cands.append((w.pos, r, False))
        if not cands:
            return None
        cands.sort(key=lambda c: (0 if c[2] else 1, c[1], c[0].x, c[0].y))
        return self._go_use(turn, unit, ctx, "WallFixer", cands[0][0], inside_only=True)

    def _go_use(
        self, turn: Turn, unit: Unit, ctx, item: str, target: Pos, *, inside_only: bool = False
    ) -> dict[str, Any] | None:
        """走到 target 旁并使用 item（券/修复包）。

        inside_only=True（夜间，IKI77S）：只从**内侧**接近（距离基地锚点比目标更近的邻格），
        避免修理工为修开口侧墙而**走出墙外**（夜里危险）。
        """
        if unit.pos != target and distance(unit.pos, target) <= 1:
            self.state = STATE_REPAIR
            return use_command(item, target)
        if inside_only:
            anchor = getattr(ctx, "layout_anchor", None)
            anchor_pos = Pos(anchor[0], anchor[1]) if anchor is not None else None
            all_cells = [
                c for c in target.neighbours()
                if turn.land(c) and c not in turn.blocked(unit) and c not in ctx.reserved
            ]
            cells = list(all_cells)
            if anchor_pos is not None and all_cells:
                inside = [
                    c for c in all_cells
                    if distance(c, anchor_pos) < distance(target, anchor_pos)
                ]
                if inside:
                    cells = inside
            for group in (cells, all_cells):   # 内侧优先；内侧不可达才退全量（防卡死）
                group = sorted(group, key=lambda p: (distance(p, unit.pos), p.x, p.y))
                for c in group:
                    step = next_step(turn, unit, c, ctx.reserved) or next_step(turn, unit, c)
                    if step is not None:
                        self.state = STATE_REPAIR
                        return self._move(step, ctx)
                if group is all_cells:
                    break
            return None
        step = step_toward(turn, unit, target, ctx.reserved)
        if step is not None:
            self.state = STATE_REPAIR
            return self._move(step, ctx)
        return None

    # ---- 归位 ----
    def _path_home_len(self, turn: Turn, unit: Unit, ctx, home=None) -> int:
        """归位所需回合：优先用 A* 实际路径长度（切比雪夫距离会低估绕墙路程）。"""
        home = home if home is not None else ctx.home_anchor
        if home is None:
            return 0
        if unit.pos == home:
            return 0
        path = find_path(turn, unit, home, ctx.reserved)
        return len(path) - 1 if path else distance(unit.pos, home)

    def _go_home(self, turn: Turn, unit: Unit, ctx, home) -> dict[str, Any] | None:
        if home is None or unit.pos == home:
            return None
        self.state = STATE_RETURN
        # 归位目标格可能被占（如 CP 被开拓者占用）→ A* 视其为阻挡；退化到邻接格
        step = next_step(turn, unit, home, ctx.reserved)
        if step is None:
            step = step_toward(turn, unit, home, ctx.reserved)
        return self._move(step, ctx) if step is not None else None
