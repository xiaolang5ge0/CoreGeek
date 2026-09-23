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
WEAPON_L1_COST = 100   # 武器 L1→L2 券价（武器金币预留用）
WEAPON_L2_COST = 150   # 武器 L2→L3 券价（武器金币预留用）
WALL_DAMAGE_RATIO = 0.5
CRITICAL_WALL_RATIO = 0.35  # 紧急墙：血量 < 35% 满血（评估：被破前 10 回合多在 20~40%）
WALL_MIN_L2 = 6            # 武器升 L3 前，先升的最小墙量（正面+侧面转角，约 6 块）
FIXER_STOCK_MAX = 4        # WallFixer 备货上限（金币紧缺时维持 4；全升满后不设上限）
FIXER_STOCK_MAXED = 8      # 武器+墙全 L3 后：不设上限（有余钱就多备）
FRONT_L2_TARGET = 6       # 正面+转角墙 L2 死线数量（D3 入夜前，用户 2026-09-23）
FRONT_L3_TARGET = 6       # 正面+转角墙 L3 死线数量（D5 入夜前）
FRONT_STOCK_TARGET = 6    # 正面+转角墙对应券/修复包备货数量（D3+）
WALL_VOUCHER_BATCH = 4    # 墙升级券批量上限（用户 2026-09-23：上限 4，防一次买爆饿死武器）


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

