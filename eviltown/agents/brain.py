"""快脑：效用打分选行为。

规则是主体——它能独立跑出一座活着的小镇；LLM（慢脑）只在 M5 提供
「为什么」的叙事与少数高层决策，不能取代规则。所有数值均为无单位效用分，
比较用，非物理量。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum

from ..world.buildings import Building, BuildingKind
from .resident import Job, Resident


class ActionType(str, Enum):
    SLEEP = "sleep"
    EAT = "eat"
    TOILET = "toilet"
    WORK = "work"
    SCHOOL = "school"
    SOCIALIZE = "socialize"
    SHOP = "shop"
    WANDER = "wander"
    GO_HOME = "go_home"
    BAR = "bar"       # 去酒吧（夜间娱乐/社交）
    FISH = "fish"     # 去钓鱼池塘（白天娱乐/社交）
    BUY = "buy"       # 去商店买渔具（钓鱼的前置条件，不许在池塘边顺手买）
    IDLE = "idle"
    TREAT = "treat"   # 生病去诊所看病


@dataclass
class Action:
    kind: ActionType
    target: Building | None = None
    utility: float = 0.0
    reason: str = ""


def _find(buildings: list[Building], kind: BuildingKind) -> Building | None:
    return next((b for b in buildings if b.kind == kind), None)


def _nearest(resident: Resident, buildings: list[Building], kind: BuildingKind,
             hour: float | None = None) -> Building | None:
    """最近的同类建筑；给了 hour 就只在「此刻营业」的建筑里挑（打烊的进不去）。"""
    cands = [b for b in buildings if b.kind == kind
             and (hour is None or b.is_open(hour))]
    if not cands:
        return None
    return min(cands, key=lambda b: abs(b.center[0] - resident.x) + abs(b.center[1] - resident.y))


def _nearest_multi(resident: Resident, buildings: list[Building],
                   kinds: tuple[BuildingKind, ...],
                   hour: float | None = None) -> Building | None:
    """在多个建筑类型里取最近的一个（如「餐馆或食堂」）；同样过滤未营业的。"""
    # 未成年人不得进酒吧（用户口径）：把 BAR 从候选里摘掉。
    # 压力宣泄、社交、主 bar_u 三条路径都走 _pick，摘在这一处即全部覆盖；
    # 摘掉后若没有别的候选，_pick 返回 None，调用方自然退回其他选项（如只去公园）。
    if resident.age < 18 and BuildingKind.BAR in kinds:
        kinds = tuple(k for k in kinds if k is not BuildingKind.BAR)
    cands = [b for b in buildings if b.kind in kinds
             and (hour is None or b.is_open(hour))]
    if not cands:
        return None
    return min(cands, key=lambda b: abs(b.center[0] - resident.x) + abs(b.center[1] - resident.y))


def _pick(resident: Resident, buildings: list[Building],
          kinds: tuple[BuildingKind, ...], rng: random.Random,
          hour: float | None = None) -> Building | None:
    """在同类候选里做「新鲜度 + 距离」加权随机，而不是永远取最近的。

    旧实现一律 `_nearest` → 每个居民一辈子只锁死最近的那家餐馆/厕所/商店，
    全镇 33 栋建筑里人均只用到 5~6 栋（B2 的另一半根因）。
    novelty = 1/(1+到访次数)：没去过的权重高，去过多次的权重低；
    再按 1/(1+距离/25) 让远一点的建筑仍有被选中的机会（而非完全随机跑遍全城）。

    给了 `hour` 时只在「此刻营业」的建筑里挑：打烊的店不会被选为目的地，
    这是「没营业就进不去」的落点（住宅/公园/公共厕所全天开放，不受影响）。
    """
    # 未成年人不得进酒吧（用户口径）：把 BAR 从候选里摘掉。
    # 压力宣泄、社交、主 bar_u 三条路径都走 _pick，摘在这一处即全部覆盖；
    # 摘掉后若没有别的候选，_pick 返回 None，调用方自然退回其他选项（如只去公园）。
    if resident.age < 18 and BuildingKind.BAR in kinds:
        kinds = tuple(k for k in kinds if k is not BuildingKind.BAR)
    cands = [b for b in buildings if b.kind in kinds
             and (hour is None or b.is_open(hour))]
    return _pick_spot(resident, cands, rng)


def _pick_spot(resident: Resident, cands: list[Building],
               rng: random.Random) -> Building | None:
    """在给定候选里按「新鲜度 + 距离」加权抽一个（`_pick` 的抽签部分）。

    单独抽出来，是因为「钓点」的候选不是按业态筛的 —— 池塘那栋楼和
    「画了 seat 点位的公园」混在一起，没法用 kinds 表达。
    """
    if not cands:
        return None

    def weight(b: Building) -> float:
        d = abs(b.center[0] - resident.x) + abs(b.center[1] - resident.y)
        novelty = 1.0 / (1.0 + resident.visited.get(b.bid, 0))
        return (0.5 + 3.0 * novelty) / (1.0 + d / 25.0)

    total = sum(weight(b) for b in cands)
    x = rng.random() * total
    for b in cands:
        x -= weight(b)
        if x <= 0:
            return b
    return cands[-1]


def _fish_targets(buildings: list[Building],
                  hour: float | None = None) -> list[Building]:
    """可钓鱼的落点：池塘那栋楼 + 「在编辑器里画过 seat 点位的公园」。

    公园只有点过座位才算钓位 —— 没画座位的公园不该被当成池塘，否则居民
    跑去草地「钓鱼」，站那儿发呆、白清 dwell。一个座位一个人：占位逻辑在
    `sim._set_target` 的 _SPOT 里（fish → seat）。
    """
    out: list[Building] = []
    for b in buildings:
        if hour is not None and not b.is_open(hour):
            continue
        if b.kind == BuildingKind.FISHING_POND:
            out.append(b)
        elif b.kind == BuildingKind.PARK and b.furniture.get("seat"):
            out.append(b)
    return out


# --- 效用调参（修复「睡眠永远选不上 + 需求活锁」）---
# 夜间睡眠基础效用：必须高于任何「需求驱动」动作的上限。
# 需求封顶 100 时 EAT 上限 1.2×100=120、TOILET 上限 1.4×100=140，
# 故此处必须 >140；旧值 110 时睡眠永远排第二 → 整夜在餐馆/厕所之间来回，从不回家睡觉。
_SLEEP_NIGHT_BASE = 150.0
# 需求阈值：低于此值不参与竞争（无需求就别到处跑；也让封顶值不再碾压睡眠）
_NEED_GATE = 35.0
# --- 上班刚性化（修复「早上不上班」）---
# 工作时段（7:00–18:00）上班的基础效用。必须高于「正常」生理/社交需求的上限
# （EAT 1.2×100=120、TOILET 1.4×100=140、SOCIALIZE 约 95），否则居民一到点就
# 先去吃饭/上厕所/闲聊而不去上班。旧值 50~80 正是被这些分数碾压的根因。
_WORK_BASE = 150.0
# 紧急阈值：需求到达此值才允许「压过在岗」短暂离岗（内急/极饿的合理例外）。
_CRITICAL_NEED = 80.0
# 渔具价（与 sim.Simulation.PRICE_ROD 同步；这里只用于「买不起就别白跑」的判断）
_ROD_PRICE = 600.0
# 吃饭的价格 / 愉悦（与 sim.Simulation.PRICE_EAT_BY_KIND、EAT_JOY_BY_KIND 同步）：
#   在家 8 元 +0 / 食堂 12 元 +2 / 下馆子 60 元 +10
# 愉悦直接加 mood（mood 是压力载体），所以「吃顿好的」顺带把压力往下压一点。
_EAT_HOME_PRICE = 8.0
_EAT_CANTEEN = (12.0, 2.0)
_EAT_RESTAURANT = (60.0, 10.0)


def _meal_opts(r, buildings, home, rng, hour):
    """今天这顿的可选去处：[(建筑, 价格, 愉悦, 说明)]。

    三个去处都留在候选里（**贵 ≠ 不许**，不按钱包砍候选）：
    外面此刻营业的餐馆/食堂挑一家，再加上「自己家」。
    """
    opts = []
    spot = _pick(r, buildings,
                 (BuildingKind.RESTAURANT, BuildingKind.CANTEEN), rng, hour)
    if spot is not None:
        price, joy = (_EAT_RESTAURANT if spot.kind == BuildingKind.RESTAURANT
                      else _EAT_CANTEEN)
        opts.append((spot, price, joy,
                     "下馆子" if spot.kind == BuildingKind.RESTAURANT else "去食堂吃"))
    if home is not None:
        opts.append((home, _EAT_HOME_PRICE, 0.0, "在家做饭吃"))
    return opts


def _meal_best(r, opts):
    """按「−这顿钱的心理负担 + 愉悦」择优，返回 ((建筑, 说明), 分)。

    心理负担模拟的是「掏这笔钱心不心疼」：存款越少、越财迷越重（封顶 50 分）。
      存款 1 万吃 60 元 ≈ 1~2 分（等于没有）→ 下馆子
      存款 500 吃 60 元 ≈ 29 分 → 倒向食堂或家里
      存款见底时封顶 50 分 → 在家做
    于是富人和穷人在账本上自然分化，而不是被规则提前筛掉。
    """
    best, best_s = None, -1e9
    for b, price, joy, why in opts:
        pen = min(50.0, price / max(r.money, 100.0) * 300.0 * (0.4 + r.ocean.greed))
        s = joy - pen
        if s > best_s:
            best, best_s = (b, why), s
    return best, best_s


# ==================================================== 人流 / 光照：软偏好（2026-11-11）
# 这两项都只当「一票」，不设闸门：夜里照样能出门，热闹处不想去也照样去得了。
# 系数定标 —— 社交本体效用 25~60、睡眠/上班 150，所以下面最多 ±15 分，
# 只在「势均力敌」时改变选择，不会把作息与生计顶翻。
_CROWD_PULL = 60.0    # crowd≈0.5 时，外向满格 +15 / 内向满格 -15
_CROWD_MID = 0.35     # crowd 的「常态基线」：街上有几个人就是这个量级
_LIGHT_SLEEP = 12.0   # 天黑 → 睡觉 +
_LIGHT_HOME = 10.0    # 天黑 → 回家 +
_LIGHT_OUT = 8.0      # 天黑 → 闲逛/在外 -（白天反方向）


def _crowd_u(r: Resident, b: Building | None, crowd_hot: dict | None) -> float:
    """热闹对「去哪玩」的吸引力：外向的人被吸引，内向的人躲清静。

    用「(热闹度 - 基线) × (外向 - 0.45)」：热的地方给外向者加分、给内向者
    减分；冷清的地方反过来。想社交的人（social 高）对热闹更敏感 —— 越久
    没说话，越愿意往人堆里扎。没传 crowd_hot 时恒为 0（默认行为不变）。
    """
    if b is None or not crowd_hot:
        return 0.0
    hot = crowd_hot.get(getattr(b, "bid", None), 0.0)
    want = 0.6 + (r.social / 100.0) * 0.8   # 0.6~1.4：社交缺口放大敏感度
    return (hot - _CROWD_MID) * _CROWD_PULL * (r.ocean.extraversion - 0.45) * want


def decide(
    r: Resident,
    buildings: list[Building],
    hour: int,
    is_night: bool,
    rng: random.Random,
    friends_nearby: int = 0,
    at_work: bool = False,
    at_school: bool = False,
    taken: set | None = None,
    crowd_hot: dict | None = None,
    ambient_light: float | None = None,
) -> Action:
    """对每个候选行为算效用，取最高。加一点噪声避免千人一面。

    白天不再只有「上班/上学」一根独苗：午休（11:30–13:00）强制「吃饭」上桌，
    傍晚（16:00–20:00）加「购物」、18:00–21:00 加「回家」，配合「闲逛放大半径」，
    让白天的活动曲线分出通勤 / 午休 / 下班购物 / 回家四段（B2 修复）。

    `at_work` / `at_school` 由主循环传入「人是否已身处工位/教室」。在岗/在校时，
    对「可自由裁量」的行为（社交 / 购物 / 闲逛）大幅扣分，避免到岗后因 dwell 到期
    重决策而被这些更高分吸走离岗（治「上班摸鱼」）；但**不**抑制吃饭/上厕所等生理
    需求——午休与内急时的合理离岗不算摸鱼。
    """
    home = next((b for b in buildings if b.bid == r.home_id), None)
    work = next((b for b in buildings if b.bid == r.work_id), None)
    noise = lambda: rng.uniform(-4, 4)  # noqa: E731
    # 作息抖动：按 rid 派生一个固定的小时偏移（±0.5h），错峰出门。
    # 否则全城会在同一分钟集体上下班/上学，人口一多就露出「整齐划一」的假象。
    hour = hour + ((r.rid * 2654435761) % 1000) / 1000.0 - 0.5

    # 睡眠承诺：夜间一旦入睡就睡到自然醒，避免被吃饭/上厕所等打断，
    # 否则「到家 → 改主意 → 再走」会形成跨建筑活锁（livelock）。
    if is_night and home is not None and r.sleeping and r.energy < 99.5:
        return Action(ActionType.SLEEP, home, 999.0, "睡眠中")

    # 在岗 / 在校判定：工作时段身处工位、或上学时段身处学校 → 抑制可自由裁量的
    # 行为（社交/购物/闲逛），但不锁死生理需求（吃饭/上厕所）。午休 12:00–13:00
    # 不算在岗，可正常离岗吃饭。这是「上班不许出去闲逛」的落点。
    on_duty = (
        not is_night
        and not (12 <= hour < 13)
        and (
            (at_work and work is not None and 9 <= hour < 18)
            or (at_school and 8 <= hour < 16)
        )
    )

    # ---- 生病：看病优先于一切（NEEDS_MODEL.md §7）----
    if r.sick_days > 0:
        hospital = _find(buildings, BuildingKind.HOSPITAL)
        if hospital is not None and hospital.is_open(hour):
            return Action(ActionType.TREAT, hospital, 500.0, "生病了，去诊所看病")
        if home is not None:
            return Action(ActionType.SLEEP, home, 450.0, "生病了，回家躺着")

    # ---- 需求驱动（NEEDS_MODEL.md §3–§4）：最急的那件事说了算 ----
    # 四个生存需求换算成同一刻度（0–100，越高越急），超阈越远越急。
    def _urg(v: float, lo: float, span: float) -> float:
        return max(0.0, min(100.0, (v - lo) / span * 100.0))

    u_food = _urg(60.0 - r.satiety, 0.0, 60.0)      # 饱食 < 60 开始急，0 时满格
    u_toilet = _urg(r.bladder, 50.0, 45.0)          # 膀胱 > 50 开始急
    u_rest = _urg(r.fatigue, 55.0, 45.0)            # 劳累 > 55 开始急
    u_stress = _urg(r.stress, 55.0, 40.0)           # 压力 > 55 开始急

    # 在岗门槛高（小事不许擅离）；自由活动时门槛低，反应更灵。
    gate = 60.0 if on_duty else 20.0
    top_u, top_kind = max(
        (u_food, "eat"), (u_toilet, "toilet"),
        (u_rest, "rest"), (u_stress, "stress"),
    )
    if top_u >= gate:
        if top_kind == "eat":
            # ⚠️ 这里必须和下面的竞争路径共用 `_meal_opts/_meal_best`：
            # 闸门是「先 return」，改下面那段等于没改（实测家选中率 0%）。
            _m, _ = _meal_best(r, _meal_opts(r, buildings, home, rng, hour))
            if _m is not None:
                return Action(ActionType.EAT, _m[0], 100.0 + top_u, _m[1])
        elif top_kind == "toilet":
            spot = _pick(r, buildings, (BuildingKind.TOILET,), rng, hour) or home
            if spot is not None:
                return Action(ActionType.TOILET, spot, 100.0 + top_u, "内急")
        elif top_kind == "rest":
            if home is not None and (is_night or r.fatigue >= 85):
                return Action(ActionType.SLEEP, home, 100.0 + top_u, "太累了，回家睡觉")
            spot = _pick(r, buildings, (BuildingKind.PARK,), rng, hour) or home
            if spot is not None:
                k = ActionType.GO_HOME if spot is home else ActionType.WANDER
                return Action(k, spot, 100.0 + top_u, "累了，找地方歇着")
        else:
            # 压力大：**随机抽一种**放松方式（减压值全都一样，挑哪个纯看运气）。
            # 只从「此刻营业、挑得到」的选项里抽；一样都抽不到就回家歇着。
            opts = [
                (ActionType.WANDER, (BuildingKind.PARK,), "去公园溜达"),
                (ActionType.FISH, (BuildingKind.FISHING_POND,), "去池塘钓鱼"),
                (ActionType.BAR, (BuildingKind.BAR,), "去酒吧坐坐"),
                (ActionType.SOCIALIZE, (BuildingKind.PARK, BuildingKind.BAR), "找人聊两句"),
                (ActionType.SHOP, (BuildingKind.SHOP,), "去商店逛逛"),
                (ActionType.EAT, (BuildingKind.CANTEEN, BuildingKind.RESTAURANT), "吃点东西"),
            ]
            # 神经质决定「烦躁时怎么宣泄」：N 高的人向外发泄（喝酒、溜达），
            # N 低的人自我安抚（吃东西、逛商店）。权重带随机扰动，避免每次都同一个选择。
            _n = r.ocean.neuroticism
            _w = {
                ActionType.WANDER: 1.0 + 1.0 * _n,
                ActionType.BAR: 0.5 + 1.5 * _n,
                ActionType.SOCIALIZE: 1.1 - 0.3 * _n,
                ActionType.FISH: 1.1 - 0.2 * _n,
                ActionType.SHOP: 1.2 - 0.4 * _n,
                ActionType.EAT: 1.3 - 0.6 * _n,
            }
            for k, kinds, why in sorted(
                    opts, key=lambda o: _w[o[0]] * (0.5 + rng.random()), reverse=True):
                spot = _pick(r, buildings, kinds, rng, hour)
                if spot is not None:
                    return Action(k, spot, 100.0 + top_u, "压力大，" + why)
            if home is not None:
                return Action(ActionType.GO_HOME, home, 100.0 + top_u, "压力大，回家歇着")
    scores: list[Action] = []

    # 睡觉：夜里刚需，低能量也睡
    sleep_u = 0.0
    if is_night:
        sleep_u += _SLEEP_NIGHT_BASE
    if r.energy < 25:
        sleep_u += (25 - r.energy) * 2
    # 光照（软作息，2026-11-11）：天黑更想睡、天亮不想睡。
    # 只 ±12 分，不是闸门 —— 白天困极了照样能睡，夜里想出门照样出得去。
    # 没传 ambient_light 时 _dim=0，默认行为完全不变。
    _dim = 0.0 if ambient_light is None else max(0.0, min(1.0, 1.0 - ambient_light))
    sleep_u += _dim * _LIGHT_SLEEP
    scores.append(Action(ActionType.SLEEP, home, sleep_u + noise(), "天黑回家睡觉" if is_night else "太累了"))

    # 吃饭：饥饿驱动（内部 hunger 0=饱/100=饿；对外呈现为「饱食度」低）
    # 低于阈值不参与竞争，避免「不饿也去餐馆」；
    # 午休时段（11:30–13:00）额外给「到点吃饭」信号，制造白天的午餐波峰。
    is_lunch = 11.5 <= hour < 13.0
    meal_opts = _meal_opts(r, buildings, home, rng, hour)
    # 吃饭三去处（用户口径 2026-11-10「明明家里也能吃」）：
    #   自己家 8 元 / 食堂 12 元 / 下馆子 60 元。
    # 旧实现候选只有餐馆与食堂 —— 家里有厨房却从不做饭，饿了只能出门；
    # 而且外面全打烊时 _pick 返回 None，连饭都没得吃（效用直接 -10）。
    # **不按钱包砍候选**（沿用既有原则：贵 ≠ 不许），比的是净效用：
    #     平价基础分 − 这顿钱的心理负担 + 愉悦加成
    # 于是富人的负担轻、下馆子；手头紧的自己在家做 —— 由账本自然分化，
    # 而不是被规则提前筛掉。
    meal, meal_why, meal_u = None, "饿了", -10.0
    _m, _ms = _meal_best(r, meal_opts)
    if _m is not None:
        _base = r.hunger * 1.2 if r.hunger >= _NEED_GATE else 0.0
        if is_lunch:
            _base += 30.0
        if r.hunger >= _CRITICAL_NEED:
            _base += 80.0       # 极饿时可压过在岗，属于合理离岗
        meal, meal_why = _m[0], ("到饭点了，" if is_lunch else "") + _m[1]
        meal_u = _base + _ms + noise()
    scores.append(Action(ActionType.EAT, meal, meal_u, meal_why))

    # 上厕所：默认去公共厕所；若人正在家中，就直接用家里的厕所——用户需求
    # 「上厕所也可以在家解决再去上班」。注意：家中如厕**不降低门槛、不额外加效用**，
    # 仍按 _NEED_GATE 的内急程度参与竞争，从而保住「bladder 每 tick +0.13」的衰减
    # 契约（06:00 开局膀胱=20 < 门槛，首个 tick 不会被清空，回归测试通过）。
    bladder_u = r.bladder * 1.4 if r.bladder >= _NEED_GATE else 0.0
    at_home = home is not None and home.contains(r.x, r.y)
    if at_home and r.bladder >= _NEED_GATE:
        toilet_target, toilet_reason = home, "在家上厕所"
    else:
        toilet_target = _pick(r, buildings, (BuildingKind.TOILET,), rng, hour)
        toilet_reason = "内急"
    if r.bladder >= _CRITICAL_NEED:
        bladder_u += 80.0       # 极度内急时可压过在岗
    scores.append(Action(ActionType.TOILET, toilet_target,
                         bladder_u + noise(), toilet_reason))

    # 上班：工作时段（8:00–18:00）刚性化。基础效用高于「正常」需求与社交的上限，
    # 使居民按时到岗；只有需求到达 _CRITICAL_NEED 的紧急加分，或清晨的「在家上厕所」，
    # 才允许短暂离岗。旧值 50~80 会被需求分碾压 → 早上一到点全城先去吃饭/上厕所，
    # 没人上班（实测 09:00 在岗 0/35）。
    # 岗位会消失：编辑器删了工位、或这栋楼不再是「用人单位」→ 立刻失业。
    # 这是「点位随时变都能跟随」的兜底：人不会一直挂在幽灵岗位上。
    # 已就业者：从 work_id 找回雇主。旧实现只在「本次决策刚应聘成功」时给 work 赋值，
    # 于是已就业的人每 tick 都被下面那句「岗位没了」当成失业者当场解雇 —— 表现就是
    # 反复求职/失业、站不住工位（31 人就业、14:00 仅个位数在岗）。
    if r.work_id is not None:
        work = next((b for b in buildings if b.bid == r.work_id), None)
    if work is not None and work.kind in _NON_EMPLOYER:
        r.work_id, r.job_tile = None, None
        work = None
    if r.work_id is not None and work is None:
        r.work_id, r.job_tile = None, None

    # 求职：待业 + 白天 + 有岗位 → 就近挑一个（打分规则在 _job_score 里）。
    # 不新增动作类型：选上即「当场应聘成功」，今天去不去仍由下面的 WORK 段
    # 与睡觉/吃饭/钓鱼/闲逛竞争 —— 所以「上不上班」始终是他自己的选择。
    if (r.work_id is None and r.job not in (Job.STUDENT, Job.CHILD)
            and not is_night and 7 <= hour < 20):
        best_post, best_s = None, -1e9
        for b, tile in _job_posts(buildings):
            # 一个点位 = 一个岗位：已有人坐的工位不再招人（否则全城挤一处）
            if taken and (b.bid, tile) in taken:
                continue
            sc = _job_score(r, b, home, tile)
            if sc > best_s:
                best_post, best_s = (b, tile), sc
        if best_post is not None and best_s > _JOB_MIN:
            r.work_id, r.job_tile = best_post[0].bid, best_post[1]
            r.job = Job.WORKER
            work = best_post[0]      # 本次决策就用这个雇主，否则 target 为空会退化成闲逛

    work_u = -10.0
    if r.job not in (Job.STUDENT, Job.CHILD) and work is not None:
        # 上班时段跟随「用人单位是否营业」：单位打烊 = 没班上。
        if work.is_open(hour):
            base = _WORK_BASE + r.ocean.conscientiousness * 20
            # 经济动机：缺钱 → 更想上班；存款厚 → 上班吸引力下降。
            # 这两项是「自由选择上不上班」的驱动力，而不是硬排班。
            # 阈值按现实生活费折算：1000 元 ≈ 25 天的吃饭+水电，6000 元算「存款厚」
            # 财迷的「焦虑线」随性格上移：不财迷的人存款跌破 500 才慌，
            # 财迷满格的人掉到 3000 就开始坐不住（阈值高 = 更早开始慌）。
            alarm = 500.0 + r.ocean.greed * 2500.0
            if r.money < alarm:
                money_anx = min(1.0, (alarm - r.money) / alarm)      # 0..1，越见底越急
                # 急到满格时：非财迷 +25、财迷满格 +60 —— 足够压过闲逛与社交
                base += money_anx * (25.0 + r.ocean.greed * 35.0)
            # 存款厚 → 上班吸引力下降（钱多不急着挣）。用 max(0,..) 防负债时反而加分
            base -= min(max(r.money, 0.0), 6000.0) / 6000.0 * 10.0
            base += min(r.jobless_days, 5.0) * 3.0
            # 财务压力 → 想上班。这是「钱的焦虑」的**唯一出口**（不写进 mood，
            # 免得和心情/压力互相喂养）：满格时非财迷 +30、财迷满格 +70。
            base += r.fin_stress * (0.30 + r.ocean.greed * 0.40)
            # 午休（12:00–13:00）弱化工作效用，让「吃饭」能上台，形成午餐离岗
            work_u = 15.0 if 12 <= hour < 13 else base
        elif r.hunger < 60 and not is_night:
            work_u = 10
    scores.append(Action(ActionType.WORK, work, work_u + noise(), "上班"))

    # 上学：只在「此刻营业」的学校里挑；校门未开则上学不出场。
    school = _nearest(r, buildings, BuildingKind.SCHOOL, hour)
    school_u = -10.0
    if r.job in (Job.STUDENT, Job.CHILD) and school is not None:
        school_u = 15.0 if 12 <= hour < 13 else 45 + r.ocean.conscientiousness * 25
    scores.append(Action(ActionType.SCHOOL, school, school_u + noise(), "上学"))

    # 社交：外向者白天更想社交；附近有朋友时更想
    park = _pick(r, buildings, (BuildingKind.PARK,), rng, hour)
    # 社交：外向决定「爱不爱热闹」，亲和决定「喜不喜欢跟人打交道」——两者独立。
    # 亲和低的人在公园里待着并不比独处舒服，所以这一项会被拉负。
    social_u = (r.social * 0.8 + r.ocean.extraversion * 25 - 10
                + (r.ocean.agreeableness - 0.5) * 14)
    social_u += min(friends_nearby, 3) * 8
    social_u += _crowd_u(r, park, crowd_hot)   # 外向的去热闹公园，内向的躲清静
    if is_night:
        social_u -= 40
    scores.append(Action(ActionType.SOCIALIZE, park, social_u + noise(), "找人说说话"))

    # 酒吧：傍晚～深夜的娱乐社交；外向 / 开放者更爱去。深夜（0–2 点）压低，
    # 避免全城熬夜 —— 夜里睡觉仍是刚需（睡眠基础效用 150，酒吧分数够不到）。
    bar = _pick(r, buildings, (BuildingKind.BAR,), rng, hour)
    bar_u = -10.0
    if bar is not None and bar.is_open(hour):
        bh = hour % 24
        if bh >= 17 or bh < 2:
            bar_u = 26 + r.social * 0.7 + r.ocean.extraversion * 24 + r.ocean.openness * 12
            bar_u -= min(r.dwell, 40) * 1.0
            bar_u += _crowd_u(r, bar, crowd_hot)   # 酒吧本来就靠人气，外向者更买账
            if bh < 2:
                bar_u -= 30
    scores.append(Action(ActionType.BAR, bar, bar_u + noise(), "去酒吧坐坐"))

    # 钓鱼：白天（6:00–18:00）的悠闲娱乐 / 社交，强度低、静心，内向者也待得住。
    # 钓点 = 池塘那栋楼 ∪「画了 seat 点位的公园」（见 _fish_targets）。
    pond = _pick_spot(r, _fish_targets(buildings, hour), rng)
    fish_u = -10.0
    if pond is not None and not is_night:
        ph = hour % 24
        if 6 <= ph < 18:
            fish_u = 30 + r.ocean.openness * 20 + r.ocean.extraversion * 12
            fish_u -= min(r.dwell, 40) * 1.0
    if "rod" not in r.items:
        fish_u = -10.0          # 没竿钓不了 —— 先去商店买（见下面 BUY 那条）
    scores.append(Action(ActionType.FISH, pond, fish_u + noise(), "去池塘钓鱼"))

    # 买渔具：钓鱼的前置条件。用户口径 —— 渔具只能在商店买，不许在池塘边顺手掏钱。
    # 有竿后恒为 -10（永不被选中）；买不起也不去（下个 tick 会去上班攒钱）。
    rod_u = -10.0
    rod_shop: Building | None = None
    if ("rod" not in r.items and not is_night
            and 6 <= hour < 18 and r.money >= _ROD_PRICE):
        rod_shop = _pick(r, buildings, (BuildingKind.SHOP,), rng, hour)
        if rod_shop is not None:
            rod_u = 18 + r.ocean.openness * 10
    scores.append(Action(ActionType.BUY, rod_shop, rod_u + noise(), "去商店买渔具"))

    # 购物：傍晚（16:00–20:00）的中段活动；逛久了会「厌倦」离店
    shop = _pick(r, buildings, (BuildingKind.SHOP,), rng, hour)
    shop_u = -10.0
    if shop is not None and not is_night and 16 <= hour < 20:
        shop_u = 24 + r.ocean.openness * 15 + r.ocean.extraversion * 10
        shop_u -= min(r.dwell, 40) * 1.0
    scores.append(Action(ActionType.SHOP, shop, shop_u + noise(), "逛街买东西"))

    # 回家：下班/放学后的傍晚（18:00–21:00）回家，睡前的过渡波
    home_u = -10.0
    if home is not None and 18 <= hour < 21:
        home_u = 26 + r.ocean.conscientiousness * 10 + _dim * _LIGHT_HOME
    scores.append(Action(ActionType.GO_HOME, home, home_u + noise(), "回家"))

    # 闲逛：兜底选项。旧实现只给 20–30 分，永远抢不过钓鱼(40–55)/上班(60+)/内急(80)
    # → 实测一整天「四处走走」出现 0 次（用户：他休息的时候可以闲逛啊）。
    # 休息时段（16–22 点，下班后到天黑前）给一段加分，让「没正事的人出来遛弯」真的发生。
    # 在岗的人不用管 —— 第 315 行 on_duty 会给 WANDER 统一扣 300，照样不摸鱼。
    wander_u = 5 + (0 if is_night else 15) + r.ocean.openness * 10 - _dim * _LIGHT_OUT
    if is_night:
        wander_u -= 25
    elif 16 <= hour < 22:
        wander_u += 18          # 傍晚散步潮：下班的人不计目的地四处走走
    scores.append(Action(ActionType.WANDER, None, wander_u + noise(), "四处走走"))

    # 在岗时把「可自由裁量」的行为压到最低，确保在岗时间留岗做事（不摸鱼），
    # 但吃饭/上厕所/回家等仍可正常竞争胜出（生理需求与下班过渡不受影响）。
    # ======================================== 紧急度裁决（NEEDS_MODEL.md §3–§4）
    # 最急的需求说了算：饱食度跌破 24（= hunger 76）或膀胱过 80 时，
    # 直接压过一切（含在岗）去做 —— 不再让「上班 60 分」把快饿晕的人按在工位上。
    # 疲劳/压力暂不强制离岗：交给下面的原竞价 + on_duty 规则处理（第 3 步后续）。
    def _need100(v: float) -> float:
        return 0.0 if v < 0.0 else (100.0 if v > 100.0 else v)

    u_food = _need100((60.0 - r.satiety) / 60.0 * 100.0)
    u_toilet = _need100((r.bladder - 50.0) / 45.0 * 100.0)
    if max(u_food, u_toilet) >= 60.0:
        want = ActionType.EAT if u_food >= u_toilet else ActionType.TOILET
        pool = [a for a in scores if a.kind is want and a.target is not None]
        if pool:
            best = max(pool, key=lambda a: a.utility)
            best.reason = f"急：{best.reason}"
            return best

    if on_duty:
        for a in scores:
            if a.kind in (ActionType.SOCIALIZE, ActionType.SHOP, ActionType.WANDER):
                a.utility -= 300.0
                a.reason = "在岗，不摸鱼"

    # 同分随机：并列最高分时随机挑一个，避免「永远选列表中靠前的那个」造成行为固化。
    best_u = max(a.utility for a in scores)
    tied = [a for a in scores if best_u - a.utility < 1e-6]
    best = rng.choice(tied)
    if best.target is None and best.kind not in (ActionType.WANDER, ActionType.IDLE):
        # 目标建筑不存在，退化为闲逛
        return Action(ActionType.WANDER, None, best.utility, "目的地不存在，随便走走")
    return best


# ==================================================== 就业：岗位来自点位（2026-11-10）
# 岗位的唯一来源是 points.json 里画的 desk 点位。**每格 = 1 个岗位**，
# 每次现算、绝不缓存 —— 编辑器保存后点位热更新（sim.reload_furniture），
# 岗位数立刻跟着变：加一个工位就多一个岗位，删一个就少一个。
_NON_EMPLOYER = (BuildingKind.HOME, BuildingKind.PARK, BuildingKind.TOILET)

# 工资（元/仿真小时）：业态差异让「上哪个班」成为真实取舍 ——
# 现实时薪标尺：26（商店/餐馆/食堂）～35（酒吧夜班）。
# 酒吧夜班 35 > 诊所 32 > 学校 30 > 办公楼 28 > 商店/餐馆/食堂 26。
_WAGE_BY_KIND = {
    BuildingKind.BAR: 35,
    BuildingKind.HOSPITAL: 32,
    BuildingKind.SCHOOL: 30,
    BuildingKind.OFFICE: 28,
    BuildingKind.SHOP: 26,
    BuildingKind.RESTAURANT: 26,
    BuildingKind.CANTEEN: 26,
}
_JOB_MIN = 24.0     # 岗位打分低于此值 = 今天先不找工作（钓鱼、闲逛也算活法）


def _job_posts(buildings):
    """全城在招岗位：[(建筑, 工位格), ...]。每次从 desk / counter 点位现算。

    **counter（柜台/前台）也是岗位**：商店收银、酒吧调酒、餐馆点单、食堂打饭、
    医院挂号都是真人在岗。旧实现只认 desk，而 points.json 里商店只有 counter
    没有 desk → 商店岗位数 0，「商店一个人都不上班」的观感就是这么来的
    （2026-11-10 用户当场指出）。"""
    posts = []
    for b in buildings:
        if b.kind in _NON_EMPLOYER:
            continue
        furn = getattr(b, "furniture", None) or {}
        tiles = list(furn.get("desk") or []) + list(furn.get("counter") or [])
        for tile in tiles:
            posts.append((b, tile))
    return posts


def _job_score(r, b, home, tile=None) -> float:
    """一个岗位对小人的吸引力 = 工资 + 班次匹配人格 + 就近。

    这里就是「上哪个班由他自己挑」的全部规则：没有职业表、没有配额，
    只有这三项打分 —— 夜猫子自然去酒吧，老实的去办公楼，住得远的懒得跑。
    """
    s = _WAGE_BY_KIND.get(b.kind, 26) * 1.5            # 35→52.5 / 26→39，量级同旧表
    night_shift = b.opens >= 15 or b.closes <= 6       # 跨夜或傍晚开工 = 夜班
    if night_shift:
        s += r.ocean.extraversion * 22 - r.ocean.conscientiousness * 12
    else:
        s += r.ocean.conscientiousness * 22 - r.ocean.extraversion * 6
    if home is not None:
        s -= (abs(b.x - home.x) + abs(b.y - home.y)) * 0.35
    if tile is not None:
        # 错开：同一个岗位对不同人吸引力略有差异，否则全城会挤在同一个工位上
        # （实测 50 人里 5 个人都挑了 bid=29 的 (113,96)）。纯规则、无随机源。
        s += ((r.rid * 37 + b.bid * 11 + tile[0] * 7 + tile[1] * 13) % 17) * 0.35
    # 学历：按业态工资加权 —— 同一个学位，在酒吧/医院比在食堂更「值钱」，
    # 于是高学历的人自然流向高薪行当（这就是「更容易找到好工作」的可算形式）。
    s += r.education * _WAGE_BY_KIND.get(b.kind, 26) * 0.9
    # 行业经验：在本行业待过的人更愿意回去，雇主也更愿意要他（封顶 80 小时）
    _kk = getattr(b.kind, "value", str(b.kind))
    s += min(r.experience.get(_kk, 0.0), 80.0) * 0.5
    return s
