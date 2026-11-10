"""建筑布局与城镇生成。

建筑是「目的地」：居民的快脑行为最终落到某个建筑的一个格子上。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .grid import Grid, Terrain


class BuildingKind(str, Enum):
    HOME = "home"
    OFFICE = "office"       # 上班
    SCHOOL = "school"       # 上学
    RESTAURANT = "restaurant"
    SHOP = "shop"
    PARK = "park"
    TOILET = "toilet"       # 公共厕所（M3 内急链用）
    HOSPITAL = "hospital"
    CANTEEN = "canteen"     # 食堂（紧邻办公楼，B2 修复新增的午休用餐点）
    BAR = "bar"             # 酒吧（夜间娱乐/社交）
    FISHING_POND = "fishing_pond"  # 钓鱼池塘（白天娱乐/社交）


@dataclass
class Building:
    bid: int
    kind: BuildingKind
    x: int
    y: int
    w: int
    h: int
    entrance: tuple[int, int] = (0, 0)
    workers: int = 0
    capacity: int = 0
    label: str = ""
    # 编辑器点位换算出的实体格：{"toilet": [(x,y)...], "bed": [...], "table": [...]}
    # 完全由 points.json 驱动；没有点位就是空字典 —— 绝不回落到随机挑格。
    furniture: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # 与 furniture **同序**的格内小数偏移（0..1）：编辑器里点在格子的哪个位置。
    # 寻路只认整数格（furniture）；站位/渲染用「格 + 偏移」→ 设在哪就站哪。
    furniture_off: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # 马桶格（建筑内的白色点点）：居民须走到其中一格并停留 1 分钟才算如厕完成。
    # 来源 = furniture["toilet"]，即用户在编辑器里点的位置，不再按数量随机挑格。
    toilets: list[tuple[int, int]] = field(default_factory=list)
    # 营业时间（单位：仿真小时，0..24，可为小数）。支持跨夜（如 22→6）。
    # 住宅 / 公园 / 公共厕所按用户规格「不设作息」→ 0→24 视为全天开放。
    # 用户可在前端点击建筑就地修改，改完下一 tick 立即生效（热更新，无需重启）。
    opens: float = 0.0
    closes: float = 24.0
    # 本栋实际使用的贴图（相对 web/assets/buildings/ 的路径，如 "home/home_07.png"）。
    # **这是「哪栋楼用哪张图」的唯一真相**：/api/map 发给前端，前端直接按它加载，
    # 不再自己按序号轮转。否则编辑器点位、后端绑定的家具格、前端画出的家具点会各画
    # 各的 —— 实测 36 栋里 21 栋对不上（住宅 17 栋全中），人睡在床上却画在另一张图上。
    tex: str = ""

    def is_open(self, hour: float) -> bool:
        """此刻是否营业。hour 为 0..24 的仿真小时（可带小数）。

        - 0→24（或 opens==closes）→ 全天开放（住宅 / 公园 / 公共厕所）；
        - opens < closes → 常规区间 [opens, closes)；
        - opens > closes → 跨夜区间（如 22→6），命中 [opens,24) ∪ [0,closes)。
        """
        if self.opens == self.closes or (self.opens <= 0 and self.closes >= 24):
            return True
        h = hour % 24
        if self.opens < self.closes:
            return self.opens <= h < self.closes
        return h >= self.opens or h < self.closes

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


# 建筑内的「家具点位」全部来自点位编辑器（points.json）。
# 旧实现按业态随机挑格放马桶（家/单位各 2 个、公厕 5~10 个），
# 已按用户要求废弃：编辑器里点在哪儿，哪里才是马桶/床位，没有点位就不生成。


# 默认营业时间（小时）；住宅 / 公园 / 公共厕所全天（不设限），其余业态按下表。
# 这些默认值只是「出厂设置」，用户可在前端逐栋覆盖（改完立即生效）。
# 与 brain.py 的行为时段对齐：办公 7-18、学校 8-16、午休 11:30-13:00 等。
DEFAULT_HOURS: dict[BuildingKind, tuple[float, float]] = {
    BuildingKind.HOME: (0.0, 24.0),
    BuildingKind.PARK: (0.0, 24.0),
    BuildingKind.TOILET: (0.0, 24.0),
    BuildingKind.OFFICE: (7.0, 18.0),     # 与「上班 7-18 点」对齐
    BuildingKind.SCHOOL: (8.0, 16.0),     # 与「上学 8-16 点」对齐
    BuildingKind.HOSPITAL: (7.0, 18.0),   # 门诊作息，与医生上班对齐
    BuildingKind.RESTAURANT: (6.0, 22.0),  # 覆盖早 / 午 / 晚三餐
    BuildingKind.CANTEEN: (7.0, 15.0),    # 只做午市（对应午休 11:30-13:00）
    BuildingKind.SHOP: (8.0, 21.0),       # 覆盖傍晚购物波（16-20 点）
    BuildingKind.BAR: (17.0, 2.0),        # 酒吧：17:00–次日 2:00（跨夜），覆盖夜生活
    BuildingKind.FISHING_POND: (6.0, 18.0),  # 钓鱼池塘：白天开放，与钓鱼效用时段对齐
}


def default_hours(kind: BuildingKind) -> tuple[float, float]:
    """该业态的默认营业时间（opens, closes）；未登记的业态视为全天。"""
    return DEFAULT_HOURS.get(kind, (0.0, 24.0))


# —— 建筑周边铺路开关（2026-11-09）——
# True ：每栋楼上下各铺一条「楼宽 + 2」的 ROAD（旧默认）→ 前端渲染成灰色横杠，
#        鼠标悬停提示为「道路」。这就是「房子背后横竖的灰色杠杠」的来源。
# False：只保留门前 3 格竖引路，房子直接坐在草地上，轮廓由贴图自身表现。
SURROUND_ROAD = False

# 编辑器支持的点位类型（与 web/furniture-editor.html 的 PTYPES 同名）。
# 当前被仿真消费的是 "toilet"（马桶）与 "bed"（床位），其余先原样存进
# Building.furniture，供后续接工位/餐位/柜台落点。
_POINT_TYPES = ("toilet", "bed", "table", "desk", "seat", "counter", "door")


def load_points(path: str | Path | None) -> dict[str, dict[str, dict]]:
    """读取点位编辑器导出的 points.json（v2 按贴图分份；兼容 v1 单份）。

    - 相对路径按**项目根**解析（如默认的 "points.json"）；
    - 文件缺失 / JSON 解析失败 / 结构不对 → 一律返回 {}。
      调用方拿到 {} 就应当「没有点位」，**不允许回落到随机生成**。
    """
    if not path:
        return {}
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    kinds = raw.get("kinds") if isinstance(raw, dict) else None
    if not isinstance(kinds, dict):
        return {}
    out: dict[str, dict[str, dict]] = {}
    for kind, spec in kinds.items():
        if not isinstance(spec, dict):
            continue
        imgs = spec.get("images")
        if isinstance(imgs, dict):                      # v2：每张贴图一份
            bucket: dict[str, dict] = {}
            for rel, one in imgs.items():
                pts2 = one.get("points") if isinstance(one, dict) else None
                if isinstance(pts2, list) and pts2:
                    bucket[str(rel)] = {
                        "grid": one.get("grid") or {"w": 8, "h": 6},
                        "points": [q for q in pts2 if isinstance(q, dict)],
                    }
            if bucket:
                out[str(kind)] = bucket
            continue
        pts = spec.get("points")                        # v1：整类只有一份
        if isinstance(pts, list) and pts:
            out[str(kind)] = {
                str(spec.get("image") or "__default__"): {
                    "grid": spec.get("grid") or {"w": 8, "h": 6},
                    "points": [q for q in pts if isinstance(q, dict)],
                }
            }
    return out


_TEX_MANIFEST = (Path(__file__).resolve().parents[2]
                 / "web" / "assets" / "buildings" / "manifest.json")


def load_texture_rels(path: "str | Path | None" = None) -> dict[str, list[str]]:
    """读 manifest.json → {业态: [贴图 rel, ...]}，即**前端实际会加载**的那套图。

    后端挑图必须限定在这套图里：manifest 里没有的图（例如还留着未抠图的 .jpg 原图）
    一旦被后端挑中，前端要么 404、要么渲染成满幅没去背景的插画；而更根本的问题是
    两边各挑各的，编辑器里摆的点位就落不到实际那栋楼上。
    """
    p = Path(path) if path else _TEX_MANIFEST
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    kinds = data.get("kinds") if isinstance(data, dict) else None
    out: dict[str, list[str]] = {}
    for kind, arr in (kinds or {}).items():
        if not isinstance(arr, list):
            continue
        rels = [str(x) for x in arr if isinstance(x, str) and x]
        if rels:
            out[str(kind)] = rels
    return out


def _spec_points(bucket: dict[str, dict], bid: int,
                 allowed: "list[str] | None" = None) -> tuple[str, list[dict]]:
    """一栋建筑用哪张贴图、以及那张图的点位。

    `allowed` = manifest 里该业态的贴图列表（前端会加载的那套）。有交集时只在这套图里
    按 bid 轮转 —— 同一栋楼永远一致，不同楼可不同。

    返回 (rel, points)；rel == "" 表示挑中的不是前端能加载的真实贴图（如 v1 的 __default__）。
    """
    if not isinstance(bucket, dict) or not bucket:
        return "", []
    pool = [r for r in (allowed or []) if r in bucket]
    if not pool:
        pool = sorted(bucket)     # manifest 缺失 / 路径写法对不上 → 退回点位自身的键序
    if not pool:
        return "", []
    rel = pool[bid % len(pool)]
    pts = bucket[rel].get("points") or []
    return ("", pts) if rel == "__default__" else (rel, pts)


def _snap_inside(grid: Grid, b: Building, x: int, y: int) -> tuple[int, int]:
    """把落在墙上 / 门外的格子收进建筑内部的可行走格（按曼哈顿距离就近搜）。

    点位是按比例换算的，建筑实体尺寸一变就可能压到外墙；这里只做「就近纠偏」，
    不改变用户摆位意图（点在哪半边，就落在哪半边的最近可走格）。
    """
    lo_x, hi_x = b.x + 1, b.x + b.w - 2
    lo_y, hi_y = b.y + 1, b.y + b.h - 2

    def ok(xx: int, yy: int) -> bool:
        return (lo_x <= xx <= hi_x and lo_y <= yy <= hi_y
                and grid.in_bounds(xx, yy) and grid.walkable(xx, yy))

    if ok(x, y):
        return (x, y)
    for _r in range(1, max(b.w, b.h) + 2):
        best: tuple[int, int] | None = None
        best_d = 10 ** 9
        for dy in range(-_r, _r + 1):
            for dx in range(-_r, _r + 1):
                xx, yy = x + dx, y + dy
                d = abs(dx) + abs(dy)
                if d < best_d and ok(xx, yy):
                    best, best_d = (xx, yy), d
        if best:
            return best
    return b.entrance        # 内部全不可走（理论上不会）→ 退回门口


def _resolve_furniture(grid: Grid, b: Building,
                       pts: list[dict]) -> tuple[dict[str, list[tuple[int, int]]],
                                               dict[str, list[tuple[float, float]]]]:
    """把归一化点位（fx/fy ∈ [0,1]）换算成该栋建筑内的具体格子。

    换算口径与编辑器完全一致：col = floor(fx * w)，row = floor(fy * h)，
    再夹进内墙、必要时就近纠偏到可走格。
    **全程无随机**：用户在编辑器里点在哪儿，居民就站到哪儿。
    """
    out: dict[str, list[tuple[int, int]]] = {}
    offs: dict[str, list[tuple[float, float]]] = {}
    for q in pts:
        t = str(q.get("type", "")).strip()
        if t not in _POINT_TYPES:
            continue
        try:
            fx = float(q.get("fx", 0.0))
            fy = float(q.get("fy", 0.0))
        except (TypeError, ValueError):
            continue
        col = min(max(int(fx * b.w), 0), max(0, b.w - 1))
        row = min(max(int(fy * b.h), 0), max(0, b.h - 1))
        # 格内偏移：只留整数格会把「点在格子偏左 / 偏上」这个信息压没，
        # 居民于是永远站格心 —— 这就是「设在哪却不在哪」的根因。
        ox = min(max(fx * b.w - col, 0.0), 1.0)
        oy = min(max(fy * b.h - row, 0.0), 1.0)
        cell = _snap_inside(grid, b, b.x + col, b.y + row)
        bucket = out.setdefault(t, [])
        if cell not in bucket:       # 同一格被点了两次 → 只算一个，不重复占用
            bucket.append(cell)
            offs.setdefault(t, []).append((ox, oy))
    return out, offs


def bind_furniture(grid: Grid, b: Building,
                   points: dict[str, dict[str, dict]] | None,
                   tex_rels: dict[str, list[str]] | None = None) -> None:
    """把点位绑到建筑上：同业态有多张贴图时按 bid 固定挑一张（同一栋楼每次一致）。

    同时把选中贴图的 rel 写进 `b.tex` —— 「哪栋楼用哪张图」只有一个真相源：前端
    /api/map 拿到后直接加载它，不再自己按序号轮转（两套规则各算一遍必然错位）。

    没有配置 → 家具为空（马桶/床位都不生成，绝不回落到随机）。
    """
    bucket = (points or {}).get(b.kind.value)
    allowed = (tex_rels or {}).get(b.kind.value)
    rel, pts = _spec_points(bucket, b.bid, allowed)
    b.tex = rel
    if pts:
        b.furniture, b.furniture_off = _resolve_furniture(grid, b, pts)
    else:
        b.furniture, b.furniture_off = {}, {}
    b.toilets = list(b.furniture.get("toilet", []))


def _place(grid: Grid, b: Building, rng: random.Random) -> None:
    """把建筑画进网格：外墙 WALL，内部 FLOOR，门外开一条路。"""
    for yy in range(b.y, b.y + b.h):
        for xx in range(b.x, b.x + b.w):
            if not grid.in_bounds(xx, yy):
                continue
            edge = xx in (b.x, b.x + b.w - 1) or yy in (b.y, b.y + b.h - 1)
            grid.set_terrain(xx, yy, Terrain.WALL if edge else Terrain.FLOOR)
    # 在下方外墙开一扇门作为入口
    ex = b.x + b.w // 2
    ey = b.y + b.h - 1
    grid.set_terrain(ex, ey, Terrain.FLOOR)
    b.entrance = (ex, ey)
    # 门外的路面延伸一点
    for d in range(1, 4):
        if grid.in_bounds(ex, ey + d):
            grid.set_terrain(ex, ey + d, Terrain.ROAD)
    # 建筑周边一圈铺路，方便通勤
    for xx in (range(b.x - 1, b.x + b.w + 1) if SURROUND_ROAD else ()):   # 默认关闭：不再铺灰路带
        for yy in (b.y - 1, b.y + b.h):
            if grid.in_bounds(xx, yy) and grid.terrain(xx, yy) == Terrain.GRASS:
                grid.set_terrain(xx, yy, Terrain.ROAD)


# 建筑尺寸（格）：2026-11-09 第二次放大 —— 用户「房子可以再大一些」。
# 演进：5~12 × 4~9 → 10~18 × 7~13（第一轮）→ 14~22 × 10~17（本轮）。
# 停在 14~22 的原因：35 栋 × 平均 18×13 格 ≈ 占地图 22%，再大地图就摆不开，
# 只能靠逐级缩小兜底，城里反而会出现大小混着的小房子。
SIZE_W = (14, 22)
SIZE_H = (10, 17)


def _random_rect(rng: random.Random, gw: int, gh: int,
                 shrink: int = 0) -> tuple[int, int, int, int]:
    w = max(5, rng.randint(*SIZE_W) - shrink)   # 尺寸基准见模块顶 SIZE_W / SIZE_H
    h = max(4, rng.randint(*SIZE_H) - shrink)
    x = rng.randint(2, max(2, gw - w - 2))
    y = rng.randint(2, max(2, gh - h - 2))
    return x, y, w, h


def _overlaps(r: tuple[int, int, int, int], others: list[Building]) -> bool:
    x, y, w, h = r
    for b in others:
        if x < b.x + b.w + 2 and x + w + 2 > b.x and y < b.y + b.h + 2 and y + h + 2 > b.y:
            return True
    return False


def _place_rect(rng: random.Random, gw: int, gh: int,
                others: list[Building]) -> tuple[int, int, int, int]:
    """给新楼找位置：标准尺寸试不通就逐级缩小重试。

    旧实现是「试 200 次放不下 → 直接 (2,2,5,4)」，地图一挤就冒出一堆 5×4 小方块，
    比放不下还难看。现在先按标准尺寸试，失败再缩 2 格，尽量保住「大房子」的观感。
    """
    for shrink in (0, 2, 4, 6, 8, 10):
        for _ in range(80):
            r = _random_rect(rng, gw, gh, shrink)
            if not _overlaps(r, others):
                return r
    return (2, 2, 5, 4)   # 极端拥挤才会走到这里


def generate_town(grid: Grid, population: int, rng: random.Random,
                  points: dict[str, list[dict]] | None = None,
                  tex_rels: dict[str, list[str]] | None = None) -> list[Building]:
    """按人口生成城镇：住宅 ≈ 人口/3，另配办公室/学校/餐厅/商店/公园/厕所。

    建筑的**位置与尺寸**仍是随机布局（世界地图每次重开不一样），但室内
    「马桶在哪、床在哪」完全由点位编辑器（points.json）决定：
    `bind_furniture` 把归一化坐标换算成这栋楼里的具体格子。
    传 None / 空字典 → 室内不生成任何马桶与床位，**不回落到随机挑格**。
    """
    buildings: list[Building] = []
    bid = 0

    def add(kind: BuildingKind, label: str, capacity: int = 0) -> Building:
        nonlocal bid
        for _ in range(200):
            r = _random_rect(rng, grid.w, grid.h)
            if not _overlaps(r, buildings):
                break
        else:
            r = _place_rect(rng, grid.w, grid.h, buildings)   # 逐级缩小，别塞 5×4 小方块
        o, c = default_hours(kind)
        b = Building(bid=bid, kind=kind, x=r[0], y=r[1], w=r[2], h=r[3],
                     capacity=capacity, label=label, opens=o, closes=c)
        bid += 1
        _place(grid, b, rng)
        bind_furniture(grid, b, points, tex_rels)          # 点位驱动：无点位即无马桶/床位
        buildings.append(b)
        return b

    def add_adjacent(kind: BuildingKind, label: str, anchor: Building,
                     capacity: int = 0) -> Building:
        """在 anchor 建筑附近紧邻生成一栋小建筑；放不下就退回随机制。"""
        nonlocal bid
        w = rng.randint(8, 12)    # 邻栋（如食堂）同步放大：4~7 → 6~9 → 8~12
        h = rng.randint(6, 9)     # 3~5 → 5~7 → 6~9
        for _ in range(200):
            x = min(max(2, anchor.x + rng.randint(-3, anchor.w + 3)), grid.w - w - 2)
            y = min(max(2, anchor.y + rng.randint(-3, anchor.h + 3)), grid.h - h - 2)
            r = (x, y, w, h)
            if not _overlaps(r, buildings):
                break
        else:
            return add(kind, label, capacity)
        o, c = default_hours(kind)
        b = Building(bid=bid, kind=kind, x=r[0], y=r[1], w=r[2], h=r[3],
                     capacity=capacity, label=label, opens=o, closes=c)
        bid += 1
        _place(grid, b, rng)
        bind_furniture(grid, b, points, tex_rels)          # 点位驱动：无点位即无马桶/床位
        buildings.append(b)
        return b

    # 住宅数按「每栋最多 3 人」定（用户设定：三人一栋房子）：
    # population // 3 在非整除时会漏掉零头，导致最后几栋挤 4 人，故向上取整。
    n_home = max(3, (population + 2) // 3)
    for i in range(n_home):
        add(BuildingKind.HOME, f"住宅{i + 1}", capacity=4)
    add(BuildingKind.OFFICE, "镇政府", capacity=12)
    add(BuildingKind.OFFICE, "工厂", capacity=20)
    gov = add(BuildingKind.OFFICE, "综合办公楼", capacity=8)
    add(BuildingKind.SCHOOL, "小镇学校", capacity=30)
    for i in range(3):
        add(BuildingKind.RESTAURANT, f"餐馆{i + 1}", capacity=20)
    # 食堂：紧邻办公区，作为上班族午休的用餐点（B2 修复新增业态）
    add_adjacent(BuildingKind.CANTEEN, "职工食堂", anchor=gov, capacity=30)
    for i in range(3):
        add(BuildingKind.SHOP, f"商店{i + 1}", capacity=20)
    add(BuildingKind.PARK, "中央公园", capacity=50)
    # 娱乐/社交建筑：酒吧（夜间小酌社交，1~2 栋）+ 钓鱼池塘（白天悠闲社交）。
    for i in range(1 if population < 80 else 2):
        add(BuildingKind.BAR, f"酒吧{i + 1}", capacity=20)
    add(BuildingKind.FISHING_POND, "钓鱼池塘", capacity=15)
    for i in range(4):
        add(BuildingKind.TOILET, f"公共厕所{i + 1}", capacity=4)
    add(BuildingKind.HOSPITAL, "诊所", capacity=10)

    return buildings
