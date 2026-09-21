"""L4 JointFirePlanner：3 火箭轮转 + AOE 落点评分（STRATEGY_DECISIONS #3）。

- 开拓者单人操控：attack 挂在武器 ID 下，controllerId=开拓者。
- 轮转游标：每回合从冷却就绪且与开拓者相邻的火箭中选一座，
  利用 3 回合冷却实现 R1→R2→R3 持续火力。
- 落点评分：中心 20 + 溅射 10/个；回避己方单位 3×3（U10 友伤未明，安全默认）。
- 无价值目标宁可空仓，不浪费冷却。
"""
from __future__ import annotations

from typing import Any

from .protocol import Pos, Turn, Unit, attack_command, distance

CENTER_DAMAGE = 20
SPLASH_DAMAGE = 10
LETHAL_BONUS = 15      # 能杀死的优先（最快降低敌方 DPS）
APPROACH_RADIUS = 12   # 距基地锚点 12 格内开始加权
SELF_TEAM_BONUS = 5    # 以我方基地为目标的机器人优先（两批机器人分攻双方）
FRIENDLY_PENALTY = 1000
MIN_SCORE = CENTER_DAMAGE


class JointFirePlanner:
    def __init__(self) -> None:
        self.cursor = 0

    def plan(
        self,
        turn: Turn,
        pioneer: Unit,
        commands: dict[int, dict[str, Any]],
        trace: dict[str, Any],
    ) -> None:
        ready = [
            w
            for w in turn.weapons()
            if w.cooldown == 0 and distance(pioneer.pos, w.pos) <= 1
        ]
        if not ready:
            trace["fire"] = {"reason": "no_ready_weapon"}
            return
        ready.sort(key=lambda w: w.unit_id)
        weapon = ready[self.cursor % len(ready)]
        targets, score = self._best_targets(turn, weapon)
        if not targets:
            trace["fire"] = {"reason": "no_valuable_target", "weapon": weapon.unit_id}
            return
        self.cursor += 1
        commands[weapon.unit_id] = attack_command(pioneer.unit_id, targets)
        trace["fire"] = {
            "weapon": weapon.unit_id,
            "targets": [t.dump() for t in targets],
            "aoe_score": score,
        }

    def _best_targets(self, turn: Turn, weapon: Unit) -> tuple[list[Pos], int]:
        reach = weapon.range_of_attack()
        robots = [
            r
            for r in turn.robots
            if r.alive and 1 <= distance(weapon.pos, r.pos) <= reach
        ]
        if not robots:
            return (), 0
        own_cells = turn.occupied_cells()
        station = turn.station()
        anchor = station.pos if station is not None else weapon.pos
        scored: list[tuple[int, int, int, int, int, Pos]] = []
        for robot in robots:
            cell = robot.pos
            # 伤害按 HP 封顶：中心 20、溅射 10
            damage = min(CENTER_DAMAGE, robot.health)
            kills = 1 if damage >= robot.health else 0
            for other in robots:
                if other.robot_id == robot.robot_id or distance(cell, other.pos) > 1:
                    continue
                splash = min(SPLASH_DAMAGE, other.health)
                damage += splash
                if splash >= other.health:
                    kills += 1
            # 致命优先 + 逼近防御圈优先 + 优先攻击以我方为目标的机器人
            score = (
                damage
                + LETHAL_BONUS * kills
                + max(0, APPROACH_RADIUS - distance(cell, anchor))
                + (SELF_TEAM_BONUS if robot.target_team == turn.team_type else 0)
            )
            if any(distance(cell, own) <= 1 for own in own_cells):
                score -= FRIENDLY_PENALTY
            scored.append((score, -robot.health, -distance(weapon.pos, cell), cell.x, cell.y, cell))
        scored.sort(reverse=True)
        n_targets = max(1, weapon.level)
        picked = [
            (item[0], item[5]) for item in scored[:n_targets] if item[0] >= MIN_SCORE
        ]
        if not picked:
            return (), 0
        return [cell for _, cell in picked], picked[0][0]
