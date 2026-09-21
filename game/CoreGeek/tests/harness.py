"""构造地图测试台：最小裁判模拟器（仅测试用，不打包）。

支持：move / build(墙、武器) / collect / attack(记录冷却) / 非法建造区反馈。
机器人由测试脚本注入，不模拟其移动与伤害。
"""
from __future__ import annotations

import copy

VENDOR_PRICES = [
    {"name": "stone", "price": 1},
    {"name": "iron", "price": 3},
    {"name": "copper", "price": 5},
]

WEAPON_RANGE = {"gatling": 3, "railgun": 6, "rocket": 10}

ROBOT_STATS = {"smallRobot": (5, 40), "middleRobot": (10, 60), "largeRobot": (20, 500), "bossRobot": (40, 800)}
ROBOT_RANGE = 3
BUILDING_TYPES = ("station", "wall", "gatling", "railgun", "rocket")
ROCKET_CENTER = 20
ROCKET_SPLASH = 10
DAY_ROUNDS = 70
ROUNDS_PER_DAY = 130

SHOP_LIST = [
    {"name": "WeaponUpgradeVoucher1", "price": 100},
    {"name": "WeaponUpgradeVoucher2", "price": 150},
    {"name": "WallUpgradeVoucher1", "price": 20},
    {"name": "WallUpgradeVoucher2", "price": 30},
    {"name": "StationUpgradeVoucher1", "price": 100},
    {"name": "StationUpgradeVoucher2", "price": 150},
    {"name": "WallFixer", "price": 10},
    {"name": "Medicine", "price": 10},
]
SHOP_PRICES = {item["name"]: item["price"] for item in SHOP_LIST}
BUILDING_MAX_HP = {
    "wall": (1000, 1500, 2000),
    "gatling": (1000, 1500, 2000),
    "railgun": (1000, 1500, 2000),
    "rocket": (1000, 1500, 2000),
    "station": (1500, 3000, 4500),
}
VOUCHER_KIND = {
    "WeaponUpgradeVoucher1": ("weapon", 1), "WeaponUpgradeVoucher2": ("weapon", 2),
    "WallUpgradeVoucher1": ("wall", 1), "WallUpgradeVoucher2": ("wall", 2),
    "StationUpgradeVoucher1": ("station", 1), "StationUpgradeVoucher2": ("station", 2),
}
WEAPON_KINDS = ("gatling", "railgun", "rocket")
ROCKET_RANGE_BY_LEVEL = (10, 15, 2147483647)


def _cheb(a, b):
    return max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"]))


def _sign(v):
    return (v > 0) - (v < 0)


