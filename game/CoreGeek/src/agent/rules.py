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
    IMP,
    LAND,
    MINE_TYPES,
    PIONEER,
    Pos,
    RAILGUN,
    RANGED_ITEMS,
    STATION,
    SUMMON_ORDERS,
    TARGETED_ITEMS,
    TOWER_TYPES,
    Turn,
    Unit,
    VEHICLE_TYPES,
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
    is_summon_robot_id,
    parse_targets,
    station_footprint,
)

WORKER_ONLY = {"build", "remove", "collect"}
PIONEER_ONLY = {"acceptTask", "submitAnswer", "summonTreasure"}
IMP_ONLY = {"destroy"}  # 32进16：破坏仅捣乱鬼可用
NIGHT_ONLY = {"attack"}
# remove 夜间合法性未明（事实表 U-拆除），保守按白天处理
DAY_ONLY = {"build", "remove"}
# 可控机器人可用动作（32进16：roleCommandMap key 可为 30000/31000 段）
ROBOT_ACTIONS = {"move", "attack"}
ROBOT_ATTACK_RANGE = 3  # 任务书 4.7.2：机器人攻击距离 3


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
        # 32进16：可控机器人（30000/31000 段）走独立校验通道
        if isinstance(unit_id, int) and is_summon_robot_id(unit_id):
            return self._check_robot(unit_id, cmd)
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
        if action in IMP_ONLY and unit.kind != IMP:
            return Verdict(False, "imp_only")
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

    # ---- 32进16：可控机器人指令校验 ----
    def _check_robot(self, unit_id: int, cmd: dict[str, Any]) -> Verdict:
        robot = self.turn.summon_robot(unit_id)
        if robot is None:
            return Verdict(False, "robot_not_found")
        if not isinstance(cmd, dict):
            return Verdict(False, "cmd_not_dict")
        action = cmd.get("action")
        if action not in ROBOT_ACTIONS:
            return Verdict(False, f"robot_action_unknown:{action}")
        targets = parse_targets(cmd.get("targetPos"))
        if not targets:
            return Verdict(False, "bad_targetPos")
        for pos in targets:
            if not in_bounds(pos, self.turn.width, self.turn.height):
                return Verdict(False, "target_out_of_map")
        if action == "move":
            if len(targets) != 1 or distance(robot.pos, targets[0]) != 1:
                return Verdict(False, "robot_move_not_adjacent")
            return Verdict(True)
        # attack：仅夜晚可用（同角色口径，待实战确认——RULE_ASSUMPTIONS U13）
        if self.turn.is_day:
            return Verdict(False, "night_only")
        for pos in targets:
            if distance(robot.pos, pos) > ROBOT_ATTACK_RANGE:
                return Verdict(False, "target_out_of_range")
        return Verdict(True)

    # ---- 公共小工具 ----
    def _targets(self, cmd: dict[str, Any], expect: int | None = None) -> tuple[Pos, ...] | None:
        targets = parse_targets(cmd.get("targetPos"))
        if not targets:
            return None
        if expect is not None and not (1 <= len(targets) <= expect):
            return None   # 允许 1..武器等级 个目标（只剩少量机器人时也能开火，用户 2026-09-23）
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
        dist = distance(unit.pos, targets[0])
        # 32进16：驾驶小车时每次最多移动两格（第1格碰撞停原地/第2格碰撞停第2格，结算在判题器）
        if dist == 1 or (unit.is_driving and dist == 2):
            return Verdict(True)
        return Verdict(False, "move_not_adjacent")

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
        # 32进16：小车所在格不可建造（含己方/敌方小车）——绝对否决，先于建造区校验
        if any(target == vpos for vpos in self.turn.vehicle_cells()):
            return Verdict(False, "target_on_vehicle_cell")
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

    # ---- 32进16 新动作 ----
    def _check_destroy(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        """破坏矿物（仅捣乱鬼）：目标周围一格且为矿；连续 4 回合执行（进度在 FSM 跟踪）。"""
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        target = targets[0]
        if distance(unit.pos, target) != 1:
            return Verdict(False, "mine_not_adjacent")
        if self.turn.zones.get(target) not in MINE_TYPES:
            return Verdict(False, "no_mine_there")
        return Verdict(True)

    def _check_catch(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        """抓捕（全部角色）：目标 8 邻域内、且该位置有**可见**敌方捣乱鬼。

        保守口径：敌方 imp 仅 destroy 站桩期间全图可见；看不到就不发（防"指令错误"异常）。
        """
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "bad_targetPos")
        target = targets[0]
        if distance(unit.pos, target) != 1:
            return Verdict(False, "target_not_adjacent")
        if not any(e.pos == target for e in self.turn.enemy_imps()):
            return Verdict(False, "no_enemy_imp_there")
        return Verdict(True)

    def _check_use(self, unit: Unit, cmd: dict[str, Any]) -> Verdict:
        name = cmd.get("name")
        if not name or name not in unit.backpack:
            return Verdict(False, "item_not_in_backpack")
        if name in SUMMON_ORDERS:
            return self._check_use_summon_order(cmd)
        if name in TARGETED_ITEMS:
            targets = self._targets(cmd, expect=1)
            if targets is None:
                return Verdict(False, "bad_targetPos")
            if name not in RANGED_ITEMS and distance(unit.pos, targets[0]) != 1:
                return Verdict(False, "use_not_adjacent")
        return Verdict(True)

    def _check_use_summon_order(self, cmd: dict[str, Any]) -> Verdict:
        """召唤令（32进16）：use 必须携带召唤位置 targetPos（否则非法→异常红线）。

        非法场景（任务书 4.6.3 注，非法=退还召唤令+指令错误风险）：
        位置不在地图内 / 在任一基地建造区（距基地footprint ≤2）/ 在 NPC 处 /
        与己方已使用待生成召唤位重叠（跨回合状态，由 brain 层规避）。
        """
        targets = self._targets(cmd, expect=1)
        if targets is None:
            return Verdict(False, "summon_need_targetPos")
        target = targets[0]
        stations = [u for u in self.turn.ours if u.kind == STATION]
        stations += [u for u in self.turn.enemy if u.kind == STATION]
        for station in stations:
            fdist = footprint_distance(target, station_footprint(station.pos))
            if 1 <= fdist <= 2:
                return Verdict(False, "summon_in_build_zone")
        if self.turn.zones.get(target, LAND) != LAND:
            return Verdict(False, "summon_on_npc_or_blocked")
        return Verdict(True)