def wall_target_level(pos, anchor, front: str | None) -> int:
    """围墙目标等级（用户 2026-09-23 修订）：

    - **纯正面**（迎敌侧整列，**不含拐角**）→ L3（唯一硬性规定）
    - 其余（拐角/侧面/靠开口侧）→ L2（按掉血量排序升级）

    以 FRONT=WEST 为例（prim=dx, sec=dy, pext=3）：dx=3 且 dy∈{-1,0,1,2} → L3；
    拐角 dx=3,dy∈{-2,3} 与其余 → L2。
    """
    if anchor is None:
        return 2
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
    if prim == pext and sec not in (_RING_LO, _RING_HI):
        return 3
    return 2


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
            # 武器升级未完成时，为武器券预留金币（"不能因升级围墙导致炮台不升级"）。
            # 用户：L2 炮台最优先 → 只要还有武器 <L2，墙/备货（priority≥12）不得动用预留。
            reserve = (
                weapon_reserve()
                if (kind in ("wall", "stock") and priority >= 12)
                else 0
            )
            if budget - cost < reserve:
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

        # 只考虑火箭（用户 2026-09-23：只升火箭，gatling/railgun 不升）
        weapons = sorted(
            [w for w in turn.weapons() if w.kind == "rocket"],
            key=lambda w: (w.level, w.unit_id),
        )
        all_walls = list(turn.walls())
        any_l1_wall = any(w.level == 1 for w in all_walls)
        # 武器未到 L2 → 为武器券(100金)预留金币：暂停墙升级（仅修临界受损墙），避免廉价墙券吃光金币
        weapons_need_l2 = any(w.level < 2 for w in weapons)
        weapons_need_l3 = any(w.level < 3 for w in weapons)
        all_weapons_l3 = bool(weapons) and all(w.level >= 3 for w in weapons)
        all_walls_l3 = bool(all_walls) and all(w.level >= 3 for w in all_walls)

        def weapon_reserve() -> int:
            """武器升级预留金币（用户：L2炮台优先）：还有武器 <L2 → 100；否则 0。"""
            return WEAPON_L1_COST if weapons_need_l2 else 0
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

        # ---- 围墙终极目标等级（用户 2026-09-23 目标图）----
        def target_level(w) -> int:
            return wall_target_level(w.pos, anchor, front)

        front_wall_units = [w for w in all_walls if rank(w) in (WALL_FRONT, WALL_CORNER)]

        # ---- 硬约束（用户 2026-09-23 确认）：D5 入夜前**纯正面墙 L3 < N** →
        #      纯正面墙升级优先级**最高**（priority 1，高于 L2炮台/修复包/一切）。
        #      范围 = **纯正面（迎敌侧整列，不含拐角）**；其余墙按掉血量排序。
        front_targets = [w for w in all_walls if target_level(w) == 3]
        front_l3_n = sum(1 for w in front_targets if w.level >= 3)
        if turn.day_index <= 5 and front_l3_n < FRONT_L3_TARGET:
            for wall in front_order([w for w in front_targets if w.level < 3]):
                v, c = voucher_for("wall", wall.level)
                if v is not None:
                    add(v, c, wall.pos, "wall", 1)

        # 0. 受损/紧急墙（用户 2026-09-23）：最受损优先 → 用券升级回血（比 WallFixer 划算）。
        #    - 受损：ratio < 0.5
        #    - 紧急：ratio < 0.35（评估：被破前 10 回合多在 20~40%）或 **本夜累计掉血 > 剩余血量**
        #    优先级 12（**不高于 L2 炮台 10**，与受损墙同级）。
        def _is_hurt(w) -> bool:
            if w.level >= 3:
                return False
            if ratio(w) < WALL_DAMAGE_RATIO:
                return True
            if registry is not None:
                st = registry.walls.get(w.pos)
                if st is not None and st.exists:
                    if st.health < CRITICAL_WALL_RATIO * st.max_health:
                        return True
                    if st.night_damage > st.health > 0:
                        return True
            return False

        hurt = sorted(
            [w for w in all_walls if _is_hurt(w)],
            key=lambda w: (ratio(w), rank(w), w.pos.x, w.pos.y),
        )
        for wall in hurt:
            v, c = voucher_for("wall", wall.level)
            if v is not None:
                add(v, c, wall.pos, "wall", 12)
        # 1. 围墙修复包备货（用户：D3/D4/D5 预留 ≥3 应对夜间；D4+ 加量到 ≥4；D6+ ≥5；全满后更多）
        #    **白天提前备足**（夜里买来不及）；优先级 11，保证先于其余升级。
        if turn.day_index >= 3:
            l3_walls = [w for w in all_walls if w.level >= 3]
            if all_weapons_l3 and all_walls_l3:
                desired = FIXER_STOCK_MAXED
            elif turn.day_index >= 6:
                desired = min(FIXER_STOCK_MAXED, max(5, len(l3_walls)))
            elif turn.day_index >= 4:
                desired = min(FIXER_STOCK_MAXED, max(4, len(l3_walls)))
            else:
                desired = min(FIXER_STOCK_MAX, max(3, len(l3_walls)))
            held = sum(
                u.backpack.count("WallFixer") for u in turn.ours if u.kind == "worker"
            )
            if held < desired and turn.gold >= RESERVE_GOLD + 10 + weapon_reserve():
                missions.append(
                    UpgradeMission("WallFixer", 10, None, "stock", 11, qty=desired)
                )
        # 2. L2 炮台（武器 L1→L2）—— 升级顺序最高（用户：L2炮台 > L2围墙 > L3炮台 > L3围墙）
        for w in weapons:
            if w.level == 1:
                v, c = voucher_for("weapon", 1)
                add(v, c, w.pos, "weapon", 10)
        # 3. L2 围墙（目标 ≥2 且当前 L1）—— **按掉血量**（最受损优先），不再按位置
        for wall in sorted(
            [w for w in all_walls if w.level == 1 and target_level(w) >= 2],
            key=lambda w: (ratio(w), rank(w), w.pos.x, w.pos.y),
        ):
            v, c = voucher_for("wall", 1)
            add(v, c, wall.pos, "wall", 15)
        # 4. L3 炮台（武器 L2→L3）
        for w in weapons:
            if w.level == 2:
                v, c = voucher_for("weapon", 2)
                add(v, c, w.pos, "weapon", 20)
        # 5. L3 围墙（目标 ==3 且当前 L2）—— 纯正面（硬约束未覆盖时兜底）
        for wall in sorted(
            [w for w in all_walls if w.level == 2 and target_level(w) == 3],
            key=lambda w: (ratio(w), rank(w), w.pos.x, w.pos.y),
        ):
            v, c = voucher_for("wall", 2)
            add(v, c, wall.pos, "wall", 25)
        # 7. 基地：**仅当武器+墙全 L3**（D4 前不升基地）
        station = turn.station()
        if (
            station is not None and 1 <= station.level <= 2
            and turn.gold >= RICH_GOLD and all_weapons_l3 and all_walls_l3
        ):
            v, c = voucher_for("station", station.level)
            add(v, c, station.pos, "station", 45)
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
                if held < target and turn.gold >= RESERVE_GOLD + cost + weapon_reserve():
                    missions.append(
                        UpgradeMission(voucher, cost, None, "stock", 47, qty=target)
                    )
        # 10. 应急道具（Day6+，用户 2026-09-23）：关键围墙/炮塔升级后、有余钱时备炸弹/眩晕
        #     （危险夜用炸弹清群/眩晕拖时间，占用开拓者动作）
        if turn.day_index >= 6 and not weapons_need_l2:
            front_ok = all(w.level >= 2 for w in front_wall_units) or (
                sum(1 for w in front_wall_units if w.level >= 2) >= FRONT_L2_TARGET
            )
            if front_ok and turn.gold >= RICH_GOLD + weapon_reserve():
                held = sum(
                    u.backpack.count("Bomb") + u.backpack.count("DizzyWeapon")
                    for u in turn.ours
                )
                if held < 2:
                    add("Bomb", 100, None, "stock", 52)
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
