"""L1 协议层：报文 DTO、指令构造器、常量表。只懂报文，不懂策略。

规则真源优先级（RULE_ASSUMPTIONS.md）：
Runtime Request > 接口文档字段语义 > 任务书静态规则 > 示例 > 本地默认值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---- 时间常量 ----
DAY_ROUNDS = 70
NIGHT_ROUNDS = 60
ROUNDS_PER_DAY = DAY_ROUNDS + NIGHT_ROUNDS  # 130
MAX_ROUNDS = 1300

# ---- 经济/建造常量（静态兜底，运行时以 Request 字段为准）----
WEAPON_BUILD_COST = 25
WALL_MATERIAL = "stone"
INITIAL_GOLD = 75
WEAPON_LIMIT = 3
ROCKET_COOLDOWN = 3

# ---- 类型取值 ----
LAND = "land"
STATION = "station"
WALL = "wall"
WORKER = "worker"
PIONEER = "pioneer"
IMP = "imp"  # 32进16 新角色：捣乱鬼（破坏/被抓，无背包）
GATLING = "gatling"
RAILGUN = "railgun"
ROCKET = "rocket"
TOWER_TYPES = (GATLING, RAILGUN, ROCKET)
CONTROLLABLE_TYPES = (WORKER, PIONEER)
ROLE_KINDS = (WORKER, PIONEER, IMP)  # 全部可控角色（imp 能否控炮待实战确认，暂不进 CONTROLLABLE_TYPES）
MINE_TYPES = ("stone", "iron", "copper")
VENDOR = "vendor"
WEAPON_SHOP = "weaponShop"
TASK_POINT_PREFIXES = ("challengerTaskPoint", "defenderTaskPoint")
ROBOT_TYPES = ("smallRobot", "middleRobot", "largeRobot", "bossRobot")
# 32进16：可驾驶小车（中立元素，分己方/敌方）
CHALLENGER_VEHICLE = "challengerVehicle"
DEFENDER_VEHICLE = "defenderVehicle"
VEHICLE_TYPES = (CHALLENGER_VEHICLE, DEFENDER_VEHICLE)
# 32进16：机器人召唤令（use 必须携带召唤位置 targetPos）
SUMMON_ORDERS = (
    "SmallRobotSummonOrder",
    "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder",
    "BossRobotSummonOrder",
)


def is_summon_robot_id(unit_id: int) -> bool:
    """可控机器人 ID 段：30000/31000（接口文档 §2.1/roleCommandMap key）。"""
    return 30000 <= int(unit_id) < 32000

# 静态射程兜底表（RUNTIME_OVERRIDE：优先使用 Unit.attack_range）
TOWER_RANGE_BY_LEVEL = {
    GATLING: (3, 5, 7),
    RAILGUN: (6, 8, 10),
    ROCKET: (10, 15, 10**9),
}

# 动作码全集（32进16 新增 destroy/catch）
ACTIONS = (
    "move", "attack", "sell", "buy", "build", "remove",
    "acceptTask", "submitAnswer", "summonTreasure", "use", "drop", "collect",
    "destroy", "catch",
)

# 需要 targetPos 的消耗品/券
TARGETED_ITEMS = {
    "WallFixer", "DizzyWeapon", "Bomb",
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
}
# 其中使用距离不限的（其余需与目标相邻）
RANGED_ITEMS = {"DizzyWeapon", "Bomb"}

NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


@dataclass(frozen=True, slots=True)
class Pos:
    x: int
    y: int

    @classmethod
    def load(cls, raw: Any) -> "Pos":
        return cls(int(raw["x"]), int(raw["y"]))

    def dump(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}

    def neighbours(self) -> tuple["Pos", ...]:
        return tuple(Pos(self.x + dx, self.y + dy) for dx, dy in NEIGHBOUR_STEPS)


def distance(first: Pos, second: Pos) -> int:
    """切比雪夫距离 max(|dx|, |dy|)"""
    return max(abs(first.x - second.x), abs(first.y - second.y))


def in_bounds(pos: Pos, width: int, height: int) -> bool:
    return 0 <= pos.x < width and 0 <= pos.y < height


def station_footprint(pos: Pos) -> tuple[Pos, ...]:
    """基地 2×2，pos 为左上角，向右(+x)向下(-y)延伸。"""
    return (
        pos,
        Pos(pos.x + 1, pos.y),
        Pos(pos.x, pos.y - 1),
        Pos(pos.x + 1, pos.y - 1),
    )


def footprint_distance(pos: Pos, footprint: tuple[Pos, ...]) -> int:
    """到基地四格的最小切比雪夫距离。建造区规则（已确认）：=1 蓝区建武器，=2 黄区建墙。"""
    return min(distance(pos, cell) for cell in footprint)


# 可建造区（用户确认，任务书缺图）：武器=距基地1格（蓝区），墙=距基地2格（黄区）
WEAPON_ZONE_DIST = 1
WALL_ZONE_DIST = 2
WALL_LIMIT = 20  # 围墙数量上限（用户确认版数值表）

# 建筑各等级满血（升级=回满血；L3 为最高级，之后升级券失效）
WALL_MAX_HP = (1000, 1500, 2000)
WEAPON_MAX_HP = (1000, 1500, 2000)
STATION_MAX_HP = (1500, 3000, 4500)


@dataclass(frozen=True, slots=True)
class Unit:
    unit_id: int
    pos: Pos
    kind: str
    health: int
    level: int
    cooldown: int
    attack_range: int
    attack_power: int
    capacity: int | None
    backpack: tuple[str, ...]
    is_driving: bool = False  # 32进16：是否处于驾驶小车状态

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Unit":
        cap = raw.get("backPackCapability")
        return cls(
            unit_id=int(raw.get("id") or 0),
            pos=Pos.load(raw.get("pos") or {"x": 0, "y": 0}),
            kind=str(raw.get("roleType") or ""),
            health=int(raw.get("health") or 0),
            level=int(raw.get("level") or 0),
            cooldown=int(raw.get("cooldown") or 0),
            attack_range=int(raw.get("attackRange") or 0),
            attack_power=int(raw.get("attackPower") or 0),
            capacity=int(cap) if cap is not None else None,
            backpack=tuple(str(item) for item in (raw.get("backpack") or ())),
            is_driving=bool(raw.get("isDriving") or False),
        )

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def backpack_full(self) -> bool:
        return self.capacity is not None and len(self.backpack) >= self.capacity

    def range_of_attack(self) -> int:
        """运行时 attackRange 优先（RUNTIME_OVERRIDE），静态表兜底。"""
        if self.attack_range > 0:
            return self.attack_range
        table = TOWER_RANGE_BY_LEVEL.get(self.kind)
        if not table:
            return 0
        level = min(max(self.level, 1), len(table))
        return table[level - 1]


@dataclass(frozen=True, slots=True)
class Robot:
    robot_id: int
    pos: Pos
    kind: str
    health: int
    abnormal_state: str
    target_team: str

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "Robot":
        return cls(
            robot_id=int(raw.get("id") or 0),
            pos=Pos.load(raw.get("pos") or {"x": 0, "y": 0}),
            kind=str(raw.get("roleType") or ""),
            health=int(raw.get("health") or 0),
            abnormal_state=str(raw.get("abnormalState") or ""),
            target_team=str(raw.get("targetTeam") or ""),
        )

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def dizzy(self) -> bool:
        return self.abnormal_state == "dizzy"


@dataclass(frozen=True, slots=True)
class PlayerTask:
    task_type: str
    pos: Pos
    cooldown_rounds: int
    score_reward: int
    gold_reward: int
    is_valid: bool
    timeout_rounds: int

    @classmethod
    def load(cls, raw: dict[str, Any]) -> "PlayerTask":
        return cls(
            task_type=str(raw.get("taskType") or ""),
            pos=Pos.load(raw.get("taskPosition") or {"x": 0, "y": 0}),
            cooldown_rounds=int(raw.get("coldDownRounds") or 0),
            score_reward=int(raw.get("scoreReward") or 0),
            gold_reward=int(raw.get("goldReward") or 0),
            is_valid=bool(raw.get("isValid")),
            timeout_rounds=int(raw.get("timeoutRounds") or 0),
        )


def _price_map(raw: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in raw or ():
        try:
            out[str(item["name"])] = int(item["price"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _int_bool_map(raw: Any) -> dict[int, bool]:
    out: dict[int, bool] = {}
    for key, value in (raw or {}).items():
        try:
            out[int(key)] = bool(value)
        except (TypeError, ValueError):
            continue
    return out


def _errors(raw: Any) -> tuple[tuple[int, str], ...]:
    out = []
    for err in raw or ():
        if isinstance(err, dict):
            out.append((int(err.get("errorCode") or 0), str(err.get("description") or "")))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Turn:
    """一回合的世界快照（防御性解析，字段缺失给安全默认值）。"""

    round_no: int
    width: int
    height: int
    zones: dict[Pos, str]
    team_type: str
    team_id: str
    gold: int
    total_score: int
    tasks: tuple[PlayerTask, ...]
    ours: tuple[Unit, ...]
    enemy: tuple[Unit, ...]
    robots: tuple[Robot, ...]
    phase_task: str
    llm_resp: str
    last_cmd_result: str
    last_summon_result: int
    last_action_results: dict[int, bool]
    vendor_prices: dict[str, int]
    shop_prices: dict[str, int]
    official_news: str
    folk_legends: str
    errors: tuple[tuple[int, str], ...]
    mine_remain: dict[Pos, int] = None  # type: ignore[assignment]  # 32进16：矿剩余采集次数
    summon_robots: tuple[Robot, ...] = ()  # 32进16：本方名下可控机器人（仅己方可见）

    @classmethod
    def load(cls, payload: dict[str, Any]) -> "Turn":
        info = payload.get("mapInfo") or {}
        team = payload.get("teamOur") or {}
        enemy = payload.get("teamEnemy") or {}
        robot = payload.get("robot") or {}
        news = payload.get("worldNews") or {}
        zones: dict[Pos, str] = {}
        mine_remain: dict[Pos, int] = {}
        for zone in info.get("zones") or ():
            if isinstance(zone, dict) and isinstance(zone.get("pos"), dict):
                zpos = Pos.load(zone["pos"])
                zkind = str(zone.get("neutralType") or "")
                zones[zpos] = zkind
                # 32进16：矿区元素携带 remain（剩余采集次数 1~10）
                remain = zone.get("remain")
                if zkind in MINE_TYPES and isinstance(remain, (int, float)):
                    mine_remain[zpos] = int(remain)
        return cls(
            round_no=int(payload.get("roundNo") or 0),
            width=int(info.get("width") or 41),
            height=int(info.get("height") or 32),
            zones=zones,
            team_type=str(team.get("type") or ""),
            team_id=str(team.get("teamId") or ""),
            gold=int(team.get("goldNum") or 0),
            total_score=int(team.get("totalScore") or 0),
            tasks=tuple(PlayerTask.load(t) for t in (team.get("playerTasks") or ())),
            ours=tuple(Unit.load(r) for r in (team.get("roles") or ())),
            enemy=tuple(Unit.load(r) for r in (enemy.get("roles") or ())),
            robots=tuple(Robot.load(r) for r in (robot.get("roles") or ())),
            phase_task=str(payload.get("phaseTask") or ""),
            llm_resp=str(payload.get("llmResp") or ""),
            last_cmd_result=str(payload.get("lastCmdResult") or ""),
            last_summon_result=int(payload.get("lastSummonTreasureResult") or 0),
            last_action_results=_int_bool_map(payload.get("lastRoundRoleActionResults")),
            vendor_prices=_price_map(payload.get("vendorShopList")),
            shop_prices=_price_map(payload.get("weaponShopList")),
            official_news=str(news.get("officialNews") or ""),
            folk_legends=str(news.get("folkLegends") or ""),
            errors=_errors(payload.get("errors")),
            mine_remain=mine_remain,
            summon_robots=tuple(
                Robot.load(r) for r in (team.get("summonRobotList") or ()) if isinstance(r, dict)
            ),
        )

    # ---- 时间 ----
    @property
    def round_in_day(self) -> int:
        """当天内回合序号 0..129。"""
        return (self.round_no - 1) % ROUNDS_PER_DAY if self.round_no > 0 else 0

    @property
    def day_index(self) -> int:
        """第几天，1..10。"""
        return (self.round_no - 1) // ROUNDS_PER_DAY + 1 if self.round_no > 0 else 1

    @property
    def is_day(self) -> bool:
        return self.round_in_day < DAY_ROUNDS

    @property
    def is_night(self) -> bool:
        return not self.is_day

    @property
    def rounds_until_night(self) -> int:
        """白天时：含本回合在内距夜晚还剩多少回合。"""
        return DAY_ROUNDS - self.round_in_day if self.is_day else 0

    @property
    def rounds_until_dawn(self) -> int:
        """黑夜时：含本回合在内距天亮还剩多少回合。"""
        return ROUNDS_PER_DAY - self.round_in_day if self.is_night else 0

    # ---- 单位查询 ----
    def find(self, unit_id: int) -> Unit | None:
        for unit in self.ours:
            if unit.unit_id == unit_id:
                return unit
        return None

    def station(self) -> Unit | None:
        for unit in self.ours:
            if unit.kind == STATION:
                return unit
        return None

    def alive(self, kinds: tuple[str, ...]) -> tuple[Unit, ...]:
        return tuple(u for u in self.ours if u.kind in kinds and u.alive)

    def workers(self) -> tuple[Unit, ...]:
        return tuple(sorted(self.alive((WORKER,)), key=lambda u: u.unit_id))

    def pioneer(self) -> Unit | None:
        for unit in self.alive((PIONEER,)):
            return unit
        return None

    def weapons(self) -> tuple[Unit, ...]:
        return tuple(sorted(self.alive(TOWER_TYPES), key=lambda u: u.unit_id))

    def walls(self) -> tuple[Unit, ...]:
        return self.alive((WALL,))

    def controllable(self) -> tuple[Unit, ...]:
        return tuple(sorted(self.alive(CONTROLLABLE_TYPES), key=lambda u: u.unit_id))

    # ---- 32进16 新增查询 ----
    def imp(self) -> Unit | None:
        for unit in self.alive((IMP,)):
            return unit
        return None

    def enemy_imps(self) -> tuple[Unit, ...]:
        """可见敌方捣乱鬼（仅 destroy 站桩引导期间全图可见）。"""
        return tuple(u for u in self.enemy if u.kind == IMP and u.alive)

    def summon_robot(self, unit_id: int) -> Robot | None:
        for robot in self.summon_robots:
            if robot.robot_id == unit_id and robot.alive:
                return robot
        return None

    def vehicle_cells(self) -> dict[Pos, str]:
        return {pos: kind for pos, kind in self.zones.items() if kind in VEHICLE_TYPES}

    def my_vehicle_side(self) -> str:
        return CHALLENGER_VEHICLE if self.team_type == "challenger" else DEFENDER_VEHICLE

    def half_of(self, pos: Pos) -> str:
        """矿区半区（左下-右上对角线分界）：返回 "upper"/"lower"。

        challenger 基地在左上（线上方），defender 在右下（线下方）。
        分界线：y = (31/40)·x → y·40 > 31·x 为 upper。
        """
        return "upper" if pos.y * 40 >= 31 * pos.x else "lower"

    def own_half(self, pos: Pos) -> bool:
        """pos 是否在我方半区（challenger=upper / defender=lower）。"""
        mine_half = "upper" if self.team_type == "challenger" else "lower"
        return self.half_of(pos) == mine_half

    # ---- 地图元素 ----
    def mines(self, kind: str | None = None) -> tuple[Pos, ...]:
        kinds = (kind,) if kind else MINE_TYPES
        return tuple(pos for pos, k in self.zones.items() if k in kinds)

    def zone_positions(self, kind: str) -> tuple[Pos, ...]:
        return tuple(pos for pos, k in self.zones.items() if k == kind)

    def vendor_positions(self) -> tuple[Pos, ...]:
        return self.zone_positions(VENDOR)

    def shop_positions(self) -> tuple[Pos, ...]:
        return self.zone_positions(WEAPON_SHOP)

    # ---- 地形与阻挡 ----
    def land(self, pos: Pos) -> bool:
        return in_bounds(pos, self.width, self.height) and self.zones.get(pos, LAND) == LAND

    def footprint(self, unit: Unit) -> tuple[Pos, ...]:
        if unit.kind == STATION:
            return station_footprint(unit.pos)
        return (unit.pos,)

    def occupied_cells(self) -> frozenset[Pos]:
        cells: set[Pos] = set()
        for unit in (*self.ours, *self.enemy):
            cells.update(self.footprint(unit))
        return frozenset(cells)

    def blocked(self, moving: Unit) -> frozenset[Pos]:
        """对 moving 角色而言的阻挡格（建筑/角色/机器人/中立元素/矿区）。

        32进16 小车特例（任务书 4.6.4）：
        - 己方小车所在格**可驶入**（驶入即驾驶）→ 不算阻挡；
        - 敌方小车所在格不可驶入 → 阻挡。
        """
        cells: set[Pos] = set()
        my_vehicle = self.my_vehicle_side()
        for pos, kind in self.zones.items():
            if kind == LAND:
                continue
            if kind in VEHICLE_TYPES and kind == my_vehicle:
                continue  # 己方小车格可驶入（上车）
            cells.add(pos)
        cells.update(self.occupied_cells())
        cells.discard(moving.pos)
        for robot in self.robots:
            if robot.alive:
                cells.add(robot.pos)
        return frozenset(cells)


def parse_targets(raw: Any) -> tuple[Pos, ...]:
    """解析 targetPos 数组；任一坏点则整体视为非法，返回空。"""
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for item in raw:
        try:
            out.append(Pos.load(item))
        except Exception:
            return ()
    return tuple(out)


# ---- 指令构造器 ----
def move_command(pos: Pos) -> dict[str, Any]:
    return {"action": "move", "targetPos": [pos.dump()]}


def attack_command(controller_id: int, positions: tuple[Pos, ...] | list[Pos]) -> dict[str, Any]:
    return {
        "action": "attack",
        "controllerId": str(controller_id),
        "targetPos": [p.dump() for p in positions],
    }


def sell_command(name: str, num: int = 1) -> dict[str, Any]:
    return {"action": "sell", "name": name, "num": int(num)}


def buy_command(name: str, num: int = 1) -> dict[str, Any]:
    return {"action": "buy", "name": name, "num": int(num)}


def build_command(pos: Pos, name: str) -> dict[str, Any]:
    return {"action": "build", "name": name, "targetPos": [pos.dump()]}


def remove_command(pos: Pos) -> dict[str, Any]:
    return {"action": "remove", "targetPos": [pos.dump()]}


def accept_task_command() -> dict[str, Any]:
    return {"action": "acceptTask"}


def submit_answer_command(answer: str) -> dict[str, Any]:
    return {"action": "submitAnswer", "taskAnswer": str(answer)}


def summon_treasure_command(pos: Pos, items: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {"action": "summonTreasure", "targetPos": [pos.dump()], "item": list(items)}


def use_command(name: str, pos: Pos | None = None) -> dict[str, Any]:
    cmd: dict[str, Any] = {"action": "use", "name": name}
    if pos is not None:
        cmd["targetPos"] = [pos.dump()]
    return cmd


def drop_command(name: str) -> dict[str, Any]:
    return {"action": "drop", "name": name}


def collect_command(pos: Pos) -> dict[str, Any]:
    return {"action": "collect", "targetPos": [pos.dump()]}


# ---- 32进16 新指令构造器 ----
def destroy_command(pos: Pos) -> dict[str, Any]:
    """破坏矿物（仅捣乱鬼）：需连续 4 回合站桩执行。"""
    return {"action": "destroy", "targetPos": [pos.dump()]}


def catch_command(pos: Pos) -> dict[str, Any]:
    """抓捕 8 邻域内的敌方捣乱鬼（全部角色可用）：成功 +20 金币，目标阵亡。"""
    return {"action": "catch", "targetPos": [pos.dump()]}


# ---- lastCmdResult 解析（32进16 新格式）----
# 完整形态："[exitCode:N]\n[durationMs:N]\n<输出>"
# 超时："[TIMEOUT]\n[durationMs:N]\n<部分输出>"
# 判题器异常："[JUDGER_ERROR]\n[durationMs:N]\n<原因>"
# 超 64KB：输出末尾追加 "[TRUNCATED]"
_CMD_STATUS = ("[TIMEOUT]", "[JUDGER_ERROR]")


def parse_cmd_result(raw: str) -> dict[str, Any]:
    """解析 executeCmd 的回合结果，兼容新旧两种格式。

    返回 {status, exit_code, duration_ms, output, truncated}：
    - status: "ok" / "timeout" / "judger_error" / "legacy"（无状态头 = 旧格式或空）
    - output: 剥离状态头与 durationMs 后的正文（timeout/judger_error 保留状态标记前缀，
      使 LLM/transcript 能看到失败语义）
    """
    text = raw or ""
    if not text:
        return {"status": "legacy", "exit_code": None, "duration_ms": None,
                "output": "", "truncated": False}
    lines = text.split("\n")
    first = lines[0].strip()
    status = "legacy"
    exit_code: int | None = None
    duration_ms: int | None = None
    body_start = 0
    if first.startswith("[exitCode:"):
        status = "ok"
        try:
            exit_code = int(first[len("[exitCode:") : -1])
        except (ValueError, IndexError):
            exit_code = None
        body_start = 1
    elif first == "[TIMEOUT]":
        status = "timeout"
        body_start = 1
    elif first == "[JUDGER_ERROR]":
        status = "judger_error"
        body_start = 1
    # 第二行固定 [durationMs:N]（仅新格式有）
    if body_start == 1 and len(lines) > 1 and lines[1].strip().startswith("[durationMs:"):
        try:
            duration_ms = int(lines[1].strip()[len("[durationMs:") : -1])
        except (ValueError, IndexError):
            duration_ms = None
        body_start = 2
    output = "\n".join(lines[body_start:])
    truncated = output.rstrip().endswith("[TRUNCATED]")
    if status in ("timeout", "judger_error"):
        # 保留失败语义（供 transcript/LLM 感知），但避免与状态头解析混淆
        output = f"[{status.upper()}]\n{output}"
    return {"status": status, "exit_code": exit_code, "duration_ms": duration_ms,
            "output": output, "truncated": truncated}


# ---- 响应构造 ----
def empty_response() -> dict[str, Any]:
    return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


def build_response(
    commands: dict[int, dict[str, Any]],
    prompt: str = "",
    execute_cmd: str = "",
) -> dict[str, Any]:
    return {
        "roleCommandMap": {str(key): value for key, value in commands.items()},
        "prompt": prompt or "",
        "executeCmd": execute_cmd or "",
    }
