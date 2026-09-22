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
from .planners.upgrade import WALL_MAX_HP, voucher_for
from .protocol import (
    MINE_TYPES,
    Pos,
    STATION,
    TOWER_TYPES,
    Turn,
    Unit,
    WALL,
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
SELL_NEAR_VENDOR_DIST = 3
SELL_NEAR_MIN_VALUE = 8
SELL_RICH_MIN_VALUE = 25
SELL_RICH_MAX_DIST = 20
SELL_FULL_RATIO = 0.6
STUCK_LIMIT = 5
STUCK_COLLECT_LIMIT = 3
WORKER_DANGER_DIST = 3      # 机器人贴脸(≤)才规避；脱离 +2 恢复
MAX_MINE_DIST = 16
REPAIR_MARGIN = 4           # 修理工归位余量（距天黑 ≤ 路径 + 4）
REPAIR_STONE_KEEP = 5       # 修理工常备石头（修墙用），低于此不卖
RETURN_STICKY_DAY = 4       # D4+ 修理工回防粘性：一旦开始返回，跨昼夜持续到进墙


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
        self.build_phase = False
        self.returning = False       # 回防粘性：D4+ 一旦开始返回，持续到进墙
        self.return_since = 0        # 开始返回的回合（防死循环兜底）

    # ================= 主入口 =================
    def decide(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if turn.is_day:
            self._evading = False
        if unit.pos != self._last_pos:
            self._stuck_moves = 0
        if len(unit.backpack) != self._last_bag:
            self._stuck_collects = 0
        self._last_pos = unit.pos
        self._last_bag = len(unit.backpack)

        # 0. 吃药（所有角色最高优先）
        cmd = self._medicine_cmd(turn, unit)
        if cmd is None:
            if self.role == ROLE_REPAIRER:
                cmd = self._repairer(turn, unit, ctx)
            else:
                cmd = self._miner(turn, unit, ctx)
        self._trace(ctx, cmd)
        return cmd

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
        if unit.health > 0.55 * cap:
            return None
        med = next((i for i in unit.backpack if i.lower() == "medicine"), None)
        if med:
            self.state = STATE_FREE
            return use_command(med)
        return None

    def _move(self, step: Pos, ctx) -> dict[str, Any] | None:
        self._stuck_moves += 1
        if self._stuck_moves >= STUCK_LIMIT:
            self._stuck_moves = 0
            ctx.note(self.unit_id, "unstuck")
            self.mine = None
            self.build = None
            self.sell_vendor = None
            self.upgrade = None
            self.state = STATE_FREE
            return None
        return move_command(step)

    # ================= 修理工 =================
    def _repairer(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        # 回防粘性（D4+）：一旦开始返回，跨昼夜持续到进墙（A* 绕墙，不穿墙）
        if self.returning:
            cmd = self._go_home(turn, unit, ctx, ctx.home_anchor)
            if cmd is not None:
                if turn.round_no - self.return_since > 40:  # 兜底：长期无法进墙则放弃
                    self.returning = False
                return cmd
            self.returning = False  # 已进墙/受阻 → 继续正常流程
        # 夜间优先：升级（=回血，省修复包）→ 抢修 → 无事才采矿
        if turn.is_night:
            cmd = self._upgrade_flow(turn, unit, ctx, allow_use=True, allow_buy=False)
            if cmd is not None:
                return cmd
            cmd = self._repair_cmd(turn, unit, ctx)
            if cmd is not None:
                return cmd
            if turn.day_index >= RETURN_STICKY_DAY:
                return None  # D4+ 夜：守内圈，不外出采矿
            return self._miner(turn, unit, ctx)
        # 白天
        if turn.day_index <= 1:
            # D1 全力石料 + 建墙
            return self._build_mine(turn, unit, ctx, prefer="stone")
        # D2+：建墙优先（无缺口）→ 采购 → 回防预留 → 采矿(铜/铁)
        near_dusk = (
            0 < turn.rounds_until_night
            <= self._path_home_len(turn, unit, ctx) + REPAIR_MARGIN
        )
        if near_dusk:
            if turn.day_index >= RETURN_STICKY_DAY:
                self.returning = True
                self.return_since = turn.round_no
            cmd = self._go_home(turn, unit, ctx, ctx.home_anchor)
            if cmd is not None:
                return cmd
            self.returning = False
            return self._build_mine(turn, unit, ctx, prefer="money")  # 已归位/受阻 → 就近
        cmd = self._upgrade_flow(turn, unit, ctx, allow_use=False, allow_buy=True)
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
        # 危险规避（滞回，非粘性）
        cmd = self._evade_cmd(turn, unit, ctx)
        if cmd is not None or self._evading:
            return cmd
        return self._mine_flow(turn, unit, ctx, prefer="money")

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
            if self.mine not in turn.mines():
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
                ctx.note(self.unit_id, "mine_unreachable")
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
        return self._mine_cmd(turn, unit, ctx)

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
        step = step_toward(turn, unit, self.mine, ctx.reserved)
        if step is None:
            return None
        return self._move(step, ctx)

    def _select_mine(self, turn: Turn, unit: Unit, ctx, *, prefer: str) -> Pos | None:
        other_locks = () if ctx.share_mines else ctx.other_mine_locks(self.unit_id)
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
        for pos, kind, dist, our_dist in pool:
            side = 12.0 if (enemy_station is not None
                            and distance(enemy_station.pos, pos) < our_dist) else 0.0
            if prefer == "stone":
                key = (dist + our_dist * 0.6 + side, dist, pos.x, pos.y)
            else:
                price = turn.vendor_prices.get(kind, 1)
                # 新闻囤货：被预测涨价的矿种优先级提高
                boost = ctx.price_boost(kind) if hasattr(ctx, "price_boost") else 0.0
                key = (dist - price * 10 - boost + our_dist * 0.6 + side, dist, pos.x, pos.y)
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
                if ctx.walls_left > 0:
                    continue  # 墙料不外流
                count = max(0, count - self._stone_keep())
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
                if ctx.walls_left > 0:
                    continue
                count = max(0, count - self._stone_keep())
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
    def _evade_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if not turn.is_night:
            return None
        nearest = min(
            (distance(unit.pos, r.pos) for r in turn.robots
             if r.alive and r.target_team in ("", turn.team_type)),
            default=99,
        )
        if nearest <= WORKER_DANGER_DIST:
            self._evading = True
        elif nearest >= WORKER_DANGER_DIST + 2:
            self._evading = False
        if not self._evading:
            return None
        self.mine = None
        self.build = None
        self.sell_vendor = None
        if getattr(ctx, "base_in_danger", False):
            cell = getattr(ctx, "safe_anchor", None)
            if cell is not None and unit.pos != cell:
                self.state = STATE_CRITICAL
                step = next_step(turn, unit, cell, ctx.reserved)
                if step is not None:
                    return self._move(step, ctx)
            self.state = STATE_CRITICAL
            return None
        self.state = STATE_EVADE
        step = self._flee_step(turn, unit, ctx)
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
            want = self._stock_qty(turn, unit, item, qty)
            if want <= 0:
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
                self.upgrade = None
                self.state = STATE_FREE
                return None
            want = self._voucher_qty(turn, unit, voucher, cost, kind)
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

    def _stock_qty(self, turn: Turn, unit: Unit, item: str, want: int = 1) -> int:
        price = turn.shop_prices.get(item, 10)
        if price <= 0:
            return 0
        held = unit.backpack.count(item)
        need = max(0, want - held)
        if need <= 0:
            return 0
        room = (unit.capacity or 100) - len(unit.backpack)
        afford = turn.gold // price
        return max(0, min(need, room, afford))

    def _voucher_qty(self, turn: Turn, unit: Unit, voucher: str, cost: int, kind: str) -> int:
        """批量购买：min(刚需, 背包容量, 金币//单价)，扣除已持有（不多买）。"""
        lvl = 1 if voucher.endswith("1") else 2
        if kind == "wall":
            need = sum(1 for w in turn.walls() if w.level == lvl)
        else:
            need = sum(1 for w in turn.weapons() if w.level == lvl)
        held = unit.backpack.count(voucher)
        want = max(0, need - held)
        if want <= 0:
            return 0
        room = (unit.capacity or 100) - len(unit.backpack)
        afford = turn.gold // cost if cost > 0 else 1
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
    def _repair_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """夜间抢修（用户补充）：血量 <50% 才修；正面优先。

        优先级：未满级(L1/L2) 优先用升级券（升级=回满血+提升上限）；
        满级(L3) 升级券失效 → 只能用 WallFixer（修复包）。
        """
        registry = getattr(ctx, "wall_registry", None)
        candidates: list[tuple[Pos, int, bool, float]] = []
        if registry is not None:
            for state in registry.damaged():
                candidates.append((state.pos, state.level, state.is_front, state.ratio))
        else:
            for wall in turn.walls():
                max_hp = WALL_MAX_HP[min(max(wall.level, 1), 3) - 1]
                ratio = wall.health / max_hp
                if ratio < REPAIR_HP_RATIO:
                    candidates.append((wall.pos, wall.level, False, ratio))
        if not candidates:
            return None  # 无达标修复需求 → 落回（修理工夜间无威胁时外出采矿）
        # 正面优先，其次血最少
        candidates.sort(key=lambda c: (0 if c[2] else 1, c[3], c[0].x, c[0].y))
        target, level, _is_front, _ratio = candidates[0]
        item = None
        if level <= 2:
            voucher = f"WallUpgradeVoucher{level}"
            if voucher in unit.backpack:
                item = voucher
        if item is None and "WallFixer" in unit.backpack:
            item = "WallFixer"
        if item is None:
            return None
        if unit.pos != target and distance(unit.pos, target) <= 1:
            self.state = STATE_REPAIR
            return use_command(item, target)
        step = step_toward(turn, unit, target, ctx.reserved)
        if step is not None:
            self.state = STATE_REPAIR
            return self._move(step, ctx)
        return None

    # ---- 归位 ----
    def _path_home_len(self, turn: Turn, unit: Unit, ctx) -> int:
        """归位所需回合：优先用 A* 实际路径长度（切比雪夫距离会低估绕墙路程）。"""
        home = ctx.home_anchor
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
