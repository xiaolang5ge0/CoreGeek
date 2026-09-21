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
    def plan(self, turn: Turn) -> list[UpgradeMission]:
        missions: list[UpgradeMission] = []
        budget = turn.gold - RESERVE_GOLD

        # 1. 武器：等级最低优先
        for weapon in sorted(turn.weapons(), key=lambda w: (w.level, w.unit_id)):
            entry = voucher_for("weapon", weapon.level)
            if entry is None:
                continue
            voucher, cost = entry
            if budget < cost:
                continue
            missions.append(UpgradeMission(voucher, cost, weapon.pos, "weapon", 10 + weapon.level))
            budget -= cost

        # 2. 受损墙（升级=回血），血量比例最低优先
        walls = sorted(
            (w for w in turn.walls() if 1 <= w.level <= 2),
            key=lambda w: w.health / WALL_MAX_HP[w.level - 1],
        )
        for wall in walls:
            ratio = wall.health / WALL_MAX_HP[wall.level - 1]
            if ratio >= WALL_DAMAGE_RATIO:
                continue
            entry = voucher_for("wall", wall.level)
            if entry is None:
                continue
            voucher, cost = entry
            if budget < cost:
                continue
            missions.append(UpgradeMission(voucher, cost, wall.pos, "wall", 20 + int(ratio * 10)))
            budget -= cost

        # 3. 基地：金币富余时
        station = turn.station()
        if station is not None and 1 <= station.level <= 2 and turn.gold >= RICH_GOLD:
            entry = voucher_for("station", station.level)
            if entry is not None:
                voucher, cost = entry
                if budget >= cost:
                    missions.append(UpgradeMission(voucher, cost, station.pos, "station", 30))
        return sorted(missions, key=lambda m: m.priority)
