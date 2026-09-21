"""L4 路径规划：8方向 A*（切比雪夫启发）+ 邻接操作格解析 + 移动预留。

注意：矿/小贩/炮台等"操作对象"通常不是角色最终站立格，
角色目标应是其"合法邻接操作格"（ARCHITECTURE_DESIGN §4 path）。
"""
from __future__ import annotations

from heapq import heappop, heappush
from itertools import count

from .protocol import Pos, Turn, Unit, distance


def find_path(
    turn: Turn,
    unit: Unit,
    goal: Pos,
    reserved: frozenset[Pos] = frozenset(),
) -> list[Pos] | None:
    """A* 全路径（含起点终点）；不可达返回 None。"""
    if unit.pos == goal:
        return [unit.pos]
    blocked = turn.blocked(unit)
    reserved = frozenset(reserved) - {unit.pos}
    if goal in blocked or goal in reserved:
        return None
    order = count()
    frontier: list[tuple[int, int, int, Pos]] = [
        (distance(unit.pos, goal), 0, next(order), unit.pos)
    ]
    came_from: dict[Pos, Pos] = {}
    best = {unit.pos: 0}
    seen: set[Pos] = set()
    while frontier:
        _, cost, _, current = heappop(frontier)
        if current in seen:
            continue
        if current == goal:
            path = [current]
            while current != unit.pos:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path
        seen.add(current)
        for step in current.neighbours():
            if step in blocked or step in reserved or not turn.land(step):
                continue
            new_cost = cost + 1
            if new_cost >= best.get(step, new_cost + 1):
                continue
            best[step] = new_cost
            came_from[step] = current
            heappush(frontier, (new_cost + distance(step, goal), new_cost, next(order), step))
    return None


def next_step(
    turn: Turn,
    unit: Unit,
    goal: Pos,
    reserved: frozenset[Pos] = frozenset(),
) -> Pos | None:
    path = find_path(turn, unit, goal, reserved)
    if path and len(path) >= 2:
        return path[1]
    return None


def approach_cells(
    turn: Turn,
    unit: Unit,
    target: Pos,
    reserved: frozenset[Pos] = frozenset(),
) -> list[Pos]:
    """target（矿/小贩/武器等）的合法邻接操作格，按距 unit 由近到远排序。"""
    blocked = turn.blocked(unit)
    reserved = frozenset(reserved) - {unit.pos}
    cells = [
        pos
        for pos in target.neighbours()
        if turn.land(pos) and pos not in blocked and pos not in reserved
    ]
    cells.sort(key=lambda p: (distance(p, unit.pos), p.x, p.y))
    return cells


def step_toward(
    turn: Turn,
    unit: Unit,
    target: Pos,
    reserved: frozenset[Pos] = frozenset(),
) -> Pos | None:
    """target 是操作对象：走到其邻接操作格。已在邻接格返回 None。"""
    if unit.pos != target and distance(unit.pos, target) <= 1:
        return None
    for cell in approach_cells(turn, unit, target, reserved):
        step = next_step(turn, unit, cell, reserved)
        if step is not None:
            return step
    return None
