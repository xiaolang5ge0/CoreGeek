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
BASE_SAFE_RADIUS = 8   # 基地安全半径：此半径内无来犯机器人 → 可腾手轰对面家（用户 IKI9XF）
# U10 已由实战证据关闭：紧邻己方建筑的机器人必须能打（墙聚怪+火箭隔山打牛是核心玩法），
# 不再扣友伤惩罚（2026-09-21 TeamB 实战：惩罚导致基地被啃时全面哑火）。
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
            # 我方兵潮已清（射程内无有价值机器人）+ 基地安全 → 转而轰对面家
            # （用户 IKI9XF 2026-09-24：对手火箭清完自家兵潮后就轰我们城区，我们也要这么做）
            enemy = self._enemy_targets(turn, weapon) if self._base_safe(turn) else []
            if enemy:
                self.cursor += 1
                commands[weapon.unit_id] = attack_command(pioneer.unit_id, enemy)
                trace["fire"] = {
                    "weapon": weapon.unit_id,
                    "targets": [t.dump() for t in enemy],
                    "mode": "enemy_base",
                }
                return
            trace["fire"] = {"reason": "no_valuable_target", "weapon": weapon.unit_id}
            return
        self.cursor += 1
        commands[weapon.unit_id] = attack_command(pioneer.unit_id, targets)
        trace["fire"] = {
            "weapon": weapon.unit_id,
            "targets": [t.dump() for t in targets],
            "aoe_score": score,
        }

    @staticmethod
    def _base_safe(turn: Turn) -> bool:
        """基地是否安全：附近没有冲我方来的机器人（用户：保证安全后再轰对面家）。"""
        station = turn.station()
        if station is None:
            return False
        return not any(
            r.alive and r.target_team in ("", turn.team_type)
            and distance(r.pos, station.pos) <= BASE_SAFE_RADIUS
            for r in turn.robots
        )

    @staticmethod
    def _enemy_targets(turn: Turn, weapon: Unit) -> list[Pos]:
        """射程内的敌方建筑（优先基地/炮塔，其次围墙）——清完己方兵潮后拆对面家。"""
        reach = weapon.range_of_attack()
        rank = {"station": 0, "gatling": 1, "railgun": 1, "rocket": 1, "wall": 2}
        cands = []
        for u in turn.enemy:
            if not u.alive:
                continue
            d = distance(weapon.pos, u.pos)
            if not (1 <= d <= reach):
                continue
            cands.append((rank.get(u.kind, 3), u.health, d, u.pos.x, u.pos.y, u.pos))
        if not cands:
            return []
        cands.sort()
        n = max(1, weapon.level)
        return [c[5] for c in cands[:n]]

    def _best_targets(self, turn: Turn, weapon: Unit) -> tuple[list[Pos], int]:
        reach = weapon.range_of_attack()
        in_range = [
            r
            for r in turn.robots
            if r.alive and 1 <= distance(weapon.pos, r.pos) <= reach
        ]
        if not in_range:
            return (), 0
        # 硬优先：射程内有冲我方来的机器人时，绝不打敌方机器人（实战教训：炮台误击对方机器人）
        ours = [r for r in in_range if r.target_team in ("", turn.team_type)]
        robots = ours if ours else in_range
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
            scored.append((score, -robot.health, -distance(weapon.pos, cell), cell.x, cell.y, cell))
        scored.sort(reverse=True)
        n_targets = max(1, weapon.level)
        picked = [
            (item[0], item[5]) for item in scored[:n_targets] if item[0] >= MIN_SCORE
        ]
        if not picked:
            return (), 0
        return [cell for _, cell in picked], picked[0][0]
