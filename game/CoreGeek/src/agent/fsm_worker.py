"""L4 WorkerFSM：FSM + Goal Commitment + Task Lock。

主链：ECONOMIC_FREE → 选矿 → MINE_COLLECT_LOCKED → 矿耗尽 → ECONOMIC_FREE
支链：BUILD（白天，brain 分配，优先级最高）
      SELL（仅 FREE 态评估/背包满触发，**绝不抢占采矿锁** —— STRATEGY_DECISIONS #11）
看门狗：连续移动意图但位置未变 → UNSTUCK，清空目标下轮重选（防原地打转）。
CRITICAL_DEFENSE 抢关在 P3 接入。
"""
from __future__ import annotations

from typing import Any

from .path import next_step, step_toward
from .planners.upgrade import voucher_for
from .protocol import (
    MINE_TYPES,
    Pos,
    TOWER_TYPES,
    Turn,
    Unit,
    WALL,
    STATION,
    build_command,
    buy_command,
    collect_command,
    distance,
    move_command,
    sell_command,
    use_command,
)

ORE_STONE = "ore_stone"
ORE_MONEY = "ore_money"

STATE_FREE = "ECONOMIC_FREE"
STATE_BUILD = "BUILD"
STATE_MINE_LOCKED = "MINE_COLLECT_LOCKED"
STATE_SELL = "SELL"
STATE_CRITICAL = "CRITICAL_DEFENSE"
STATE_EVADE = "EVADE"
STATE_UPGRADE = "UPGRADE"

EVADE_DIST = 2  # 非眩晕机器人贴近此距离即撤离

# 墙料批量阈值：攒够即去建墙，摊薄往返路费（入夜前紧急时 1 块也建）
STONE_BATCH = 4
DUSK_URGENT_ROUNDS = 12

# 机会性卖货阈值（STRATEGY_DECISIONS #11：看矿点与小贩相对位置）
SELL_NEAR_VENDOR_DIST = 3   # 小贩近在咫尺
SELL_NEAR_MIN_VALUE = 8     # 顺路最低货值（金）
SELL_RICH_MIN_VALUE = 25    # 值得专程跑一趟的货值
SELL_RICH_MAX_DIST = 15     # 专程跑的最大距离

STUCK_LIMIT = 5             # 连续移动意图但位置未变的上限


