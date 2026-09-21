"""L2 可建造区学习（RULE_ASSUMPTIONS 冲突1：蓝/黄区不下发，靠试探+反馈学习）。

非法建造只算"指令执行失败"不计异常，试错成本低：
- 建造反馈 True  → 该格对该建筑类型 LEGAL
- 建造反馈 False → 该格对该建筑类型 ILLEGAL（加入回避）
"""
from __future__ import annotations

from .protocol import Pos, WALL

WALL_KIND = "wall"
WEAPON_KIND = "weapon"


def kind_of(name: str) -> str:
    return WALL_KIND if name == WALL else WEAPON_KIND


class BuildableMap:
    def __init__(self) -> None:
        self._states: dict[tuple[Pos, str], bool] = {}

    def record(self, pos: Pos, name: str, ok: bool) -> None:
        self._states[(pos, kind_of(name))] = bool(ok)

    def is_usable(self, pos: Pos, kind: str) -> bool:
        """未知格视为可用（允许试探）；已证实非法才回避。"""
        return self._states.get((pos, kind), True)

    def known_illegal(self) -> list[tuple[Pos, str]]:
        return [key for key, ok in self._states.items() if not ok]
