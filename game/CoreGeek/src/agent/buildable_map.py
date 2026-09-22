"""L2 可建造区学习（RULE_ASSUMPTIONS 冲突1：蓝/黄区不下发，靠试探+反馈学习）。

非法建造只算"指令执行失败"不计异常，试错成本低：
- 建造反馈 True  → 该格对该建筑类型 LEGAL
- 建造反馈 False → 该格**暂时**回避（带过期重试）——失败多是"被角色/机器人占用"
  等瞬态原因；永久拉黑会导致墙环永久缺口（issue#26 的 (31,12) 缺口）。
"""
from __future__ import annotations

from .protocol import Pos, WALL

WALL_KIND = "wall"
WEAPON_KIND = "weapon"

# 建造失败回避回合数（瞬态占用/失败 → 过期后允许重试，消除永久缺口）
BUILD_FAIL_RETRY = 40


def kind_of(name: str) -> str:
    return WALL_KIND if name == WALL else WEAPON_KIND


class BuildableMap:
    def __init__(self) -> None:
        # (pos, kind) -> 失败回合（失败时记录）；成功则移除记录
        self._failed: dict[tuple[Pos, str], int] = {}

    def record(self, pos: Pos, name: str, ok: bool, round_no: int = 0) -> None:
        key = (pos, kind_of(name))
        if ok:
            self._failed.pop(key, None)
        else:
            self._failed[key] = round_no

    def is_usable(self, pos: Pos, kind: str, round_no: int | None = None) -> bool:
        """未知格可用；失败格在过期前回避，过期后允许重试（消除永久缺口）。"""
        failed_at = self._failed.get((pos, kind))
        if failed_at is None:
            return True
        if round_no is None:
            return False
        return (round_no - failed_at) >= BUILD_FAIL_RETRY

    def known_illegal(self) -> list[tuple[Pos, str]]:
        return list(self._failed)
