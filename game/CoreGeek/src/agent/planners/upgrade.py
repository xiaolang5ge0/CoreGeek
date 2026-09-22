"""L3 UpgradePlanner：升级任务规划（STRATEGY_DECISIONS #13：武器>墙>基地）。

要点：
- 升级 = 回满血 → 受损墙/武器优先用升级代替修理（性价比高于 WallFixer）。
- 预算：保留 RESERVE 应急金，其余才可用于升级。
- 武器：等级最低优先（轮转下均匀提升）；墙：血量比例 <60% 的受损墙优先；
  基地：仅当金币富余（≥RICH_GOLD）时。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..protocol import Turn

RESERVE_GOLD = 30      # 应急金（炸弹/修墙包）
RICH_GOLD = 250        # 基地升级门槛
WALL_DAMAGE_RATIO = 0.6
CRITICAL_WALL_RATIO = 0.3  # 武器未到 L2 时，仅临界受损墙才修（其余攒钱升塔）

WALL_MAX_HP = (1000, 1500, 2000)
WEAPON_MAX_HP = (1000, 1500, 2000)
STATION_MAX_HP = (1500, 3000, 4500)

VOUCHER = {
    ("weapon", 1): ("WeaponUpgradeVoucher1", 100),
    ("weapon", 2): ("WeaponUpgradeVoucher2", 150),
    ("wall", 1): ("WallUpgradeVoucher1", 20),
    ("wall", 2): ("WallUpgradeVoucher2", 30),
    ("station", 1): ("StationUpgradeVoucher1", 100),
    ("station", 2): ("StationUpgradeVoucher2", 150),
}


@dataclass(frozen=True, slots=True)
class UpgradeMission:
    voucher: str
    cost: int
    target: tuple  # Pos
    kind: str      # weapon / wall / station
    priority: int  # 小=高


def voucher_for(kind: str, level: int) -> tuple[str, int] | None:
    """按目标建筑当前等级给券（L1→Voucher1，L2→Voucher2，L3 None）。"""
    return VOUCHER.get((kind, level))


class UpgradePlanner:
    """升级优先序（用户指定）：武器优先整体推进，墙按受损/FRONT 插队。
    武器L2 → 受损墙(FRONT优先) → FRONT方向墙L2 → 武器L3 → 墙全L2 → 墙L3 → 基地 → Day3+备货。
    约束：墙 L3 门控——仍有 L1 墙时不升任何墙到 L3（杜绝相邻 L3/L1/L1）。
    FRONT = 离控制点 CP 最远的一侧（迎敌面）。
    """

    def plan(self, turn: Turn, cp=None) -> list[UpgradeMission]:
        missions: list[UpgradeMission] = []
        budget = turn.gold - RESERVE_GOLD

        def add(voucher, cost, target, kind, priority):
            nonlocal budget
            if budget < cost:
                return False
            missions.append(UpgradeMission(voucher, cost, target, kind, priority))
            budget -= cost
            return True

        from ..protocol import distance as _dist

        def dist_cp(w):
            return _dist(w.pos, cp) if cp is not None else 0

        def front_first(walls):
            """迎敌面（离 CP 最远）优先。"""
            return sorted(walls, key=lambda w: (-dist_cp(w), w.pos.x, w.pos.y))

        def ratio(w):
            return w.health / WALL_MAX_HP[min(max(w.level, 1), 3) - 1]

        weapons = sorted(turn.weapons(), key=lambda w: (w.level, w.unit_id))
        all_walls = list(turn.walls())
        any_l1_wall = any(w.level == 1 for w in all_walls)
        # 武器未到 L2 → 为武器券(100金)预留金币：暂停墙升级（仅修临界受损墙），避免廉价墙券吃光金币
        weapons_need_l2 = any(w.level < 2 for w in weapons)
        # FRONT 方向墙 = 离 CP 最远的一半（迎敌面）
        ordered = front_first(all_walls)
        front_walls = set(id(w) for w in ordered[: max(1, len(ordered) // 2)])

        # 1. 武器全部 L2（最高优先）
        for w in weapons:
            if w.level == 1:
                v, c = voucher_for("weapon", 1)
                add(v, c, w.pos, "weapon", 10)
        # 2. 受损墙 → 升级回血。武器未到 L2 时仅修临界受损(ratio<0.3)，其余攒钱升武器
        repair_line = WALL_DAMAGE_RATIO if not weapons_need_l2 else CRITICAL_WALL_RATIO
        damaged = sorted(
            (w for w in all_walls if w.level == 1 and ratio(w) < repair_line),
            key=lambda w: (ratio(w), -dist_cp(w)),
        )
        for wall in damaged:
            v, c = voucher_for("wall", 1)
            add(v, c, wall.pos, "wall", 20)
        # 3. FRONT 方向健康墙 L1→L2 —— 仅当武器已全部 L2（否则金币留给武器）
        if not weapons_need_l2:
            for wall in front_first([w for w in all_walls if w.level == 1 and id(w) in front_walls]):
                v, c = voucher_for("wall", 1)
                add(v, c, wall.pos, "wall", 25)
        # 4. 武器 L3
        for w in weapons:
            if w.level == 2:
                v, c = voucher_for("weapon", 2)
                add(v, c, w.pos, "weapon", 30)
        # 5. 墙全 L2（剩余非 FRONT 墙）—— 同样仅在武器已全部 L2 后
        if not weapons_need_l2:
            for wall in front_first([w for w in all_walls if w.level == 1]):
                v, c = voucher_for("wall", 1)
                add(v, c, wall.pos, "wall", 35)
        # 6. 墙 L3 —— 门控：仍有 L1 墙时不升 L3（杜绝相邻 L3/L1/L1）
        if not any_l1_wall:
            for wall in front_first([w for w in all_walls if w.level == 2]):
                v, c = voucher_for("wall", 2)
                add(v, c, wall.pos, "wall", 40)
        # 7. 基地：金币富余时
        station = turn.station()
        if station is not None and 1 <= station.level <= 2 and turn.gold >= RICH_GOLD:
            v, c = voucher_for("station", station.level)
            add(v, c, station.pos, "station", 45)
        # 8. Day3+（BOSS 夜）备货 WallFixer
        if turn.day_index >= 3 and turn.gold >= RESERVE_GOLD + 60:
            has_fixer = any(
                "WallFixer" in u.backpack
                for u in turn.ours
                if u.kind in ("worker", "pioneer")
            )
            if not has_fixer:
                missions.append(UpgradeMission("WallFixer", 10, None, "stock", 50))
        return sorted(missions, key=lambda m: m.priority)