class WorkerFSM:
    def __init__(self, unit_id: int):
        self.unit_id = unit_id
        self.state = STATE_FREE
        self.ore_role = ORE_MONEY
        self.mine: Pos | None = None
        self.build: tuple[Pos, str] | None = None
        self.sell_vendor: Pos | None = None
        self.upgrade: tuple[Pos, str] | None = None  # (目标建筑格, 种类)
        self._stuck_moves = 0
        self._last_pos: Pos | None = None

    # ---- 主入口 ----
    def decide(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if unit.pos != self._last_pos:
            self._stuck_moves = 0
        self._last_pos = unit.pos
        cmd = self._decide(turn, unit, ctx)
        ctx.trace["workers"][str(self.unit_id)] = {
            "state": self.state,
            "ore_role": self.ore_role,
            "mine": self.mine.dump() if self.mine else None,
            "build": [self.build[0].dump(), self.build[1]] if self.build else None,
            "sell": self.sell_vendor.dump() if self.sell_vendor else None,
            "upgrade": [self.upgrade[0].dump(), self.upgrade[1]] if self.upgrade else None,
            "cmd": cmd.get("action") if cmd else None,
        }
        return cmd

    def _decide(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        # 0a. 夜间 CRITICAL_DEFENSE 召回（最高优先，可抢占一切经济行为）
        if turn.is_night and getattr(ctx, "threat_level", "SAFE") == "CRITICAL":
            cell = ctx.recall_cell(self.unit_id)
            if cell is not None and unit.pos != cell:
                self.mine = None
                self.build = None
                self.sell_vendor = None
                self.state = STATE_CRITICAL
                step = next_step(turn, unit, cell, ctx.reserved)
                if step is not None:
                    return self._move(step, ctx)
            return None  # 到位待命
        # 0b. 近身机器人闪避（安全 > 矿锁）
        home = getattr(ctx, "home_anchor", None)
        if home is not None and distance(unit.pos, home) > EVADE_DIST:
            close_robot = any(
                r.alive and not r.dizzy
                and r.target_team in ("", turn.team_type)
                and distance(r.pos, unit.pos) <= EVADE_DIST
                for r in turn.robots
            )
            if close_robot:
                self.mine = None
                self.sell_vendor = None
                self.state = STATE_EVADE
                # 逃向 CP 邻域（CP 本身被开拓者占用，目标为其邻接格）
                step = step_toward(turn, unit, home, ctx.reserved)
                if step is not None:
                    return self._move(step, ctx)
        # 1. 建造任务（白天，优先级最高）
        if self.build is not None:
            if turn.is_night:
                self.build = None
            else:
                self.state = STATE_BUILD
                return self._build_cmd(turn, unit, ctx)
        # 1b. 升级任务（仅白天；夜间挂起，次日自动继续）
        if self.upgrade is not None and turn.is_day:
            self.state = STATE_UPGRADE
            return self._upgrade_cmd(turn, unit, ctx)
        # 2. 背包满 → 卖矿（解除矿锁，卖完重选，可能回到同一矿）
        if unit.backpack_full:
            if self.mine is not None:
                ctx.note(self.unit_id, "release_lock_backpack_full")
                self.mine = None
            return self._sell_chain(turn, unit, ctx, mandatory=True)
        # 3. 采矿锁（SELL 不得抢占 —— 采完再走）
        if self.mine is not None:
            if self.mine not in turn.mines():
                ctx.note(self.unit_id, "mine_depleted")
                self.mine = None
            elif ctx.is_mine_blocked(self.mine, turn.round_no):
                ctx.note(self.unit_id, "mine_blocked_news")
                self.mine = None
            else:
                self.state = STATE_MINE_LOCKED
                cmd = self._mine_cmd(turn, unit, ctx)
                if cmd is not None:
                    return cmd
                ctx.note(self.unit_id, "mine_unreachable")
                self.mine = None
        # 4. FREE 态：机会性卖货评估
        self.state = STATE_FREE
        if self._should_sell(turn, unit, ctx):
            return self._sell_chain(turn, unit, ctx, mandatory=False)
        # 5. 选矿
        self.mine = self._select_mine(turn, unit, ctx)
        if self.mine is None:
            ctx.note(self.unit_id, "no_mine")
            return None
        self.state = STATE_MINE_LOCKED
        return self._mine_cmd(turn, unit, ctx)

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

    # ---- 升级任务链：走到商店买券 → 走到目标建筑用券 ----
    def _upgrade_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        target, kind = self.upgrade
        building = next(
            (u for u in turn.ours if u.pos == target and u.kind in (*TOWER_TYPES, WALL, STATION)),
            None,
        )
        if building is None or building.level >= 3:
            ctx.note(self.unit_id, "upgrade_target_gone")
            self.upgrade = None
            self.state = STATE_FREE
            return None
        entry = voucher_for(kind, building.level)
        if entry is None:
            self.upgrade = None
            self.state = STATE_FREE
            return None
        voucher, cost = entry
        if voucher not in unit.backpack:
            if turn.gold < cost:
                ctx.note(self.unit_id, "upgrade_gold_short")
                self.upgrade = None
                self.state = STATE_FREE
                return None
            shop = self._nearest_shop(turn, unit)
            if shop is None:
                ctx.note(self.unit_id, "no_shop")
                self.upgrade = None
                self.state = STATE_FREE
                return None
            if unit.pos != shop and distance(unit.pos, shop) <= 1:
                return buy_command(voucher)
            step = step_toward(turn, unit, shop, ctx.reserved)
            if step is None:
                ctx.note(self.unit_id, "shop_unreachable")
                self.upgrade = None
                self.state = STATE_FREE
                return None
            return self._move(step, ctx)
        # 券已入手 → 走到目标旁使用
        if unit.pos != target and distance(unit.pos, target) <= 1:
            self.upgrade = None
            self.state = STATE_FREE
            return use_command(voucher, target)
        step = step_toward(turn, unit, target, ctx.reserved)
        if step is None:
            ctx.note(self.unit_id, "upgrade_target_unreachable")
            self.upgrade = None
            self.state = STATE_FREE
            return None
        return self._move(step, ctx)

    def _nearest_shop(self, turn: Turn, unit: Unit) -> Pos | None:
        shops = turn.shop_positions()
        if not shops:
            return None
        return min(shops, key=lambda s: (distance(unit.pos, s), s.x, s.y))

    # ---- 采矿 ----
    def _mine_cmd(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        if unit.pos != self.mine and distance(unit.pos, self.mine) <= 1:
            return collect_command(self.mine)
        step = step_toward(turn, unit, self.mine, ctx.reserved)
        if step is None:
            return None
        return self._move(step, ctx)

    def _select_mine(self, turn: Turn, unit: Unit, ctx) -> Pos | None:
        other_locks = ctx.other_mine_locks(self.unit_id)
        best: Pos | None = None
        best_key = None
        for pos in turn.mines():
            if pos in other_locks:
                continue
            if ctx.is_mine_blocked(pos, turn.round_no):
                continue
            kind = turn.zones.get(pos)
            if self.ore_role == ORE_STONE and kind != "stone":
                continue
            dist = distance(unit.pos, pos)
            if self.ore_role == ORE_STONE:
                key = (dist, pos.x, pos.y)
            else:
                price = turn.vendor_prices.get(kind, 1)
                # 价高优先、距离次要：约 10 格路程 ≈ 2 金差价
                key = (dist - price * 10, dist, pos.x, pos.y)
            if best is None or key < best_key:
                best, best_key = pos, key
        return best

    # ---- 卖货 ----
    def _sell_chain(
        self, turn: Turn, unit: Unit, ctx, *, mandatory: bool
    ) -> dict[str, Any] | None:
        vendor = self._nearest_vendor(turn, unit)
        if vendor is None:
            ctx.note(self.unit_id, "no_vendor")
            return None
        self.sell_vendor = vendor
        if unit.pos != vendor and distance(unit.pos, vendor) <= 1:
            cmd = self._sell_best(turn, unit, ctx)
            if cmd is None:
                self.sell_vendor = None
                self.state = STATE_FREE
                return None
            self.state = STATE_SELL
            return cmd
        if mandatory is False and self.state != STATE_SELL:
            self.state = STATE_FREE  # 尚未出发，保持 FREE 语义
        step = step_toward(turn, unit, vendor, ctx.reserved)
        if step is None:
            ctx.note(self.unit_id, "vendor_unreachable")
            return None
        self.state = STATE_SELL
        return self._move(step, ctx)

    def _sell_best(self, turn: Turn, unit: Unit, ctx) -> dict[str, Any] | None:
        """单次只能卖一种矿：选 数量×单价 最高的种类批量卖。"""
        best: tuple[int, str] | None = None
        for ore in MINE_TYPES:
            if ore == "stone" and ctx.walls_missing:
                continue  # 墙料不外流
            count = unit.backpack.count(ore)
            if count == 0:
                continue
            value = count * turn.vendor_prices.get(ore, 1)
            if best is None or value > best[0]:
                best = (value, ore)
        if best is None:
            return None
        _, ore = best
        return sell_command(ore, unit.backpack.count(ore))

    def _should_sell(self, turn: Turn, unit: Unit, ctx) -> bool:
        value = self._sellable_value(turn, unit, ctx)
        if value <= 0:
            return False
        if ctx.need_gold:
            return True  # 急用金（重建武器等）
        vendor = self._nearest_vendor(turn, unit)
        if vendor is None:
            return False
        dist = distance(unit.pos, vendor)
        if dist <= SELL_NEAR_VENDOR_DIST and value >= SELL_NEAR_MIN_VALUE:
            return True  # 顺路
        if value >= SELL_RICH_MIN_VALUE and dist <= SELL_RICH_MAX_DIST:
            return True  # 值得专程
        return False

    def _sellable_value(self, turn: Turn, unit: Unit, ctx) -> int:
        value = 0
        for ore in MINE_TYPES:
            if ore == "stone" and ctx.walls_missing:
                continue
            value += unit.backpack.count(ore) * turn.vendor_prices.get(ore, 1)
        return value

    def _nearest_vendor(self, turn: Turn, unit: Unit) -> Pos | None:
        vendors = turn.vendor_positions()
        if not vendors:
            return None
        return min(vendors, key=lambda v: (distance(unit.pos, v), v.x, v.y))

    # ---- 看门狗 ----
    def _move(self, step: Pos, ctx) -> dict[str, Any] | None:
        self._stuck_moves += 1
        if self._stuck_moves >= STUCK_LIMIT:
            self._stuck_moves = 0
            ctx.note(self.unit_id, "unstuck")
            self.mine = None
            self.build = None
            self.sell_vendor = None
            self.state = STATE_FREE
            return None
        return move_command(step)
