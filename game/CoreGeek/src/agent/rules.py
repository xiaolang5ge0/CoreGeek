"""L2 运行时规则与合法性守卫。

职责：逐条校验指令（动作-角色-昼夜-距离-预算-字段），非法指令退回并记录原因。
边界：只校验"Turn 可查证"的规则；蓝/黄可建造区合法性由 BuildableMap(P1) 学习后补充。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import (
    ACTIONS,
    CONTROLLABLE_TYPES,
    GATLING,
    MINE_TYPES,
    PIONEER,
    Pos,
    RAILGUN,
    RANGED_ITEMS,
    TARGETED_ITEMS,
    TOWER_TYPES,
    Turn,
    Unit,
    VENDOR,
    WALL,
    WALL_ZONE_DIST,
    WEAPON_BUILD_COST,
    WEAPON_LIMIT,
    WEAPON_SHOP,
    WEAPON_ZONE_DIST,
    WORKER,
    distance,
    footprint_distance,
    in_bounds,
    parse_targets,
    station_footprint,
)

WORKER_ONLY = {"build", "remove", "collect"}
PIONEER_ONLY = {"acceptTask", "submitAnswer", "summonTreasure"}
NIGHT_ONLY = {"attack"}
# remove 夜间合法性未明（事实表 U-拆除），保守按白天处理
DAY_ONLY = {"build", "remove"}


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reason: str = ""


def cone_ok(origin: Pos, targets: tuple[Pos, ...]) -> bool:
    """加特林 90° 锥形校验：任意两目标方向夹角 ≤ 90°（点积 ≥ 0）。"""
    vecs = [(p.x - origin.x, p.y - origin.y) for p in targets]
    if any(dx == 0 and dy == 0 for dx, dy in vecs):
        return False
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if vecs[i][0] * vecs[j][0] + vecs[i][1] * vecs[j][1] < 0:
                return False
    return True


class LegalityGuard:
    """对单个 Turn 做指令合法性校验。"""

    def __init__(self, turn: Turn):
        self.turn = turn

    # ---- 批量过滤 ----
    def filter_commands(
        self,
        commands: dict[int, dict[str, Any]],
        trace: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """校验全部指令，丢弃非法项并记录原因；保证一名操控者每回合至多操控一座武器。"""
        accepted: dict[int, dict[str, Any]] = {}
        dropped: dict[str, str] = {}
        controllers: set[int] = set()
        for unit_id, cmd in commands.items():
            verdict = self.check(unit_id, cmd)
            if verdict.ok and cmd.get("action") == "attack":
                try:
                    controller_id = int(cmd.get("controllerId"))
                except (TypeError, ValueError):
                    controller_id = -1
                if controller_id in controllers:
                    verdict = Verdict(False, "controller_busy")
                else:
                    controllers.add(controller_id)
            if verdict.ok:
                accepted[unit_id] = cmd
            else:
                dropped[str(unit_id)] = verdict.reason
        if dropped and trace is not None:
            trace.setdefault("guard_dropped", {}).update(dropped)
        return accepted

    # ---- 单条校验 ----
    def check(self, unit_id: int, cmd: dict[str, Any]) -> Verdict:
        turn = self.turn
        unit = turn.find(unit_id)
        if unit is None:
            return Verdict(False, "unit_not_found")
        if not unit.alive:
            return Verdict(False, "unit_dead")
        if not isinstance(cmd, dict):
            return Verdict(False, "cmd_not_dict")
        action = cmd.get("action")
        if action not in ACTIONS:
            return Verdict(False, f"unknown_action:{action}")
        if action in WORKER_ONLY and unit.kind != WORKER:
            return Verdict(False, "worker_only")
        if action in PIONEER_ONLY and unit.kind != PIONEER:
            return Verdict(False, "pioneer_only")
        if action == "attack" and unit.kind not in TOWER_TYPES:
            return Verdict(False, "attack_requires_weapon")
        if action in NIGHT_ONLY and turn.is_day:
            return Verdict(False, "night_only")
        if action in DAY_ONLY and turn.is_night:
            return Verdict(False, "day_only")
        handler = getattr(self, f"_check_{str(action).lower()}", None)
        if handler is None:
            return Verdict(True)
        return handler(unit, cmd)

    # ---- 公共小工具 ----
    def _targets(self, cmd: dict[str, Any], expect: int | None = None) -> tuple[Pos, ...] | None:
        targets = parse_targets(cmd.get("targetPos"))
        if not targets:
            return None
        if expect is not None and len(targets) != expect:
            return None
        for pos in targets:
            if not in_bounds(pos, self.turn.width, self.turn.height):
                return None
        return targets

    def _num(self, cmd: dict[str, Any]) -> int | None:
        try:
            num = int(cmd.get("num") or 1)
        except (TypeError, ValueError):
            return None
        return num if num >= 1 else None

    def _adjacent_zone(self, pos: Pos, kind: str) -> bool:
        return any(
            pos != zpos and distance(pos, zpos) <= 1
            for zpos in self.turn.zone_positions(kind)
        )

    # ---- 各动作 ----
    def _check_move(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        if distance(unit.pos, targets[0]) != 1:
            return Verdict(False, "move_not_adjacent")
        return Verdict(True)

    def _check_attack(self, weapon: Unit, cmd: dict[str, Any]) -> Verdict:
        if weapon.cooldown > 0:
            return Verdict(False, "weapon_cooling")
        try:
            controller_id = int(cmd.get("controllerId"))
        except (TypeError, ValueError):
            return Verdict(False, "bad_controllerId")
        controller = self.turn.find(controller_id)
        if controller is None or not controller.alive:
            return Verdict(False, "controller_dead")
        if controller.kind not in CONTROLLABLE_TYPES:
            return Verdict(False, "controller_not_role")
        if distance(controller.pos, weapon.pos) > 1:
            return Verdict(False, "controller_not_adjacent")
        expect = 1 if weapon.kind == RAILGUN else max(1, weapon.level)
        targets = self._targets(cmd, expect=expect)
        if targets is None:
            return Verdict(False, f"bad_targetPos_expect_{expect}")
        reach = weapon.range_of_attack()
        for pos in targets:
            dist = distance(weapon.pos, pos)
            if dist < 1:
                return Verdict(False, "target_is_self")
            if dist > reach:
                return Verdict(False, "target_out_of_range")
        if weapon.kind == GATLING and not cone_ok(weapon.pos, targets):
            return Verdict(False, "gatling_cone_over_90")
        return Verdict(True)

    def _check_sell(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if name not in MINE_TYPES:
            return Verdict(False, "sell_not_ore")
        num = self._num(cmd)
        if num is None:
            return Verdict(False, "bad_num")
        if unit.backpack.count(name) < num:
            return Verdict(False, "ore_not_enough")
        if not self._adjacent_zone(unit.pos, VENDOR):
            return Verdict(False, "vendor_not_adjacent")
        return Verdict(True)

    def _check_buy(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if not name or name not in self.turn.shop_prices:
            return Verdict(False, "goods_unknown")
        num = self._num(cmd)
        if num is None:
            return Verdict(False, "bad_num")
        if self.turn.gold < self.turn.shop_prices[name] * num:
            return Verdict(False, "gold_not_enough")
        if unit.capacity is not None and len(unit.backpack) + num > unit.capacity:
            return Verdict(False, "backpack_full")
        if not self._adjacent_zone(unit.pos, WEAPON_SHOP):
            return Verdict(False, "shop_not_adjacent")
        return Verdict(True)

    def _check_build(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if name != WALL and name not in TOWER_TYPES:
            return Verdict(False, "build_name_unknown")
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        target = targets[0]
        if distance(unit.pos, target) != 1:
            return Verdict(False, "build_not_adjacent")
        # 建造区规则（CONFIRMED）：武器=距基地1格（蓝区），墙=距基地2格（黄区）
        station = self.turn.station()
        if station is not None:
            fdist = footprint_distance(target, station_footprint(station.pos))
            if name == WALL and fdist != WALL_ZONE_DIST:
                return Verdict(False, "wall_zone_not_dist2")
            if name in TOWER_TYPES and fdist != WEAPON_ZONE_DIST:
                return Verdict(False, "weapon_zone_not_dist1")
        if name == WALL:
            if "stone" not in unit.backpack:
                return Verdict(False, "stone_missing")
        else:
            if self.turn.gold < WEAPON_BUILD_COST:
                return Verdict(False, "gold_not_enough")
            weapons = self.turn.weapons()
            overwrite = any(w.pos == target for w in weapons)
            if not overwrite and len(weapons) >= WEAPON_LIMIT:
                return Verdict(False, "weapon_limit")
        blocked = self.turn.blocked(unit)
        overwrite_own_weapon = name in TOWER_TYPES and any(
            w.pos == target for w in self.turn.weapons()
        )
        if target in blocked and not overwrite_own_weapon:
            return Verdict(False, "target_occupied")
        return Verdict(True)

    def _check_remove(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        if distance(unit.pos, targets[0]) != 1:
            return Verdict(False, "remove_not_adjacent")
        if not any(w.pos == targets[0] for w in self.turn.walls()):
            return Verdict(False, "no_wall_there")
        return Verdict(True)

    def _check_accepttask(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        for task in self.turn.tasks:
            if task.is_valid and task.cooldown_rounds == 0 and distance(unit.pos, task.pos) <= 1:
                return Verdict(True)
        return Verdict(False, "no_valid_task_adjacent")

    def _check_submitanswer(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        if not self.turn.phase_task:
            return Verdict(False, "no_task_in_progress")
        if not cmd.get("taskAnswer"):
            return Verdict(False, "empty_answer")
        return Verdict(True)

    def _check_summontreasure(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        if distance(unit.pos, targets[0]) != 1:
            return Verdict(False, "treasure_not_adjacent")
        items = cmd.get("item")
        if not isinstance(items, (list, tuple)) or not items:
            return Verdict(False, "no_sacrifice_items")
        for item in items:
            if item not in unit.backpack:
                return Verdict(False, f"item_missing:{item}")
        return Verdict(True)

    def _check_use(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if not name or name not in unit.backpack:
            return Verdict(False, "item_not_in_backpack")
        if name in TARGETED_ITEMS:
            targets = self._targets(cmd, expect=1)
            if targets is None:
                return Verdict(False, "bad_targetPos")
            if name not in RANGED_ITEMS and distance(unit.pos, targets[0]) != 1:
                return Verdict(False, "use_not_adjacent")
        return Verdict(True)

    def _check_drop(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if not name or name not in unit.backpack:
            return Verdict(False, "item_not_in_backpack")
        return Verdict(True)

    def _check_collect(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        target = targets[0]
        if distance(unit.pos, target) != 1:
            return Verdict(False, "mine_not_adjacent")
        if self.turn.zones.get(target) not in MINE_TYPES:
            return Verdict(False, "no_mine_there")
        if unit.backpack_full:
            return Verdict(False, "backpack_full")
        return Verdict(True)