class SimWorld:
    def __init__(
        self,
        station_pos=(10, 24),
        mines=None,
        gold=75,
        vendor=(20, 16),
        shop=(25, 20),
        illegal_builds=(),
        tasks=None,
        llm_script=None,
        cmd_handler=None,
        expected_answer=None,
        accept_fails=False,
    ):
        """mines: {(x,y): kind}，每矿 10 次；illegal_builds: {(x,y)} 建造必失败。
        tasks: [{"pos": (x,y), "text": str, "scoreReward": 50, "goldReward": 30, "timeoutRounds": 60}]
        llm_script: [str,...] 每次 prompt 弹出一条作为下回合 llmResp；
        cmd_handler: fn(cmd)->str 沙盒结果；expected_answer: 提交答案包含该子串即判完成。"""
        self.round_no = 1
        self.gold = gold
        self.score = 0
        self.illegal = set(illegal_builds)
        sx, sy = station_pos
        self.roles = [
            self._role(10013, sx, sy, "station", 1500, level=1),
            self._role(10010, sx - 1, sy - 1, "worker", 220, cap=100),
            self._role(10012, sx - 1, sy - 2, "worker", 220, cap=100),
            self._role(10011, sx - 1, sy, "pioneer", 200, cap=40),
        ]
        self.mines = {tuple(pos): [kind, 10] for pos, kind in (mines or {}).items()}
        self.vendor = tuple(vendor)
        self.shop = tuple(shop)
        self.robots = []
        self.cooldowns: dict[int, int] = {}
        self.last_results: dict[int, bool] = {}
        self.wall_seq = 40000
        self.weapon_seq = 10040
        self.closed_mines = set()  # 新闻封矿：矿在但 collect 失败
        # 任务模拟
        self.task_points = [
            {
                "pos": tuple(t["pos"]),
                "text": t.get("text", "任务原文"),
                "scoreReward": t.get("scoreReward", 50),
                "goldReward": t.get("goldReward", 30),
                "timeoutRounds": t.get("timeoutRounds", 60),
                "isValid": True,
                "coldDownRounds": 0,
                "seq": index + 1,
            }
            for index, t in enumerate(tasks or [])
        ]
        self.llm_script = list(llm_script or [])
        self.cmd_handler = cmd_handler or (lambda cmd: "[exitCode:0]\n")
        self.expected_answer = expected_answer
        self.phase_task = ""
        self.task_active = None  # {"point": dict, "accept_round": int}
        self.accept_fails = accept_fails
        self.accept_count = 0
        self.pending_llm_resp = ""
        self.pending_cmd_result = ""
        self.next_errors = []
        self.submissions = []
        self.prompts_seen = []
        self.cmds_seen = []

    @staticmethod
    def _role(rid, x, y, kind, hp, level=0, cap=0):
        return {
            "id": rid,
            "pos": {"x": x, "y": y},
            "roleType": kind,
            "health": hp,
            "attackPower": 0,
            "attackRange": 0,
            "level": level,
            "backPackCapability": cap,
            "backpack": [],
        }

    # ---- 状态查询 ----
    def role(self, rid):
        return next(r for r in self.roles if r["id"] == rid)

    def weapons(self):
        return [r for r in self.roles if r["roleType"] in ("gatling", "railgun", "rocket")]

    def walls(self):
        return [r for r in self.roles if r["roleType"] == "wall"]

    def spawn_robot(self, x, y, kind="smallRobot", hp=40, rid=None):
        rid = rid if rid is not None else 30000 + len(self.robots)
        self.robots.append(
            {
                "id": rid,
                "pos": {"x": x, "y": y},
                "roleType": kind,
                "health": hp,
                "abnormalState": "",
                "targetTeam": "challenger",
            }
        )

    # ---- 回合推进 ----
    def _zones(self):
        zones = [
            {"neutralType": kind, "pos": {"x": x, "y": y}}
            for (x, y), (kind, _) in self.mines.items()
        ]
        zones.append({"neutralType": "vendor", "pos": {"x": self.vendor[0], "y": self.vendor[1]}})
        zones.append(
            {"neutralType": "weaponShop", "pos": {"x": self.shop[0], "y": self.shop[1]}}
        )
        for point in self.task_points:
            zones.append({
                "neutralType": f"challengerTaskPoint{point['seq']}",
                "pos": {"x": point["pos"][0], "y": point["pos"][1]},
            })
        return zones

    def _player_tasks(self):
        return [
            {
                "taskType": f"自进化类{point['seq']}",
                "taskPosition": {"x": point["pos"][0], "y": point["pos"][1]},
                "coldDownRounds": point["coldDownRounds"],
                "scoreReward": point["scoreReward"],
                "goldReward": point["goldReward"],
                "isValid": point["isValid"],
                "timeoutRounds": point["timeoutRounds"],
            }
            for point in self.task_points
        ]

    def payload(self):
        roles = copy.deepcopy(self.roles)
        for role in roles:
            role["cooldown"] = self.cooldowns.get(role["id"], 0)
            if role["roleType"] in WEAPON_RANGE and not role.get("attackRange"):
                role["attackRange"] = WEAPON_RANGE[role["roleType"]]
        return {
            "roundNo": self.round_no,
            "mapInfo": {"width": 41, "height": 32, "zones": self._zones()},
            "teamOur": {
                "type": "challenger",
                "teamId": "sim",
                "teamName": "Sim",
                "goldNum": self.gold,
                "totalScore": self.score,
                "playerTasks": self._player_tasks(),
                "roles": roles,
            },
            "teamEnemy": {"roles": []},
            "robot": {"roles": copy.deepcopy(self.robots)},
            "phaseTask": self.phase_task,
            "lastRoundRoleActionResults": {str(k): v for k, v in self.last_results.items()},
            "lastSummonTreasureResult": 0,
            "llmResp": self.pending_llm_resp,
            "worldNews": {"officialNews": "", "folkLegends": ""},
            "lastCmdResult": self.pending_cmd_result,
            "vendorShopList": copy.deepcopy(VENDOR_PRICES),
            "weaponShopList": copy.deepcopy(SHOP_LIST),
            "errors": copy.deepcopy(self.next_errors),
        }

    def _is_day(self):
        return (self.round_no - 1) % ROUNDS_PER_DAY < DAY_ROUNDS

    @property
    def base_alive(self):
        return any(r["roleType"] == "station" and r["health"] > 0 for r in self.roles)

    def advance(self):
        was_day = self._is_day()
        self.round_no += 1
        self.cooldowns = {
            rid: cd - 1 for rid, cd in self.cooldowns.items() if cd > 1
        }
        if self._is_day() and not was_day:
            self.robots.clear()  # 天亮残余机器人自动清除
        # 任务点冷却递减 / 任务超时 / 开拓者离开任务点 → 任务结束
        for point in self.task_points:
            if point["coldDownRounds"] > 0:
                point["coldDownRounds"] -= 1
                if point["coldDownRounds"] == 0:
                    point["isValid"] = True
        if self.task_active is not None:
            pioneer = self.role(10011)
            px, py = self.task_active["point"]["pos"]
            adjacent = max(
                abs(pioneer["pos"]["x"] - px), abs(pioneer["pos"]["y"] - py)
            ) <= 1
            timeout = (
                self.round_no - self.task_active["accept_round"]
                >= self.task_active["point"]["timeoutRounds"]
            )
            if not adjacent or timeout:
                self._end_task(cooldown=30)
        if not self._is_day():
            self._robots_act()
        # 血量归零的建筑拆除（角色简化不死）
        self.roles = [
            r for r in self.roles
            if r["health"] > 0 or r["roleType"] in ("worker", "pioneer")
        ]
        self.robots = [r for r in self.robots if r["health"] > 0]

    def _robots_act(self):
        buildings = [
            r for r in self.roles
            if r["roleType"] in BUILDING_TYPES and r["health"] > 0
        ]
        occupied = {(r["pos"]["x"], r["pos"]["y"]) for r in self.robots}
        for robot in self.robots:
            if robot["health"] <= 0 or not buildings:
                continue
            atk, _ = ROBOT_STATS[robot["roleType"]]
            target = min(buildings, key=lambda b: _cheb(robot["pos"], b["pos"]))
            dist = _cheb(robot["pos"], target["pos"])
            if dist <= ROBOT_RANGE:
                target["health"] -= atk
                continue
            nx = robot["pos"]["x"] + _sign(target["pos"]["x"] - robot["pos"]["x"])
            ny = robot["pos"]["y"] + _sign(target["pos"]["y"] - robot["pos"]["y"])
            occupied.discard((robot["pos"]["x"], robot["pos"]["y"]))
            if (nx, ny) not in occupied:
                robot["pos"] = {"x": nx, "y": ny}
            occupied.add((robot["pos"]["x"], robot["pos"]["y"]))

    # ---- 指令应用（简化裁判语义）----
    def apply(self, response):
        self.next_errors = []
        # LLM / 沙盒异步：本回合 prompt/executeCmd → 下回合 llmResp / lastCmdResult
        prompt = response.get("prompt") or ""
        if prompt:
            self.prompts_seen.append(prompt)
            self.pending_llm_resp = self.llm_script.pop(0) if self.llm_script else ""
        else:
            self.pending_llm_resp = ""
        execute_cmd = response.get("executeCmd") or ""
        if execute_cmd:
            self.cmds_seen.append(execute_cmd)
            self.pending_cmd_result = self.cmd_handler(execute_cmd)
        else:
            self.pending_cmd_result = ""
        results = {}
        for rid, cmd in (response.get("roleCommandMap") or {}).items():
            results[int(rid)] = self._apply(int(rid), cmd)
        self.last_results = results

    def _apply_accept_task(self, rid):
        self.accept_count += 1
        if self.accept_fails:
            return False
        pioneer = self.role(rid)
        for point in self.task_points:
            px, py = point["pos"]
            adjacent = max(
                abs(pioneer["pos"]["x"] - px), abs(pioneer["pos"]["y"] - py)
            ) <= 1
            if adjacent and point["isValid"] and point["coldDownRounds"] == 0:
                self.phase_task = point["text"]
                self.task_active = {"point": point, "accept_round": self.round_no}
                point["isValid"] = False
                return True
        return False

    def _apply_submit(self, rid, cmd):
        if not self.phase_task:
            return False
        answer = str(cmd.get("taskAnswer") or "")
        self.submissions.append(answer)
        point = self.task_active["point"]
        if self.expected_answer and self.expected_answer in answer:
            self.gold += point["goldReward"]
            self.score += point["scoreReward"]
            self._end_task(cooldown=30)
        else:
            self.next_errors = [{"errorCode": 2, "description": "答案错误或不完全正确"}]
        return True

    def _end_task(self, cooldown=30):
        if self.task_active is not None:
            self.task_active["point"]["coldDownRounds"] = cooldown
        self.phase_task = ""
        self.task_active = None

    def _apply(self, rid, cmd):
        action = cmd.get("action")
        try:
            if action == "move":
                self.role(rid)["pos"] = dict(cmd["targetPos"][0])
                return True
            if action == "build":
                return self._apply_build(rid, cmd)
            if action == "collect":
                return self._apply_collect(rid, cmd)
            if action == "sell":
                return self._apply_sell(rid, cmd)
            if action == "buy":
                return self._apply_buy(rid, cmd)
            if action == "use":
                return self._apply_use(rid, cmd)
            if action == "acceptTask":
                return self._apply_accept_task(rid)
            if action == "submitAnswer":
                return self._apply_submit(rid, cmd)
            if action == "attack":
                self.cooldowns[rid] = 3
                for tp in cmd.get("targetPos") or []:
                    for robot in self.robots:
                        dist = _cheb(tp, robot["pos"])
                        if dist == 0:
                            robot["health"] -= ROCKET_CENTER
                        elif dist == 1:
                            robot["health"] -= ROCKET_SPLASH
                self.robots = [r for r in self.robots if r["health"] > 0]
                return True
            return True
        except (KeyError, IndexError, StopIteration):
            return False

    PRICE = {"stone": 1, "iron": 3, "copper": 5}

    def add_mine(self, pos, kind, remaining=10):
        self.mines[tuple(pos)] = [kind, remaining]

    def _apply_sell(self, rid, cmd):
        role = self.role(rid)
        name = cmd.get("name")
        num = int(cmd.get("num") or 1)
        if role["backpack"].count(name) < num:
            return False
        rx, ry = role["pos"]["x"], role["pos"]["y"]
        if max(abs(rx - self.vendor[0]), abs(ry - self.vendor[1])) > 1:
            return False
        for _ in range(num):
            role["backpack"].remove(name)
        self.gold += self.PRICE.get(name, 0) * num
        return True

    def _apply_build(self, rid, cmd):
        pos = cmd["targetPos"][0]
        if (pos["x"], pos["y"]) in self.illegal:
            return False
        name = cmd.get("name")
        # 建造区规则：武器=距基地1格，墙=距基地2格
        station = next((r for r in self.roles if r["roleType"] == "station"), None)
        if station is not None:
            sx, sy = station["pos"]["x"], station["pos"]["y"]
            fp = [(sx, sy), (sx + 1, sy), (sx, sy - 1), (sx + 1, sy - 1)]
            fdist = min(max(abs(pos["x"] - cx), abs(pos["y"] - cy)) for cx, cy in fp)
            if name == "wall" and fdist != 2:
                return False
            if name in WEAPON_RANGE and fdist != 1:
                return False
        worker = self.role(rid)
        if name == "wall":
            if "stone" not in worker["backpack"]:
                return False
            worker["backpack"].remove("stone")
            self.roles.append(
                self._role(self.wall_seq, pos["x"], pos["y"], "wall", 1000, level=1)
            )
            self.wall_seq += 1
            return True
        if name in WEAPON_RANGE:
            if self.gold < 25:
                return False
            self.gold -= 25
            self.roles.append(
                self._role(self.weapon_seq, pos["x"], pos["y"], name, 1000, level=1)
            )
            self.weapon_seq += 1
            return True
        return False

    def _apply_buy(self, rid, cmd):
        role = self.role(rid)
        name = cmd.get("name")
        num = int(cmd.get("num") or 1)
        if name not in SHOP_PRICES or self.gold < SHOP_PRICES[name] * num:
            return False
        if len(role["backpack"]) + num > role["backPackCapability"]:
            return False
        rx, ry = role["pos"]["x"], role["pos"]["y"]
        if max(abs(rx - self.shop[0]), abs(ry - self.shop[1])) > 1:
            return False
        self.gold -= SHOP_PRICES[name] * num
        role["backpack"].extend([name] * num)
        return True

    def _apply_use(self, rid, cmd):
        role = self.role(rid)
        name = cmd.get("name")
        if name not in role["backpack"]:
            return False
        if name == "Medicine":
            role["health"] = 220 if role["roleType"] == "worker" else 200
            role["backpack"].remove(name)
            return True
        target = (cmd.get("targetPos") or [{}])[0]
        tx, ty = target.get("x"), target.get("y")
        if name == "WallFixer":
            wall = self._building_at(tx, ty, ("wall",))
            if wall is None or not self._adjacent(role, wall):
                return False
            wall["health"] = BUILDING_MAX_HP["wall"][wall["level"] - 1]
            role["backpack"].remove(name)
            return True
        if name in VOUCHER_KIND:
            kind, from_level = VOUCHER_KIND[name]
            kinds = WEAPON_KINDS if kind == "weapon" else (kind,)
            building = self._building_at(tx, ty, kinds)
            if building is None or building["level"] != from_level:
                return False
            if not self._adjacent(role, building):
                return False
            building["level"] += 1
            building["health"] = BUILDING_MAX_HP[building["roleType"]][building["level"] - 1]
            if building["roleType"] == "rocket":
                building["attackRange"] = ROCKET_RANGE_BY_LEVEL[building["level"] - 1]
            role["backpack"].remove(name)
            return True
        return False

    def _building_at(self, x, y, kinds):
        for role in self.roles:
            if role["roleType"] in kinds and role["pos"]["x"] == x and role["pos"]["y"] == y:
                return role
        return None

    @staticmethod
    def _adjacent(role, building):
        return max(
            abs(role["pos"]["x"] - building["pos"]["x"]),
            abs(role["pos"]["y"] - building["pos"]["y"]),
        ) <= 1 and (role["pos"]["x"], role["pos"]["y"]) != (building["pos"]["x"], building["pos"]["y"])

    def _apply_collect(self, rid, cmd):
        pos = cmd["targetPos"][0]
        key = (pos["x"], pos["y"])
        if key not in self.mines or self.mines[key][1] <= 0:
            return False
        if key in self.closed_mines:
            return False
        role = self.role(rid)
        if len(role["backpack"]) >= role["backPackCapability"]:
            return False
        kind, remaining = self.mines[key]
        role["backpack"].append(kind)
        remaining -= 1
        if remaining <= 0:
            del self.mines[key]
        else:
            self.mines[key][1] = remaining
        return True
