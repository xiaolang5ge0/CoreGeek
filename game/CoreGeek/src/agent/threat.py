"""L3 ThreatEstimator / TimeToFailureEstimator。

估计本夜机器人威胁：
- our_dps：3 火箭轮转持续火力 ≈ Σ(20×level)/3 每回合
- kill_rounds：清场估计 = 机器人总HP / our_dps
- breach_rounds：破防估计(TTF) = 防御圈总HP / 机器人总攻击
等级：SAFE / WARN（接敌或清不完） / CRITICAL（破防赛跑落后 → 召回工人）
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import Turn, station_footprint, distance

SAFE = "SAFE"
WARN = "WARN"
CRITICAL = "CRITICAL"

ROBOT_ATK = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}
ROCKET_CENTER = 20
ROCKET_CD = 3


@dataclass(frozen=True, slots=True)
class ThreatReport:
    robots_alive: int
    total_hp: int
    total_atk: int
    nearest_dist: int
    our_dps: float
    kill_rounds: float
    breach_rounds: float
    level: str

    def dump(self) -> dict:
        return {
            "robots": self.robots_alive,
            "total_hp": self.total_hp,
            "total_atk": self.total_atk,
            "nearest": self.nearest_dist,
            "our_dps": round(self.our_dps, 1),
            "kill_rounds": round(self.kill_rounds, 1),
            "breach_rounds": round(self.breach_rounds, 1),
            "level": self.level,
        }


class ThreatEstimator:
    def evaluate(self, turn: Turn) -> ThreatReport:
        robots = [
            r
            for r in turn.robots
            if r.alive and r.target_team in ("", turn.team_type)
        ]
        if not robots:
            return ThreatReport(0, 0, 0, 99, 0.0, 0.0, float("inf"), SAFE)

        defense_cells = set()
        station = turn.station()
        if station is not None:
            defense_cells.update(station_footprint(station.pos))
        defense_cells.update(w.pos for w in turn.walls())
        defense_cells.update(w.pos for w in turn.weapons())
        nearest = min(
            (min(distance(r.pos, cell) for cell in defense_cells) for r in robots),
            default=99,
        )

        total_hp = sum(r.health for r in robots)
        total_atk = sum(ROBOT_ATK.get(r.kind, 5) for r in robots)
        weapons = turn.weapons()
        our_dps = sum(ROCKET_CENTER * max(1, w.level) for w in weapons) / ROCKET_CD
        kill_rounds = total_hp / our_dps if our_dps > 0 else float("inf")
        defense_hp = (
            sum(w.health for w in turn.walls())
            + sum(w.health for w in weapons)
            + (station.health if station is not None else 0)
        )
        breach_rounds = defense_hp / total_atk if total_atk > 0 else float("inf")

        if our_dps <= 0:
            level = CRITICAL  # 无火力纯挨打
        elif nearest <= 4 and kill_rounds > breach_rounds:
            level = CRITICAL  # 破防赛跑落后
        elif nearest <= 3 or kill_rounds > turn.rounds_until_dawn:
            level = WARN  # 已接敌或本夜清不完
        else:
            level = SAFE
        return ThreatReport(
            len(robots), total_hp, total_atk, nearest,
            our_dps, kill_rounds, breach_rounds, level,
        )
