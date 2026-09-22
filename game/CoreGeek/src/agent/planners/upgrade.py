"""L3 UpgradePlanner：升级任务规划（STRATEGY_DECISIONS #13：武器>墙>基地）。

要点：
- 升级 = 回满血 → 受损墙/武器优先用升级代替修理（性价比高于 WallFixer）。
- 预算：保留 RESERVE 应急金，其余才可用于升级。
- 武器：等级最低优先（轮转下均匀提升）；墙：血量比例 <60% 的受损墙优先；
  基地：仅当金币富余（≥RICH_GOLD）时。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..protocol import STATION_MAX_HP, Turn, WALL_MAX_HP, WEAPON_MAX_HP

RESERVE_GOLD = 30      # 应急金（炸弹/修墙包）
RICH_GOLD = 250        # 基地升级门槛
WALL_DAMAGE_RATIO = 0.6
CRITICAL_WALL_RATIO = 0.3  # 武器未到 L2 时，仅临界受损墙才修（其余攒钱升塔）
WALL_MIN_L2 = 6            # 武器升 L3 前，先升的最小墙量（正面+侧面转角，约 6 块）
FIXER_STOCK_MAX = 4        # WallFixer 备货上限（金币紧缺时维持 4；全升满后不设上限）
FIXER_STOCK_MAXED = 8      # 武器+墙全 L3 后：不设上限（有余钱就多备）
FRONT_L2_TARGET = 5       # 正面墙 L2 死线数量（D3 入夜前，用户 2026-09-23）
FRONT_L3_TARGET = 5       # 正面墙 L3 死线数量（D5 入夜前）
FRONT_STOCK_TARGET = 5    # 正面墙对应券/修复包备货数量（D4+，没钱则不要求）


def wall_hp_threshold(day: int) -> int:
    """墙修复/插队动态阈值（D3，外部策略）：max(100, (day+1)×100)，随天数递增。"""
    return max(100, (day + 1) * 100)


# 墙环分级（任务2）：0=正面(迎敌侧) > 1=拐角 > 2=侧面（用户指定升级顺序）
WALL_FRONT = 0
WALL_CORNER = 1
WALL_SIDE = 2
_RING_LO, _RING_HI = -2, 3


def wall_rank(pos, anchor, front: str | None) -> int:
    """按墙环位置分级：正面(迎敌侧) → 拐角 → 侧面。

    front = 开口侧（背向敌人）；敌人方向 = front 的反向。以基地锚点 (xmin,ymin) 为原点，
    墙环偏移 dx,dy ∈ [-2,3]。正面 = 敌人方向的极值列/行；拐角 = 正面两端；
    其余（开口两侧的翼排） = 侧面。
    """
    dx = pos.x - anchor[0]
    dy = pos.y - anchor[1]
    if front == "W":
        prim, sec, pext = dx, dy, _RING_HI
    elif front == "E":
        prim, sec, pext = dx, dy, _RING_LO
    elif front == "N":
        prim, sec, pext = dy, dx, _RING_LO
    else:  # S / None
        prim, sec, pext = dy, dx, _RING_HI
    at_prim = prim == pext
    at_sec = sec in (_RING_LO, _RING_HI)
    if at_prim and at_sec:
        return WALL_CORNER
    if at_prim:
        return WALL_FRONT
    return WALL_SIDE

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
    kind: str      # weapon / wall / station / stock
    priority: int  # 小=高
    qty: int = 1   # stock 备货目标数量（升级类恒为 1）


def voucher_for(kind: str, level: int) -> tuple[str, int] | None:
    """按目标建筑当前等级给券（L1→Voucher1，L2→Voucher2，L3 None）。"""
    return VOUCHER.get((kind, level))


class UpgradePlanner:
    """升级优先序（用户指定）：武器优先整体推进，墙按受损/FRONT 插队。
    武器L2 → 受损墙(FRONT优先) → FRONT方向墙L2 → 武器L3 → 墙全L2 → 墙L3 → 基地 → Day3+备货。
    约束：墙 L3 门控——仍有 L1 墙时不升任何墙到 L3（杜绝相邻 L3/L1/L1）。
    FRONT = 离控制点 CP 最远的一侧（迎敌面）。
    """

    def plan(self, turn: Turn, cp=None, registry=None, front: str | None = None) -> list[UpgradeMission]:
        missions: list[UpgradeMission] = []
        budget = turn.gold - RESERVE_GOLD
        station0 = turn.station()
        anchor = (station0.pos.x, station0.pos.y - 1) if station0 is not None else None

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
        # FRONT 方向墙 = 离 CP 最远的一半（迎敌面）；有 WallRegistry 时用其标注的正面
        # rebuilt_pos = 前夜被攻破、次日补建的墙（回到 L1，必须重新纳入升级队列）
        rebuilt_pos: set = set()
        if registry is not None and any(s.is_front for s in registry.walls.values()):
            front_pos = {s.pos for s in registry.walls.values() if s.is_front}
            front_walls = {id(w) for w in all_walls if w.pos in front_pos}
            rebuilt_pos = {
                s.pos for s in registry.walls.values()
                if s.exists and s.rebuilt_round > 0 and s.level < 3
            }
        else:
            ordered = front_first(all_walls)
            front_walls = set(id(w) for w in ordered[: max(1, len(ordered) // 2)])

        def rank(w):
            return wall_rank(w.pos, anchor, front) if anchor is not None else 0

        def front_order(walls):
            """升级顺序（任务2）：正面 → 拐角 → 侧面；同级补建墙优先、低血优先。"""
            return sorted(
                walls,
                key=lambda w: (
                    rank(w),
                    0 if w.pos in rebuilt_pos else 1,
                    ratio(w),
                    -dist_cp(w), w.pos.x, w.pos.y,
                ),
            )

        # 正面墙防御死线（用户 2026-09-23）：D3 入夜前正面≥5 面 L2；D5 入夜前正面≥5 面 L3。
        # 正面 = 迎敌侧整列（含两端拐角，共 ~6 格），保证目标 5 可达。
        front_wall_units = [w for w in all_walls if rank(w) in (WALL_FRONT, WALL_CORNER)]
        front_l2_n = sum(1 for w in front_wall_units if w.level >= 2)
        front_l3_n = sum(1 for w in front_wall_units if w.level >= 3)
        # 6a. 正面 L1→L2 死线（D2-D3 提前冲，保证扛住 D3 夜）—— 优先级 12（武器 L2 之后）
        if not weapons_need_l2 and 2 <= turn.day_index <= 3 and front_l2_n < FRONT_L2_TARGET:
            need = FRONT_L2_TARGET - front_l2_n
            for wall in front_order([w for w in front_wall_units if w.level == 1])[:need]:
                v, c = voucher_for("wall", 1)
                add(v, c, wall.pos, "wall", 12)
        # 6b. 正面 L2→L3 死线（D4-D5 冲，保证扛住 D5 夜）—— 优先级 12（武器 L2 之后）
        if not weapons_need_l2 and 4 <= turn.day_index <= 5 and front_l3_n < FRONT_L3_TARGET:
            need = FRONT_L3_TARGET - front_l3_n
            for wall in front_order([w for w in front_wall_units if w.level == 2])[:need]:
                v, c = voucher_for("wall", 2)
                add(v, c, wall.pos, "wall", 12)

        # 0. 【插队 D3】墙血低于动态阈值 max(100,(day+1)×100) → 优先修复/升级（可插武器队）
        #    L1→Voucher1、L2→Voucher2（正面 L2 也能升 L3 回血，用户补充）。
        crit = front_order(
            [w for w in all_walls
             if w.level in (1, 2) and w.health < wall_hp_threshold(turn.day_index)]
        )
        for wall in crit:
            v, c = voucher_for("wall", wall.level)
            if v is not None:
                add(v, c, wall.pos, "wall", 5)
        # 1. 武器全部 L2（常规最高优先）
        for w in weapons:
            if w.level == 1:
                v, c = voucher_for("weapon", 1)
                add(v, c, w.pos, "weapon", 10)
        # 2. 受损墙 → 升级回血。武器未到 L2 时仅修临界受损(ratio<0.3)，其余攒钱升武器
        repair_line = WALL_DAMAGE_RATIO if not weapons_need_l2 else CRITICAL_WALL_RATIO
        damaged = front_order(
            [w for w in all_walls if w.level == 1 and ratio(w) < repair_line]
        )
        for wall in damaged:
            v, c = voucher_for("wall", 1)
            add(v, c, wall.pos, "wall", 20)
        # 3. 墙 L2（任务2 顺序：正面→拐角→侧面）—— 仅当武器已全部 L2；
        #    先升最小量 WALL_MIN_L2（≈正面4+拐角2），之后优先把武器升到 L3。
        if not weapons_need_l2:
            n = 0
            for wall in front_order([w for w in all_walls if w.level == 1]):
                if n >= WALL_MIN_L2:
                    break
                v, c = voucher_for("wall", 1)
                if add(v, c, wall.pos, "wall", 25):
                    n += 1
        # 4. 武器 L3
        for w in weapons:
            if w.level == 2:
                v, c = voucher_for("weapon", 2)
                add(v, c, w.pos, "weapon", 30)
        # 5. 墙全 L2（剩余非 FRONT 墙）—— 同样仅在武器已全部 L2 后
        if not weapons_need_l2:
            for wall in front_order([w for w in all_walls if w.level == 1]):
                v, c = voucher_for("wall", 1)
                add(v, c, wall.pos, "wall", 35)
        # 6. 墙 L3 —— 门控：仍有 L1 墙时不升 L3（杜绝相邻 L3/L1/L1）
        if not any_l1_wall:
            for wall in front_order([w for w in all_walls if w.level == 2]):
                v, c = voucher_for("wall", 2)
                add(v, c, wall.pos, "wall", 40)
        # 7. 基地：**仅当武器+墙全 L3**（D4：暂不升基地，全满后才考虑）
        station = turn.station()
        all_weapons_l3 = bool(weapons) and all(w.level >= 3 for w in weapons)
        all_walls_l3 = bool(all_walls) and all(w.level >= 3 for w in all_walls)
        if (
            station is not None and 1 <= station.level <= 2
            and turn.gold >= RICH_GOLD and all_weapons_l3 and all_walls_l3
        ):
            v, c = voucher_for("station", station.level)
            add(v, c, station.pos, "station", 45)
        # 8. WallFixer 备货（D8 + 用户 2026-09-23）：金币紧缺时上限 4；**D4+ 常备 ≥5**；
        #    武器+墙全 L3 后不设上限。只数**工人**背包（修理工单独用，炮手持有不算数）。
        if turn.day_index >= 3:
            l3_walls = [w for w in all_walls if w.level >= 3]
            if all_weapons_l3 and all_walls_l3:
                desired = FIXER_STOCK_MAXED
            elif turn.day_index >= 4:
                desired = min(FIXER_STOCK_MAXED, max(FRONT_STOCK_TARGET, len(l3_walls)))
            else:
                desired = 2
                if l3_walls:
                    desired = min(FIXER_STOCK_MAX, max(2, len(l3_walls)))
            held = sum(
                u.backpack.count("WallFixer")
                for u in turn.ours
                if u.kind == "worker"
            )
            if held < desired and turn.gold >= RESERVE_GOLD + 10:
                missions.append(
                    UpgradeMission("WallFixer", 10, None, "stock", 50, qty=desired)
                )
        # 9. 正面墙对应升级券备货（用户 2026-09-23）：正面墙 L1→Voucher1、L2→Voucher2，
        #    D3+ 按正面墙等级各备 ≥5（金币不够则不要求）；武器+墙全满后不限制。
        #    门控：武器未到 L2 时不为墙券花钱（"金币充足时武器优先"）。
        if turn.day_index >= 3 and not weapons_need_l2:
            for lvl, voucher, cost in ((1, "WallUpgradeVoucher1", 20),
                                       (2, "WallUpgradeVoucher2", 30)):
                need = sum(1 for w in front_wall_units if w.level == lvl)
                if need <= 0:
                    continue
                held = sum(u.backpack.count(voucher) for u in turn.ours if u.kind == "worker")
                target = FIXER_STOCK_MAXED if (all_weapons_l3 and all_walls_l3) else FRONT_STOCK_TARGET
                if held < target and turn.gold >= RESERVE_GOLD + cost:
                    missions.append(
                        UpgradeMission(voucher, cost, None, "stock", 47, qty=target)
                    )
        # 去重：同一建筑只保留最高优先（小=高）的一条任务
        seen: set = set()
        uniq: list[UpgradeMission] = []
        for m in sorted(missions, key=lambda m: m.priority):
            if m.target is not None:
                key = (m.kind, m.target)
                if key in seen:
                    continue
                seen.add(key)
            uniq.append(m)
        return uniq
