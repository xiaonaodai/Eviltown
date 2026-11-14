"""仿真引擎：世界的「心脏」。

职责边界：
- 拥有网格、时钟、居民列表、建筑列表
- 每 tick：时钟前进 → 需求衰减 → 快脑决策 → 沿路径移动 → 到达后施加效果
- 不碰 HTTP、不碰 LLM。UI 只读取 snapshot()
"""
from __future__ import annotations

import random
from collections import deque

from .dialogue import DialogueRequest, DialogueSynth
from ..world.buildings import (Building, BuildingKind, bind_furniture,
                               generate_town, load_points,
                               load_texture_rels)
from ..world.clock import Clock
from ..world.fields import FieldGrid
from ..world.grid import Grid
from ..world.pathfind import a_star
from . import interactions as ix
from .brain import Action, ActionType, decide, _WAGE_BY_KIND  # 工资表单一真相
from .relations import RelationGraph
from .resident import Job, Ocean, Resident, Sex, make_name, make_name_en
from .wish import attach_wishes
from .narrative import DiaryBook
from ..storage import db as storage_db
from ..storage import repo as storage_repo

# 每 tick（=1 仿真分钟）需求变化率
DECAY = {"hunger": 0.09, "bladder": 0.13, "energy": 0.06, "social": 0.03}

# —— 压力模型（NEEDS_MODEL.md §2 / §5）——
# 用户口径：压力**不随时间自然产生**，只由上班/上学累积；睡觉与各种放松行为消解它。
# 底层写 mood（pressure = 100 − mood），所以减压 = mood 回涨。
STRESS_WORK = 0.10          # 上班 / 上学 每分钟 +0.10（8 小时 ≈ +48）

# 生病（NEEDS_MODEL.md §7；用户口径 C：不看峰值，看「连续高压天数」）
HIGH_STRESS = 70.0          # 压力 ≥ 70 记为「高压分钟」
HIGH_STRESS_MIN = 240       # 一天累计 ≥ 4 小时高压 = 一个「高压日」
STRESS_DAYS_TO_SICK = 3     # 连续 3 个高压日 → 病倒
SICK_DAYS = 3               # 病倒后自然恢复要几天（去诊所可立即病愈）
# 用户口径（2026-11-10）：**所有放松方式减压值一样**，不搞「喝酒比闲聊解压快」的梯度。
# 减多少只看「放松了多久」，挑哪一样纯属随机（见 brain.decide 的压力分支）。
# 好处：行为分布由「随机 + 此刻可不可去」决定，不会被「哪项最划算」吸成单一动作。
RELAX_RATE = 0.06           # 每分钟减压（≈ 3.6 / 小时）

# CA 场对心情的权重（每 tick 读一次该格浓度 0..1，直接乘）。定标依据见 _decay 末尾注释。
# 气味：满格 -0.12/min（一小时 -7.2，≈「在厕所边干一小时活」）；
# 噪声：满格 -0.03/min（8 小时嘈杂 -13，上班压力本身 -35~58，不抢戏）。
SMELL_STRESS = 0.12
NOISE_STRESS = 0.03
# 香味（aroma 场）：满格 +0.10/min（一小时 +6）。比臭味（-0.12）温和 ——
# 好闻是慢慢滋养，臭味是当场呛人。
AROMA_JOY = 0.10
# 打呼：**睡眠噪声的唯一来源**（用户口径 2026-11-11）。只有神经质高的人
# （「神经衰弱」）才被影响，且只扣「睡眠恢复」、不额外加压力。
SNORE_GATE = 0.65      # 神经质低于此值的人照样睡得着
SNORE_PENALTY = 0.5    # 满格神经质 + 1 人打呼 = 恢复砍半；2 人 = 完全睡不回来
STRESS_RELIEF = {           # 未列出的行为 = 0：发呆、在路上走着，压力既不加也不减
    "sleep": RELAX_RATE, "eat": RELAX_RATE, "bar": RELAX_RATE,
    "socialize": RELAX_RATE, "shop": RELAX_RATE, "wander": RELAX_RATE,
    "fish": RELAX_RATE, "go_home": RELAX_RATE,
}

# 健康度趋近目标值的速率（NEEDS_MODEL.md §6）：0.002/分钟 ≈ 半衰期 6 小时。
# 意思是「慢性亏空」——一次没吃饭看不出来，连着几天才垮。
HEALTH_TAU = 0.002
_EPS = 1  # 判定「到达目标」的曼哈顿距离容差
DWELL_CAP = 40  # 在同一目标建筑停留上限（tick）；到点重决策，避免「到店即原地不动」
# 马桶停留时长（tick）：1 tick = 1 仿真分钟，故「触碰马桶停留 1 分钟」= 停留满 1 tick 后结算。
TOILET_DWELL = 1
# 娱乐建筑（酒吧 / 钓鱼池塘）一次到访的结算时长（tick=分钟）：坐 / 钓满 10 分钟
# 才结算一次社交与心情，之后离场；避免「到店即走」与「久泡不走」两个极端。
ENTERTAIN_DWELL = 10
BUY_DWELL = 5   # 在柜台待满 5 分钟才成交（不许「门口路过顺手就买了」）

# ---------------------------------------------------------------- 社交可达性
# 用户口径（2026-11-11）：「认识彼此的条件太苛刻，很多人无法聊天，把认识的范围放大」。
# 实测（tools/_probe_social_reach.py 跑 2 天）：几何机会并**不缺**——每 tick 平均 6.1 对
# 相邻、96.1% 的 tick 都有人碰得上；但 363 条关系边里 **348 条停在「陌生人」**、真正的
# 「认识」只有 15 条，13/50 人的 social 需求长期 > 60。根因是「必须贴脸（距离 ≤1）」×
# 「同一对 12 tick 才试一次、每次 10%」——两者相乘，同一对人几乎永远凑不够「同类交互 3 次」。
# 故把相遇视野放大到 3 格（环内格数 8→48，机会约 6 倍），概率随距离衰减：隔一格的
# 人本来就不如贴脸的人容易搭上话。改这一个数就能调松紧，别去动 cool-down。
SOCIAL_RADIUS = 3           # 相遇视野（切比雪夫距离；1 = 旧的「必须贴脸」）
SOCIAL_DIST_FALLOFF = 0.18  # 每远一格，概率乘 (1-0.18)：3 格时约 0.64 倍



# 合法的动作字符串集合：不在里面的 = 被交互改写过的中文动作名
# （「打招呼」「聊天」「打架」…），_apply 遇到时改按 intent 结算。
# 「必须站到点上停留若干分钟才结算」的动作：买东西 / 钓鱼 / 如厕 / 看病。
# 到达后要给它们一段承诺期，别在 dwell 还没攒够时被别的效应用改走目的地
# （否则到达那一 tick 就被拉去酒吧，dwell 永远归零 → buy 选中 722 次、成交 0 次）。
_STAY_ACTIONS = frozenset({
    ActionType.BUY.value, ActionType.FISH.value,
    ActionType.TOILET.value, ActionType.TREAT.value,
})

_ACT_TEXT = {
    "work": "到岗上班",
    "school": "到校上课",
    "sleep": "回家睡觉",
    "eat": "吃了顿饭",
    "socialize": "找人聊天",
    "buy": "去商店买渔具",
    "fish": "在池塘钓鱼",
    "bar": "去酒吧坐坐",
    "treat": "去医院看病",
}

# 上面那张表的英文版。日志条目随包带出 text_en，前端切英文时直接用，
# 免得前端再维护一份「中文短语 → 英文」的映射（同一份真相只存一处）。
_ACT_TEXT_EN = {
    "work": "At work",
    "school": "At school",
    "sleep": "Home sleeping",
    "eat": "Had a meal",
    "socialize": "Chatting with someone",
    "buy": "Bought fishing gear",
    "fish": "Fishing at the pond",
    "bar": "At the bar",
    "treat": "At the clinic",
}

# ---------------------------------------------------------------- 统计日报口径
# 用户口径（2026-11-10）：白天每 2 小时一个桶，夜里整段一个桶 —— 因为夜里
# 基本没什么活动，拆成十几个桶大半是空的。
#
# ⚠️ 关键：`clock.day` 在 00:00 切，而夜桶是 22:00→次日 06:00，**会横跨两次
# day 切换**。所以「统计意义上的一个仿真日」必须按 06:00 起算（06:00 → 次日
# 05:59），夜桶才连得起来。实现见 `_report_day_of()` / `_bucket_of()`。
DAY_START_TICK = 360            # 06:00，与 clock.start_tick 一致
BUCKET_MIN = 120                # 白天每个桶 2 小时
DAY_BUCKETS = 8                 # 06–08 … 20–22 共 8 个
NIGHT_BUCKET = DAY_BUCKETS      # 第 9 个桶 = 22:00–次日 06:00（整段）
BUCKET_WINDOWS = [
    (6, 8), (8, 10), (10, 12), (12, 14),
    (14, 16), (16, 18), (18, 20), (20, 22),
    (22, 6),                    # 夜桶：跨午夜，from > to
]

# 账本条目名（中文 → 英文）。`_spend()` 与三处直接记账共用这一张表。
_SPEND_EN = {
    "工资": "Wage",
    "水电": "Utilities",
    "吃饭": "Meal",
    "买渔具": "Fishing gear",
    "酒吧消遣": "Bar tab",
    "卖鱼": "Sold fish",
    "病倒（连续高压）": "Collapsed (chronic stress)",
}

# 建筑专名（label）→ 英文。label 是 generate_town 里写死的具体专名
# （「镇政府」「中央公园」「餐馆1」…），集合封闭，故用表精确翻译；
# 尾部的编号由 _bname_en() 摘出来拼回去（「餐馆1」→ "Restaurant 1"）。
_LABEL_EN = {
    "住宅": "House",
    "镇政府": "Town Hall",
    "工厂": "Factory",
    "综合办公楼": "Office Complex",
    "小镇学校": "Town School",
    "餐馆": "Restaurant",
    "职工食堂": "Staff Canteen",
    "商店": "Shop",
    "中央公园": "Central Park",
    "酒吧": "Bar",
    "钓鱼池塘": "Fishing Pond",
    "公共厕所": "Public Toilet",
    "诊所": "Clinic",
}

_ACTION_VALUES = frozenset(t.value for t in ActionType)


# ------------------------------------------------------------------ A2A 对白
# 只给「说得出话」的交互生成对白：友好类的对话型 + 浪漫类的试探型。
# 冲突类（打架/偷窃）不生成 —— 那些已经有模板文案，且不该由模型替人加戏。
_A2A_KINDS = frozenset({
    ix.InteractionKind.GREET, ix.InteractionKind.CHAT,
    ix.InteractionKind.STORY, ix.InteractionKind.COMFORT,
    ix.InteractionKind.INVITE, ix.InteractionKind.FLIRT,
})


def _a2a_context(a: Resident, b: Resident, rel) -> str:
    """把两个人此刻的状态压成一句话，喂给对白模型（**只读**，不改任何状态）。"""

    def one(r: Resident) -> str:
        mood = float(getattr(r, "mood", 50.0))
        hunger = float(getattr(r, "hunger", 0.0))
        return "%d 岁，心情%s，%s" % (
            int(getattr(r, "age", 0)),
            "不错" if mood >= 60 else ("一般" if mood >= 35 else "很差"),
            "肚子有点饿" if hunger >= 60 else "不饿",
        )

    return "A（%s）：%s；B（%s）：%s。两人亲密度 %d/100、信任 %d。" % (
        a.name, one(a), b.name, one(b),
        int(getattr(rel, "intimacy", 0)), int(getattr(rel, "trust", 0)))


def _a2a_request(sim, a: Resident, b: Resident, res) -> DialogueRequest:
    """组装一次对白请求。meta 带上 rid，成品回来时靠它找回这两个人。"""
    rel = sim.relations.get(a.rid, b.rid)
    return DialogueRequest(
        speaker_a=a.name,
        speaker_b=b.name,
        context=_a2a_context(a, b, rel),
        scene=res.kind.value,
        meta={"tick": sim.clock.tick, "day": sim.clock.day, "a": a.rid, "b": b.rid},
    )

class Simulation:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rng = random.Random(20240607)
        self.clock = Clock(cfg.time.ticks_per_day, cfg.time.start_day, cfg.time.start_tick)
        self.grid = Grid(cfg.world.width, cfg.world.height, seed=7)
        # 室内家具（马桶 / 床位）的唯一来源：编辑器导出的 points.json。
        # 读不到 → 空字典 → 室内不生成马桶、不铺床位，**绝不会回落到随机挑格**。
        self.furniture_points = load_points(
            getattr(getattr(cfg, "furniture", None), "points_path", "points.json"))
        # 贴图清单 = 前端真正会加载的那套图（manifest.json）。后端挑图限定在这套里，
        # 选中的 rel 存进每栋的 b.tex，前端直接用 → 只有一份真相，不可能再错位。
        self.tex_rels = load_texture_rels(
            getattr(getattr(cfg, "furniture", None), "textures_manifest", None))
        self.buildings: list[Building] = generate_town(
            self.grid, cfg.population, self.rng,
            points=self.furniture_points, tex_rels=self.tex_rels)
        self.residents: list[Resident] = []
        self._path_cache: dict[tuple, list[tuple[int, int]]] = {}
        # 实际目的地坐标（抖动后的落点），供 _at_target 判定，避免抖到别处却判「未到达」
        self._target_xy: dict[int, tuple[int, int]] = {}
        # 马桶占用表：马桶格 -> 占用者 rid。同一坑位同时只允许一人占用；
        # 后来者若发现所在建筑坑位全满，则走到坑位旁排队等待（见 _toilet_spot）。
        self.toilet_use: dict[tuple[int, int], int] = {}
        self.events: deque[dict] = deque(maxlen=5000)
        # 居民日志（「每人每天干了什么」）：与 events（社会交互）分开 ——
        # events 给事件流用，日志给右侧栏「日志」页用，两者消费方不同。
        self.logs: deque[dict] = deque(maxlen=8000)
        self._log_seq = 0
        self._log_last: dict[tuple[int, str], tuple[int, int]] = {}
        self.paused = False
        # 关系图层（内存版）：社会网随交互生长
        self.relations = RelationGraph()
        self._spawn()
        # 叙事层：日记 + 当天记录导出 md。只在日终跑一次，失败走模板（见 narrative.py）
        self.diary_book = DiaryBook(getattr(cfg, "llm", None))
        # A2A 对白合成（llm.a2a=true 才建）：居民走到相邻格时让 LLM 写「A 说一句 → B 接一句」。
        # 关着 → self.a2a is None，_socialize 走原路径，行为与旧版完全一致；
        # 开着也只是「入队 + 取成品」，HTTP 在守护线程里，永不阻塞 tick（见 agents/dialogue.py）。
        _llm = getattr(cfg, "llm", None)
        self.a2a = (DialogueSynth(
            _llm,
            max_queue=getattr(_llm, "a2a_max_queue", 16),
            min_interval=getattr(_llm, "a2a_min_interval", 6.0),
            max_per_day=getattr(_llm, "a2a_max_per_day", 120),
            max_tokens=getattr(_llm, "a2a_max_tokens", 200),
        ) if _llm is not None and getattr(_llm, "a2a", False) else None)
        # CA 场层：与网格同尺寸，每 tick 演化；与智能体解耦，只被「读」
        stride = getattr(cfg.world, "field_stride", 1)
        self.fields = FieldGrid(self.grid.w, self.grid.h, stride=stride)
        self._setup_fields()
        # M6 落库：跨日快照（可在 config.storage 关闭）
        st = getattr(cfg, "storage", None)
        self.record_events = bool(getattr(st, "record_events", True)) if st else False
        self._event_cursor = 0
        self._con = storage_db.connect(st.db_path) if (
            st and getattr(st, "db_path", None)) else None
        # 统计日报：内存留最近 60 天（接口直接查这里，不必读库），
        # 每 tick 采样、到 06:00 封盘落库（见 _sample_report / _seal_report）
        self.reports: dict[int, dict] = {}
        self._hist = self._blank_hist()

    # ------------------------------------------------------------------ 场层
    # ------------------------------------------------------------------ 点位
    def reload_furniture(self, points: dict[str, list[dict]] | None = None) -> dict:
        """热应用建筑点位：只重绑家具，**不重建仿真**。

        改完 points.json 调这个即可 —— 居民、关系、时钟、事件全部保留：
        - 每栋建筑重跑 `bind_furniture`（furniture / furniture_off / toilets）；
        - 已消失的坑位从占用表释放，避免有人卡在空气位上；
        - 每栋住宅按新 bed 点位重排床位，同栋内不重复；
        - 清掉目标坐标缓存，让在途的人下一轮重算落点。
        """
        if points is None:
            self.furniture_points = load_points(
                getattr(getattr(self.cfg, "furniture", None),
                        "points_path", "points.json"))
        else:
            self.furniture_points = points

        for b in self.buildings:
            bind_furniture(self.grid, b, self.furniture_points, self.tex_rels)

        # 失效坑位释放（建筑上的马桶格变了，占用表要跟着瘦身）
        valid = {t for b in self.buildings for t in b.toilets}
        for t in [t for t in self.toilet_use if t not in valid]:
            self.toilet_use.pop(t, None)

        # 床位重排：一栋房内按点位顺序分配，不重复
        homes = {b.bid: b for b in self.buildings
                 if b.kind == BuildingKind.HOME}
        by_home: dict[int, list] = {}
        for r in self.residents:
            by_home.setdefault(r.home_id, []).append(r)
        moved = 0
        for bid, rs in by_home.items():
            home = homes.get(bid)
            if home is None:
                continue
            taken: set[tuple[int, int]] = set()
            for r in rs:
                tile = self._home_tile(home, taken)
                taken.add(tile)
                if tile != r.home_tile:
                    r.home_tile = tile
                    moved += 1

        self._target_xy.clear()
        return {
            "ok": True,
            "buildings": len(self.buildings),
            "toilets": sum(len(b.toilets) for b in self.buildings),
            "beds": sum(len(b.furniture.get("bed", [])) for b in self.buildings),
            "homes_remapped": moved,
        }

    def _setup_fields(self) -> None:
        """在世界网格上配置持久源：厕所/餐馆出气味，办公室/商店/学校/公园出噪声。"""
        self.fields.sync_blocked(self.grid)
        for b in self.buildings:
            cx, cy = b.center
            if b.kind == BuildingKind.TOILET:
                self.fields.add_emitter("smell", cx, cy, 4, 0.25)
            elif b.kind == BuildingKind.RESTAURANT:
                # 饭香 ≠ 臭味（2026-11-11）：从 smell 挪到 aroma，闻着是享受
                self.fields.add_emitter("aroma", cx, cy, 4, 0.20)
            elif b.kind == BuildingKind.CANTEEN:
                self.fields.add_emitter("aroma", cx, cy, 3, 0.16)
            elif b.kind == BuildingKind.OFFICE:
                self.fields.add_emitter("noise", cx, cy, 4, 0.12)
            elif b.kind in (BuildingKind.SHOP, BuildingKind.SCHOOL):
                self.fields.add_emitter("noise", cx, cy, 3, 0.08)
            elif b.kind == BuildingKind.PARK:
                self.fields.add_emitter("noise", cx, cy, 5, 0.05)
                # 花草香：范围大、强度弱 —— 公园大，别只在中心一小圈才闻得到
                self.fields.add_emitter("aroma", cx, cy, 6, 0.06)
            elif b.kind == BuildingKind.BAR:
                # 酒吧：夜间人声噪声源（热闹），范围略小于公园但强度稍高。
                self.fields.add_emitter("noise", cx, cy, 4, 0.09)
                # 酒香：小范围、明显 —— 路过门口就知道里面在热闹
                self.fields.add_emitter("aroma", cx, cy, 3, 0.14)
            elif b.kind == BuildingKind.FISHING_POND:
                # 池塘：水汽与青草味，很淡，要坐下来才闻得到
                self.fields.add_emitter("aroma", cx, cy, 4, 0.05)

    # ------------------------------------------------------------------ 初始化
    def _spawn(self) -> None:
        homes = [b for b in self.buildings if b.kind == BuildingKind.HOME]
        offices = [b for b in self.buildings if b.kind == BuildingKind.OFFICE]
        shops = [b for b in self.buildings if b.kind == BuildingKind.SHOP]
        schools = [b for b in self.buildings if b.kind == BuildingKind.SCHOOL]
        hospitals = [b for b in self.buildings if b.kind == BuildingKind.HOSPITAL]

        # 岗位不再按人口配额分配（原来是 WORKER 25% / FARMER 10% / SHOPKEEPER 8%…）。
        # 现在职业是「自己找」的：岗位表实时由 points.json 的 desk 点位算出
        # （brain._job_posts），居民出生一律待业，由快脑自由决定上不上班、上哪个班。
        # 学生 / 儿童按年龄走，不参与打工。
        n = self.cfg.population
        jobs: list[Job] = []
        jobs += [Job.STUDENT] * max(1, int(n * 0.20))
        jobs += [Job.CHILD] * max(1, int(n * 0.10))
        while len(jobs) < n:
            jobs.append(Job.UNEMPLOYED)
        jobs = jobs[:n]
        self.rng.shuffle(jobs)
        # 住宅分配随机化：旧实现 homes[i % N] 让 rid 相邻的人住同一栋/相邻栋，
        # 通勤路线天然重合 → 观感上像「成群移动」。打乱后入户落点彻底分散。
        self.rng.shuffle(homes)
        home_slots: dict[int, set[tuple[int, int]]] = {}

        for i in range(n):
            sex = Sex.M if self.rng.random() < 0.5 else Sex.F
            job = jobs[i]
            age = self.rng.randint(6, 12) if job == Job.CHILD else \
                self.rng.randint(16, 22) if job == Job.STUDENT else self.rng.randint(22, 65)
            home = homes[i % len(homes)]

            # 出生不分配雇主：成人一律待业，雇主与工位由快脑求职时当场写定
            # （brain.decide → _job_posts / _job_score）。
            work_id: int | None = None
            # 每人一个固定的个人入户格（= 自己的床位）；同栋住户各自错开一格，
            # 不再叠在同一个 entrance 上 → 夜里数点重合的「抱团」现象消失。
            tile = self._home_tile(home, home_slots.setdefault(home.bid, set()))
            home_slots[home.bid].add(tile)

            r = Resident(
                rid=i, name=make_name(self.rng, sex), name_en=make_name_en(self.rng, sex),
                sex=sex, age=age, job=job,
                ocean=Ocean.random(self.rng), home_id=home.bid, work_id=work_id,
                home_tile=tile,
            )
            # 落点：自家那固定的一个格子
            ex, ey = tile
            r.x, r.y = ex, ey
            r.prev_x, r.prev_y = ex, ey
            self.grid.place(i, ex, ey)
            self.residents.append(r)

        # 长期愿望：先给每人一份模板（瞬时，不阻塞启动），
        # LLM 可用时由 daemon 线程逐批覆盖成模型写的那条（见 agents/wish.py）。
        attach_wishes(self.residents, getattr(self.cfg, "llm", None), self.rng,
                      tick=getattr(self.clock, "tick", 0))

    def _home_tile(self, home: Building, taken: set[tuple[int, int]]) -> tuple[int, int]:
        """给住户分配一个**固定床位**，落点来自编辑器里的 bed 点位。

        规则（用户要求：点位在哪，接线就在哪）：
        1. 依次取该栋 `furniture["bed"]` 中**未被同栋他人占用**的床，
           顺序 = 编辑器里摆放的顺序，全程无随机；
        2. 床位数少于住户（或这栋没配 bed 点位）时，先退到宅内入口，
           再按「离入口近 → 远」的固定顺序挑一个空的可走格（排除马桶格）。
           这是确定性兜底，不是旧实现那种「把屋里随机铺满床位」。
        """
        for bed in home.furniture.get("bed", []):
            if bed not in taken:
                return bed
        ex, ey = home.entrance
        if (ex, ey) not in taken:
            return (ex, ey)
        occupied = set(home.toilets)     # 马桶格不做床位，免得「睡在马桶上」
        inner: list[tuple[int, int]] = []
        for yy in range(home.y, home.y + home.h):
            for xx in range(home.x, home.x + home.w):
                if self.grid.in_bounds(xx, yy) and self.grid.walkable(xx, yy):
                    inner.append((xx, yy))
        inner.sort(key=lambda p: abs(p[0] - ex) + abs(p[1] - ey))
        for c in inner:
            if c not in taken and c not in occupied:
                return c
        return (ex, ey)

    # ------------------------------------------------------------------ 马桶
    def _toilet_spot(self, b: Building, r: Resident) -> tuple[int, int]:
        """为居民挑一个如厕落点。

        规则（用户规格）：先找**空马桶**（离自己最近的那个）；坑位全被占用时
        不去抢位，改为走到坑位旁边**排队等待**（此时 r.toilet_tile=None）。
        占用在「决策时」即预约，避免两人同时奔向同一个坑位而叠加。
        """
        free = [t for t in b.toilets if t not in self.toilet_use]
        if free:
            t = min(free, key=lambda p: abs(p[0] - r.x) + abs(p[1] - r.y))
            self.toilet_use[t] = r.rid
            r.toilet_tile = t
            return t
        r.toilet_tile = None
        return self._queue_spot(b)

    def _queue_spot(self, b: Building) -> tuple[int, int]:
        """坑位全满时的排队等待点：建筑内离马桶最近、且本身不是马桶的可走格。"""
        best, best_d = b.entrance, 10 ** 9
        for yy in range(b.y, b.y + b.h):
            for xx in range(b.x, b.x + b.w):
                if not self.grid.walkable(xx, yy) or (xx, yy) in b.toilets:
                    continue
                d = min((abs(xx - t[0]) + abs(yy - t[1]) for t in b.toilets), default=0)
                if d < best_d:
                    best, best_d = (xx, yy), d
        return best

    def _release_toilet(self, r: Resident) -> None:
        """释放居民占用的马桶格（换目标 / 如厕完成 / 被抢占时调用）。"""
        t = r.toilet_tile
        if t is not None and self.toilet_use.get(t) == r.rid:
            self.toilet_use.pop(t, None)
        r.toilet_tile = None

    # ------------------------------------------------------------------ 寻路
    def _path_to(self, r: Resident, tx: int, ty: int) -> list[tuple[int, int]]:
        key = (r.x, r.y, tx, ty)
        cached = self._path_cache.get(key)
        if cached is not None:
            return list(cached)
        p = a_star((r.x, r.y), (tx, ty), self.grid.walkable, rng=self.rng)
        if len(self._path_cache) > 4000:
            self._path_cache.clear()
        self._path_cache[key] = p
        return list(p)

    def _set_target(self, r: Resident, action: Action) -> None:
        # 记下意图：赶路途中交互会改写 r.action，结算时还要用它。
        r.intent = action.kind.value
        self._release_toilet(r)      # 换目标即让出原先占着的马桶
        r.action = action.kind.value
        r.dwell = 0
        r.stuck = 0
        if action.kind is not ActionType.SLEEP:
            r.sleeping = False
        if action.target is None:
            # 顺路闲逛：在自身邻域内挑一个可走点，走一小段就换目标，
            # 避免「横穿全城走远点」的机械感（原实现是整图随机远点）。
            cx, cy = self._random_stroll(r)
            r.target_id = None
        else:
            r.target_id = action.target.bid
            # 记录到访次数：brain._pick 用它给「没去过的建筑」更高权重，
            # 这是打破「每人只锁死最近那几家店」的关键（原先人均仅到访 5.9 栋）。
            r.visited[r.target_id] = r.visited.get(r.target_id, 0) + 1
            if action.kind is ActionType.SLEEP:
                # 睡眠目标 = 自己的固定床位（home_tile），不做入口抖动：否则
                # 「睡下 → 目标落点在门口附近随机抖 → 人被反复拽向那一格」，
                # 整夜在屋里走个不停（实测夜间 64% 的 tick 在移动）。
                b = action.target
                fallback = b.entrance
                if r.home_id == b.bid and r.home_tile is not None:
                    cx, cy = r.home_tile
                else:
                    cx, cy = fallback
            elif action.kind is ActionType.TOILET and action.target.toilets:
                # 如厕目标 = 具体的某个马桶格（而非入口）：先找空坑位，全满则排队。
                cx, cy = self._toilet_spot(action.target, r)
            else:
                ex, ey = action.target.entrance
                # 目标点抖动：在入口附近随机取一个可走格，避免所有人落同一格
                # → 路径完全重合、扎堆同进同出。
                # ⚠️ 关键约束：落点必须仍在建筑矩形内（b.contains）。entrance 位于
                # 外墙线上，抖动若落到 entrance 下方（ey+1/ey+2 的门外马路）就是
                # 「可走但在楼外」→ 人停在门口马路，_at_workplace/_at_school/
                # 到店判定永远不成立，表现为「编制满员、白天工位却 0 人」。
                b = action.target
                cx, cy = ex, ey
                for _ in range(8):
                    jx = ex + self.rng.randint(-2, 2)
                    jy = ey + self.rng.randint(-2, 2)
                    if b.contains(jx, jy) and self.grid.walkable(jx, jy):
                        cx, cy = jx, jy
                        break
        # 有对应点位就站到点位上 —— 这是 table/desk/seat/counter/idle 真正生效的
        # 唯一入口：覆盖上面 entrance 抖动算出的结果。没点位时才用抖动的落点。
        _SPOT = {"work": "desk", "school": "desk", "eat": "table",
                 "shop": "counter", "buy": "counter", "socialize": "seat",
                 "bar": "seat", "fish": "seat", "wander": "idle"}
        if action.target is not None:
            _cells = action.target.furniture.get(_SPOT.get(action.kind.value, "")) or []
            if _cells:
                _free = [c for c in _cells
                         if not any(o is not r and (o.x, o.y) == c for o in self.residents)]
                _pool = _free or _cells              # 全占满就排队共享
                cx, cy = _pool[r.rid % len(_pool)]   # 确定性取模，避免每 tick 抖
        r.path = self._path_to(r, cx, cy)
        self._target_xy[r.rid] = (cx, cy)
        # a_star 在「起点==终点」时返回 []（表示已到达），与「不可达」同为空值。
        # 若不加区分，居民站在目标格上时会被误判为寻路失败并回落 idle，
        # 导致每 tick「决策→清空→再决策」的死循环，_apply 永不执行。
        if not r.path and (r.x, r.y) != (cx, cy):
            # 寻路失败（目标被围死 / 超出搜索上限）。必须同时清空 target_id：
            # 否则 _at_target() 长期返回 False → 主循环永不重新决策 → 居民永久冻结。
            r.action = "idle"
            r.target_id = None
            self._target_xy.pop(r.rid, None)
            # 兜底：改去邻域走走，保证人一定在动
            sx, sy = self._random_stroll(r)
            r.path = self._path_to(r, sx, sy)
            self._target_xy[r.rid] = (sx, sy)
    def _random_stroll(self, r: Resident, radius: int = 16) -> tuple[int, int]:
        """在自身邻域内挑一个可走点，供「顺路闲逛」；找不到就用原地兜底。

        半径由 8 提升到 16：原来的 8 格≈脚下打转，肉眼看着像「原地不动」，
        故放大到 16 格，闲逛才是真的在城里走动（B2 修复）。
        """
        for _ in range(12):
            cx = min(max(2, r.x + self.rng.randint(-radius, radius)), self.grid.w - 3)
            cy = min(max(2, r.y + self.rng.randint(-radius, radius)), self.grid.h - 3)
            if self.grid.walkable(cx, cy):
                return cx, cy
        return r.x, r.y

    def _at_target(self, r: Resident) -> bool:
        if r.target_id is None:
            return not r.path
        if r.path:
            return False
        b = next((b for b in self.buildings if b.bid == r.target_id), None)
        if b is None:
            return True
        tx, ty = self._target_xy.get(r.rid, b.center)
        return abs(r.x - tx) + abs(r.y - ty) <= _EPS or b.contains(r.x, r.y)

    def _at_home(self, r: Resident) -> bool:
        """居民是否正待在自家住宅建筑内。"""
        b = next((b for b in self.buildings if b.bid == r.home_id), None)
        return b is not None and b.contains(r.x, r.y)

    def set_building_hours(self, bid: int, opens: float, closes: float) -> bool:
        """热更新某栋建筑的营业时间（改完下一 tick 立即生效，无需重启）。

        校验：0 <= opens/closes <= 24。opens==closes 视为全天开放。
        返回是否成功（bid 不存在或数值越界则 False）。
        """
        b = next((bb for bb in self.buildings if bb.bid == bid), None)
        if b is None:
            return False
        if not (0.0 <= opens <= 24.0 and 0.0 <= closes <= 24.0):
            return False
        b.opens, b.closes = float(opens), float(closes)
        return True

    def _arrival_allowed(self, r: Resident) -> bool:
        """到达目的地时该建筑是否仍开门；打烊场所不进门、不结算需求。

        选目标阶段已过滤营业状态，但居民可能走了一段才到、恰逢打烊；
        也可能在外逗留到自家单位/学校关门 —— 统一按「关门就进不去」处理。
        """
        if r.target_id is None:
            return True     # 闲逛等无建筑目标，放行
        b = next((bb for bb in self.buildings if bb.bid == r.target_id), None)
        if b is None:
            return True
        return b.is_open(self.clock.hour + self.clock.minute / 60.0)

    def _at_workplace(self, r: Resident) -> bool:
        """居民是否正身处自己的工位（工作建筑内）。"""
        if r.work_id is None:
            return False
        b = next((b for b in self.buildings if b.bid == r.work_id), None)
        return b is not None and b.contains(r.x, r.y)

    def _at_school(self, r: Resident) -> bool:
        """学生/儿童是否正身处学校内（其 work_id 为 None，需按建筑类型判定）。"""
        b = next((b for b in self.buildings if b.kind == BuildingKind.SCHOOL), None)
        return b is not None and b.contains(r.x, r.y)

    def _nearest_household(self, r: Resident) -> tuple[int, int] | None:
        """寻路失败时的兜底「床」：优先回**自己家**的固定床位，其次自家入口。

        注意：绝不能找「最近的别人家」——那会让居民睡到邻居屋里，
        与「每人有自己的家」相矛盾。
        """
        own = next((b for b in self.buildings if b.bid == r.home_id), None)
        if own is None:
            return None
        if r.home_tile is not None:
            return r.home_tile
        return own.entrance

    # ------------------------------------------------------------------ 落库（M6）
    def _flush_daily(self) -> None:
        """跨日时落一份「刚结束那天」的快照。仅仿真线程访问 SQLite。"""
        if self._con is None:
            return
        day = self.clock.day - 1
        try:
            storage_repo.save_residents(self._con, day, self.residents)
            storage_repo.save_relations(self._con, day, self.relations.snapshot(top=200))
            if self.record_events:
                new = list(self.events)[self._event_cursor:]
                if new:
                    storage_repo.save_events(self._con, self.clock.tick, new)
                self._event_cursor = len(self.events)
            self._con.commit()
        except Exception as exc:          # 落库失败不能拖垮仿真
            print(f"[storage] daily flush failed: {exc}")

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def _work_taken(self) -> set:
        """已被占用的工位（一个 desk 点位 = 一个岗位，一人一位）。

        每次现算、不维护增量表 —— 辞职 / 换岗 / 点位被删都能立刻反映，
        这正是「编辑器随时加点位都能跟上」的保证。
        """
        return {(r.work_id, r.job_tile) for r in self.residents
                if r.alive and r.work_id is not None and r.job_tile is not None}
    # ------------------------------------------------------------------ 主循环
    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            if self.paused:
                return
            prev_day = self.clock.day
            self.clock.advance(1)
            self._roll_report_if_needed()
            if self.clock.day != prev_day:
                for _rr in self.residents:      # 每日水电（×50 标定）
                    self._spend(_rr, self.BILL_DAILY, "水电")
                self._flush_daily()
                self._end_of_day()
                # 叙事：为「刚结束的那天」写日记并导出 md（模板立即就绪，LLM 后台覆盖）
                self.diary_book.on_day_end(self, self.clock.day - 1)
            rng, hour, night = self.rng, self.clock.hour, self.clock.is_night
            taken = self._work_taken()   # 本 tick 的工位占用快照（求职时避让）
            # 居民坐标作为人流场的一次性源
            for r in self.residents:
                if r.alive:
                    self.fields.add_source("crowd", r.x, r.y, 0.3)
            # 人流 / 光照快照（2026-11-11）：每 tick 只读一次（33 栋 × 一次取值），
            # 交给快脑做软偏好 —— 快脑不 import 场，保持纯函数可测。
            crowd_hot = {b.bid: self.fields.value("crowd", b.center[0], b.center[1])
                         for b in self.buildings}
            ambient_light = self.fields.ambient
            # 同屋打呼：谁在哪栋住宅里睡着。打呼的「声源」只存在于睡着的屋里，
            # 不是全城噪声场 —— 睡眠噪声的唯一来源（用户口径）。
            self._snorers = {}
            for r in self.residents:
                if r.alive and r.sleeping and r.home_id is not None:
                    self._snorers[r.home_id] = self._snorers.get(r.home_id, 0) + 1
            for r in self.residents:
                if not r.alive:
                    continue
                self._decay(r, night)
                # 决策：无路径且未在目标任务中
                if not r.path and self._at_target(r):
                    r.stuck = 0
                    if r.target_id is not None:
                        r.dwell += 1
                    act = decide(r, self.buildings, hour, night, rng,
                                 friends_nearby=self._friends_nearby(r),
                                 at_work=self._at_workplace(r),
                                 at_school=self._at_school(r),
                                 taken=taken,
                                 crowd_hot=crowd_hot,
                                 ambient_light=ambient_light)
                    # 本 tick 已认领的工位立刻记入占用：否则同 tick 两个人
                    # 都看到「空位」、都应聘同一格（实测还会剩 2 对重复）。
                    if r.work_id is not None and r.job_tile is not None:
                        taken.add((r.work_id, r.job_tile))
                    # 睡眠态：已到家就安心睡，绝不再重设目标。
                    asleep = r.sleeping and act.kind is ActionType.SLEEP
                    # 只有「换了行为类型」才重设目标。旧逻辑对 SLEEP/WANDER
                    # 无条件重设 → 每 tick「决策→清空路径→再走一段」，
                    # 人永远停在路上（这也是夜间一直动作的放大器）。
                    same_kind = act.kind.value == r.action
                    # 正在办的事还没办完：人已站到目标点、dwell 刚开始累加，
                    # 而当前动作是「须停留才结算」的那几种 → 不许被新决策改写目的地。
                    # 否则到达那一 tick 就被别的效用拉走（buy：选中 722 次 / 成交 0 次）。
                    # dwell 到达 DWELL_CAP 仍放行，防「结算条件永远不满足」把人钉死。
                    busy = (
                        r.dwell > 0
                        and r.target_id is not None
                        and r.action == r.intent
                        and r.action in _STAY_ACTIONS
                        and r.dwell < DWELL_CAP
                    )
                    if not asleep and not busy and (
                        (not same_kind)
                        or act.kind is ActionType.WANDER
                        or (r.target_id is not None and r.dwell >= DWELL_CAP)
                    ):
                        self._set_target(r, act)
                else:
                    # 看门狗：只在「真正卡死」时计数。
                    # 旧实现只要命中本分支就 +1，而本分支在「正常行走」（r.path 非空）
                    # 时也会命中 → 任何超过 8 格的行程都在第 8 tick 被强制重决策，
                    # 路线被反复打断、永远走不到目的地（这是「编制有 32 人、工作楼却
                    # 全天 0 人」的关键之一）。
                    # 正确判据：有路径 = 正在推进 → 清零；只有「无路径且未到达目标」
                    # （寻路失败 / 目标丢失）才累加，达阈值再强制重决策。
                    if r.path:
                        r.stuck = 0
                    else:
                        r.stuck += 1
                        if r.stuck >= 8:
                            self._set_target(r, decide(r, self.buildings, hour, night, rng,
                                                       friends_nearby=self._friends_nearby(r),
                                                       at_work=self._at_workplace(r),
                                                       at_school=self._at_school(r),
                                                       taken=taken,
                                                       crowd_hot=crowd_hot,
                                                       ambient_light=ambient_light))
                # 睡眠者已到家时静默卧床：交互会改写 action 字符串（「聊天」「打架」），
                # 此处按「人在自宅内」把睡眠态纠正回来，别让其被反复吵醒。
                if r.sleeping and night and self._at_home(r) \
                        and r.action != ActionType.SLEEP.value:
                    r.action = ActionType.SLEEP.value
                self._step(r)
                # 在岗计薪：站在自己的工位上才进账，躺家里一分钱都没有
                # —— 这一条才是「自由选择上不上班」真正的约束。
                if r.action == ActionType.WORK.value and self._at_workplace(r):
                    _b = next((b for b in self.buildings if b.bid == r.work_id), None)
                    if _b is not None:
                        # 谈薪：同一张工位，学历与本行业经验越厚时薪越高。
                        # 学识 0→1 = +60%；本行业经验 0→200 小时 = +50%。
                        _kk = getattr(_b.kind, "value", str(_b.kind))
                        _exp = min(r.experience.get(_kk, 0.0), 200.0) / 200.0
                        _mult = 1.0 + 0.6 * r.education + 0.5 * _exp
                        _pay = _WAGE_BY_KIND.get(_b.kind, 600) / 60.0 * _mult
                        # 经验按「在这个业态干了多久」累计（单位：小时）
                        r.experience[_kk] = r.experience.get(_kk, 0.0) + 1.0 / 60.0
                        r.money += _pay
                        r.wage_today += _pay
                        # 工资只累加到「今天」，跨日时由 _end_of_day 汇总成**一笔**入账。
                        # 旧实现每 10 分钟记一笔（每笔 round(0.5*10)=5 元）：一天在岗
                        # 8 小时就是 48 条「+5」，把账本 200 条上限和详情面板的
                        # 「最近 8 笔」全占满，吃饭 / 水电 / 买渔具这些真开销被挤得看不见。
                if r.action != "idle" and self._at_target(r):
                    if not self._arrival_allowed(r):
                        # 抵达时该建筑已打烊（路上走了一段 / 在外逗留到自家单位关门）：
                        # 关门就进不去 —— 不进门、不结算需求，就地清目标重新决策，
                        # 下一 tick 会另选一个「此刻营业」的目的地。
                        r.target_id = None
                        r.dwell = 0
                    else:
                        self._apply(r)
                self._log_activity(r)
                self._sample_report(r)
                # 心情/压力不跟需求挂钩：需求（饿、累、憋）的影响走 health，
                # 心情只由「上班累积压力、睡觉与放松消解」驱动（NEEDS_MODEL.md §2）。
                # 原先这里把 mood 拉向 need_urgency 的基线，导致「一饿就等于压力大」，
                # 与「压力来自消耗、平时不变」的口径冲突，故移除。
            # 相遇交互：等所有人移动完再统一判定，避免顺序偏差
            self._socialize()
            # CA 场演化：在所有智能体行动之后统一推进一步
            self.fields.step(self.clock)

    def _decay(self, r: Resident, night: bool) -> None:
        """每分钟的需求累积（NEEDS_MODEL.md §2）。

        核心口径：干活才累、才饿得快、才有压力；闲着一个都不涨。
        旧实现所有需求恒定速率，于是「上班」只改变位置、不改变身体，
        人也就永远不需要休息 —— 这是本轮改动的根因。
        """
        act = r.action
        sleeping = act == ActionType.SLEEP.value or r.sleeping
        working = act in (ActionType.WORK.value, ActionType.SCHOOL.value)
        sick = r.sick_days > 0

        # 饱食度（内部 hunger，涨=更饿）：上班/上学 ×1.5，睡着 ×0.5，病中再 ×1.2
        k_food = 0.5 if sleeping else (1.5 if working else 1.0)
        if sick:
            k_food *= 1.2
        r.hunger += DECAY["hunger"] * k_food

        # 膀胱：睡着产尿慢，省得半夜一半人被憋醒去厕所
        r.bladder += DECAY["bladder"] * (0.5 if sleeping else 1.0)

        # 劳累（energy 反向）：上班/上学累得快（×1.8，病中 ×1.5），
        # 闲逛几乎不累（×0.3），睡觉倒扣（×4，8 小时睡回满）。
        if sleeping:
            r.energy += DECAY["energy"] * 4.0
        elif working:
            r.energy -= DECAY["energy"] * 1.8 * (1.5 if sick else 1.0)
        else:
            r.energy -= DECAY["energy"] * 0.3

        r.social += DECAY["social"] * (0.5 if night else 1.0)

        # 财务压力：底噪由「存款 vs 焦虑线」给出（穷就一直慌），临时肉痛按每分钟
        # 0.01 消退（一小时淡 0.6 分）。取两者较大值 —— 所以「刚花一笔」的慌会淡，
        # 但「真没钱」不会，钱不回来就一直挂着。（这条线不参与 mood，见 _spend 注释）
        # —— 财富轨迹采样：每 2 小时记一个点（deque 自己丢最老的）——
        if self.clock.tick % 120 == 0:
            r.money_trail.append((self.clock.tick, r.money))
        _fin_alarm = 500.0 + r.ocean.greed * 2500.0
        _fin_base = 0.0
        if r.money < _fin_alarm:
            _fin_base = min(1.0, (_fin_alarm - r.money) / _fin_alarm) * 100.0
        # —— 财富下降速度（用户口径：按「一周」看）——
        # 不比「你现在多穷」，比「你这一周少了多少」。跨度不足一天不谈趋势
        # （工资是日终一笔进账，日内只有支出，短窗口会把一顿饭放大成满格焦虑）；
        # 按实际跨度折算成「每周流失率」，再 ×800 封顶 100 ⇒ 一周流失 12.5% 满格。
        _fin_trend = 0.0
        if len(r.money_trail) >= 2:
            _t0, _m0 = r.money_trail[0]
            _span = max(self.clock.tick - _t0, 1)
            if _span >= 1440 and _m0 > r.money:
                _weekly = (_m0 - r.money) / max(_m0, 500.0) * (10080.0 / _span)
                _fin_trend = min(100.0, _weekly * 800.0)
        r.fin_stress = max(_fin_base, _fin_trend, r.fin_stress - 0.01)

        # 学识只在教室里长（每分钟 0.0002 → 一天 8 小时课约 +0.096，十天满格）
        if act == ActionType.SCHOOL.value:
            r.education = min(1.0, r.education + 0.0002)

        # 压力（底层写 mood：pressure = 100 - mood）：
        # 只有上班/上学累积；睡觉与放松行为消解；平时（赶路、待着）一点不变。
        # 这里不做「回归中性」：任何回归都会把工作攒下的压力悄悄抹掉，
        # 破坏「压力是消耗的产物」这条口径（实测 8 小时只涨到 51 而非 78）。
        if working:
            # 神经质 = 抗压差：N≈0 的人只积 0.7 倍，N≈1 的人积 1.3 倍
            # （均值仍是 1.0，全城压力总量不变，只是重新分配 —— 脆弱的人先垮）
            r.mood -= STRESS_WORK * (0.7 + 0.6 * r.ocean.neuroticism)
        elif sleeping:
            # 打呼（用户口径 2026-11-11）：睡眠噪声**只有打呼噜**，而且只有
            # 神经质高（「神经衰弱」）的人才受影响 —— 受影响的形式是「睡得浅、
            # 恢复打折」，不是额外加压力（睡不好 ≠ 多干了一份活）。
            _relief = STRESS_RELIEF["sleep"]
            _snore = max(0, getattr(self, "_snorers", {}).get(r.home_id, 0) - 1)
            if _snore > 0 and r.ocean.neuroticism > SNORE_GATE:
                _frag = (r.ocean.neuroticism - SNORE_GATE) / (1.0 - SNORE_GATE)
                _relief *= max(0.0, 1.0 - min(1.0, _frag * SNORE_PENALTY * _snore))
            r.mood += _relief
        else:
            r.mood += STRESS_RELIEF.get(act, 0.0)

        # ---- CA 场接线（2026-11-11）：气味 / 噪声开始真的作用于身体 ----
        # 此前 fields.value() 在 Python 侧**零调用**：场每 tick 辛苦演化，结果只喂前端热力图，
        # 项目名里的 CA 属于装饰。现在居民脚下那格的味道与声响直接扣心情。
        # 实测量级（tools/_probe_fields_values.py）：smell 平时 0、碰到即 0.95（厕所/餐馆）；
        # noise 上班时 P90 = 0.9、睡觉时 P90 = 0。所以噪声目前影响的是「工作与休闲」，
        # 「睡眠质量」要等住宅附近有噪声源才谈得上（本轮不造源，避免顺手改作息）。
        _sm = self.fields.value("smell", r.x, r.y)
        _nz = self.fields.value("noise", r.x, r.y)
        # 睡觉时**只听打呼**（上面已扣），不听环境噪声 —— 用户口径：睡眠噪声只有打呼。
        if _sm > 0.0 or (_nz > 0.0 and not sleeping):
            r.mood -= _sm * SMELL_STRESS + (_nz if not sleeping else 0.0) * NOISE_STRESS
        # 香味（2026-11-11）：闻着舒服的地方（餐馆/公园/酒吧）心情慢慢回涨。
        # 与臭味同量级但方向相反 —— 全城嗅觉是「有香有臭」，不是只有坏味道。
        _ar = self.fields.value("aroma", r.x, r.y)
        if _ar > 0.0:
            r.mood += _ar * AROMA_JOY

        # mood 是裸 float、没有 setter，压力口径是 100 - mood ⇒ 必须自己压边界，
        # 否则连续泡在高浓度里会冲成负数、让 pressure 越过 100。
        r.mood = max(0.0, min(100.0, r.mood))
        r.hunger = min(100.0, r.hunger)
        r.bladder = min(100.0, r.bladder)
        r.energy = max(0.0, min(100.0, r.energy))
        r.social = min(100.0, r.social)
        r.mood = max(0.0, min(100.0, r.mood))

        # 高压分钟累计：连续高压「天」由 _end_of_day 统计
        if r.stress >= HIGH_STRESS:
            r.high_stress_min += 1

        self._update_health(r)

    def _end_of_day(self) -> None:
        """跨日结算：连续高压天数 → 病倒（用户口径 C）。

        只看「今天高压够不够久」，不看峰值：连着 3 天每天高压 4 小时以上就倒，
        不必某个瞬间冲到 85。中间有一天放松下来，连续计数清零重新数。
        """
        for r in self.residents:
            if not r.alive:
                continue
            # 当天工资汇总成一笔（不足 0.5 元的零头不记，免账本刷满「工资 +0.12」）
            if r.wage_today >= 0.5:
                r.ledger.append((self.clock.day, round(r.wage_today, 2),
                                 "工资", _SPEND_EN["工资"]))
            r.wage_today = 0.0
            if r.high_stress_min >= HIGH_STRESS_MIN:
                r.stress_days += 1
            else:
                r.stress_days = 0
            r.high_stress_min = 0
            if r.sick_days <= 0 and r.stress_days >= STRESS_DAYS_TO_SICK:
                r.sick_days = SICK_DAYS
                r.health = min(r.health, 45.0)
                r.stress_days = 0
                r.ledger.append((self.clock.day, 0.0, "病倒（连续高压）", _SPEND_EN["病倒（连续高压）"]))
            elif r.sick_days > 0:
                r.sick_days -= 1        # 不去看病也会慢慢好

    def _update_health(self, r: Resident) -> None:
        """健康度朝「由需求算出的目标值」缓慢趋近（NEEDS_MODEL.md §6）。

        不直接扣分：饿、过劳、高压、憋尿都是慢性侵蚀，
        要连续亏空才看得出来；否则一顿没吃饭就掉血，人像在跳崖。
        """
        target = 100.0
        target -= 2.0 * max(0.0, 40.0 - r.satiety)       # 长期吃不饱
        target -= 1.5 * max(0.0, r.fatigue - 70.0)       # 长期过劳
        target -= 2.0 * max(0.0, r.stress - 70.0)        # 长期高压
        target -= 1.0 * max(0.0, r.bladder - 90.0)       # 长期憋着
        target = max(0.0, min(100.0, target))
        r.health += (target - r.health) * HEALTH_TAU
        r.health = max(0.0, min(100.0, r.health))
        # 身体被长期亏空拖垮（不是压力导致的）：健康跌破 25 直接病倒
        if r.health < 25.0 and r.sick_days <= 0:
            r.sick_days = 2

    def _point_offset(self, r: Resident) -> tuple[float, float]:
        """居民当前格对应的**格内偏移**（编辑器里点在格里的哪个位置）。

        找不到对应点位就退回格心 (0.5, 0.5)，保证旧行为与无点位建筑不受影响。
        """
        if r.target_id is None:
            return (0.5, 0.5)
        b = next((bb for bb in self.buildings if bb.bid == r.target_id), None)
        if b is None:
            return (0.5, 0.5)
        for kind, cells in b.furniture.items():
            offs = b.furniture_off.get(kind) or []
            for i, cell in enumerate(cells):
                if cell == (r.x, r.y) and i < len(offs):
                    return offs[i]
        return (0.5, 0.5)

    def _step(self, r: Resident) -> None:
        if not r.path:
            # 停住了 → 站到点位对应的格内位置（编辑器设在哪就在哪）
            r.off_x, r.off_y = self._point_offset(r)
            return
        r.off_x, r.off_y = 0.5, 0.5   # 走动中 → 走格心，别歪着走
        # 顺路闲逛：闲逛途中偶发「驻足一拍」，走停结合，动线不生硬。
        if r.action == ActionType.WANDER.value and self.rng.random() < 0.14:
            return
        nx, ny = r.path.pop(0)
        r.prev_x, r.prev_y = r.x, r.y
        self.grid.move_occupant(r.rid, r.x, r.y, nx, ny)
        r.x, r.y = nx, ny

    # ---- 物价（现实人民币标尺：时薪 26–35，初始本金 1 万 ≈ 一个多月工资）----
    # 按频次分三类：日频刚需（吃饭/水电）、低频大额（渔具/看病）、消遣（酒吧）。
    PRICE_EAT = 30.0        # 兜底：站在非餐馆/食堂的楼里吃
    # 按业态分档：现实一顿 10–200 的跨度来自「在哪吃」——食堂 12 / 下馆子 60。
    PRICE_EAT_BY_KIND = {
        BuildingKind.CANTEEN: 12.0,
        BuildingKind.RESTAURANT: 60.0,
    }
    # 在家自己做：只算食材成本，比食堂 12 还便宜（用户口径 2026-11-10
    # 「在家吃饭更便宜」）。旧实现没有这一档，落进 PRICE_EAT 兜底 30 元 ——
    # 比食堂贵一倍半，家里做饭反而成了最奢的选择。
    PRICE_EAT_HOME = 8.0
    # 一顿饭的「精神愉悦」（加心情 0~100）：在家只是填肚子（+0），食堂略有（+2），
    # 下馆子是消费带来的快感（+10）—— 用户口径「贵饭馆不只加饱食度，还加愉悦」。
    # 这是富人晚饭和穷人不一样的第二重原因：价差不只在账本上，还在心情上。
    EAT_JOY_BY_KIND = {
        BuildingKind.CANTEEN: 2.0,
        BuildingKind.RESTAURANT: 10.0,
    }
    PRICE_BAR = 120.0       # 一杯 + 消遣，落在吃饭 10–200 的上段
    PRICE_ROD = 600.0       # 渔具 200–1000，一次性买断、可反复用
    PRICE_HOSPITAL = 0.0    # 医保报销：看病不花钱（_spend 对 0 元直接短路，不留账）
    BILL_DAILY = 6.0        # 水电 5–7/天，取中
    FISH_SELL = 25.0        # 钓上一条卖给商店

    def _spend(self, r: Resident, amount: float, what: str) -> None:
        """扣钱并记账。允许透支为负（负债）—— 否则身无分文的人只能饿死在家里，
        而「必须去上班」的压力反而消失了。"""
        if amount <= 0:         # 0 元消费（如医保报销后的看病）不扣钱、也不记账
            return
        # 财迷肉痛：这笔钱占存款比例越大越难受，但只记到**财务压力**这条独立线上，
        # 绝不动 mood —— mood 是压力/心情，掺钱会形成「花钱→难受→去酒吧→更花钱」的
        # 恶性循环（用户 2026-11-10 指出，原来的写法确实是正反馈）。
        # 分母用「扣钱前」的存款（r.money + amount），下限 500 元，免得身无分文时
        # 花 12 块就被判成「花掉全部家当」；临时肉痛会按分钟消退（见 _decay）。
        if r.ocean.greed > 0.0:
            share = amount / max(r.money + amount, 500.0)
            r.fin_stress = min(100.0, r.fin_stress + share * 120.0 * r.ocean.greed)
        r.money -= amount
        r.ledger.append((self.clock.day, -round(amount, 2), what, _SPEND_EN.get(what, what)))
        if len(r.ledger) > 200:
            del r.ledger[:100]

    def _eat_price(self, r: Resident) -> float:
        """这一顿花多少：按就餐地点的业态分档（食堂 12 / 下馆子 60）。

        价格挂在建筑业态上而不是「吃什么」上，人才会用脚投票 —— 手头紧的去食堂，
        宽裕的下馆子，账本里于是自然出现两种单价。
        """
        b = next((bb for bb in self.buildings if bb.bid == r.target_id), None)
        kind = b.kind if b is not None else None
        if kind == BuildingKind.HOME:
            return self.PRICE_EAT_HOME      # 在家自己做：食材成本 8 元
        return self.PRICE_EAT_BY_KIND.get(kind, self.PRICE_EAT)

    def _eat_joy(self, r: Resident) -> float:
        """这一顿带来的精神愉悦（直接加 mood）。贵的地方不止填肚子 ——
        用户口径 2026-11-10。在家 / 非餐饮楼里吃 = 0。"""
        b = next((bb for bb in self.buildings if bb.bid == r.target_id), None)
        kind = b.kind if b is not None else None
        return self.EAT_JOY_BY_KIND.get(kind, 0.0)

    def _apply(self, r: Resident) -> None:
        # 交互（_socialize）会在赶路途中把 r.action 改写成「打招呼」等中文动作名，
        # 人却仍朝原目标走。此时若按 r.action 分发，到达商店也匹配不到 BUY 分支
        # → 渔具永远买不成（实测选中 722 次、成交 0 次）。故用 intent 兜底。
        a = r.action
        if a not in _ACTION_VALUES and r.target_id is not None and r.intent:
            a = r.intent
        if a == ActionType.EAT.value:
            # 到达即结算：一顿饭把饥饿清到低位。旧实现每 tick 只扣 2.5，而饥饿按
            # 0.16/分钟持续增长、行程又被看门狗反复打断（很少有人真正坐定点位），
            # 「吃一顿饭」的净收益被增长抹平 → 饥饿只涨不落（需求活锁根因之二）。
            # 清到低位后，下次决策里 EAT=0 自然落选 → 居民会离开餐馆。
            _p = self._eat_price(r)
            self._spend(r, _p, "吃饭")
            r.hunger = min(r.hunger, 5.0)
            # 精神愉悦：下馆子 +10、食堂 +2、家里 +0。心情（mood）是压力载体，
            # 所以这一口好的顺带把压力往下压一点 —— 富人靠吃，穷人靠睡。
            _joy = self._eat_joy(r)
            if _joy > 0.0:
                r.mood = min(100.0, r.mood + _joy)
            self._log(r, "eat", "在%s吃了顿饭（%d 元）" % (
                self._bname(r.target_id) or "街上", int(_p)), cooldown=60,
                text_en="Had a meal at %s (%d yuan)" % (
                    self._bname_en(r.target_id) or "the street", int(_p)))
            r.dwell = 0
        elif a == ActionType.TOILET.value:
            # 触碰马桶并停留满 TOILET_DWELL 分钟才清空膀胱（用户规格）。
            # dwell 由主循环在「已到达目标」时每 tick +1，故这里只判定、不再自增。
            t = r.toilet_tile
            if t is None or (r.x, r.y) != t:
                return              # 仍在排队 / 尚未真正站到马桶格上 → 不结算
            if self.toilet_use.get(t) != r.rid:
                # 坑位被他人占用（预约冲突）：让位，下个 tick 重新决策（找空位或排队）
                self._release_toilet(r)
                r.action = "idle"
                r.target_id = None
                r.dwell = 0
                return
            if r.dwell >= TOILET_DWELL:
                r.bladder = 0.0
                self._log(r, "toilet", "上厕所（%s）" % (
                    self._bname(r.target_id) or "公厕"), cooldown=120,
                    text_en="Toilet (%s)" % (
                        self._bname_en(r.target_id) or "public restroom"))
                self._release_toilet(r)
                r.dwell = 0
                r.target_id = None
                r.action = "idle"   # 用完即离场，下个 tick 重新决策
        elif a == ActionType.SLEEP.value:
            # 提升回能速率：原 0.4/tick 太慢，整夜 8 小时也只回约 29 点，
            # 导致「睡眠承诺」几乎无法自然醒来。1.2/tick ⇒ 一夜可回满。
            r.energy = min(100.0, r.energy + 1.2)
            r.sleeping = True
            self._log(r, "sleep", "在%s睡觉" % (
                self._bname(r.target_id) or "家"), cooldown=600,
                text_en="Sleeping at %s" % (
                    self._bname_en(r.target_id) or "home"))       # 持久睡眠态：交互改写 action 后仍能续睡
        elif a == ActionType.TREAT.value:
            # 看病（医保报销 0 元）：停在诊室按分钟回血，停留满一小时病愈。
            # 这是「连续高压病倒」之后最快的出路 —— 不来医院就一直扛着。
            r.health = min(100.0, r.health + 0.15)
            if r.dwell >= 60:
                r.sick_days = 0
                r.health = max(r.health, 60.0)
                self._log(r, "treat", "看病（%s，医保报销）" % (
                    self._bname(r.target_id) or "医院"), cooldown=600,
                    text_en="Clinic visit (%s, covered by insurance)" % (
                        self._bname_en(r.target_id) or "clinic"))
                r.dwell = 0
                r.action = "idle"
                r.target_id = None
        elif a == ActionType.SOCIALIZE.value:
            r.social = max(0.0, r.social - 0.8)
        elif a == ActionType.BUY.value:
            # 到商店买渔具：站在柜台（有 counter 点位就站上去）待满 BUY_DWELL 分钟
            # 才成交。买完立刻离店 → 下个 tick 再决策，「买竿」和「钓鱼」是两趟
            # 独立行程（用户口径：渔具必须去商店买，不能在池塘边顺手掏钱）。
            b = next((bb for bb in self.buildings if bb.bid == r.target_id), None)
            if b is None or b.kind != BuildingKind.SHOP:
                r.target_id = None
                r.action = "idle"
                r.dwell = 0
                return
            if r.dwell >= BUY_DWELL:
                if r.money < self.PRICE_ROD:
                    r.target_id = None      # 到店发现钱不够 → 走人（上班攒钱去）
                    r.action = "idle"
                    r.dwell = 0
                    return
                self._spend(r, self.PRICE_ROD, "买渔具")
                r.items["rod"] = True
                self._log(r, "buy", "在%s买了根鱼竿（%d 元）" % (
                    self._bname(r.target_id) or "商店",
                    int(self.PRICE_ROD)), cooldown=600,
                    text_en="Bought a fishing rod at %s (%d yuan)" % (
                        self._bname_en(r.target_id) or "the shop",
                        int(self.PRICE_ROD)))
                r.target_id = None
                r.action = "idle"
                r.dwell = 0
        elif a in (ActionType.BAR.value, ActionType.FISH.value):
            # 娱乐建筑（酒吧 / 钓鱼池塘）：停留满 ENTERTAIN_DWELL 分钟后结算一次
            # ——社交大幅回复 + 心情小幅上调——随后清空目标离场，避免「泡在酒吧/
            # 池塘一整天」或「到店即走」。dwell 由主循环在「已到达目标」时每 tick +1。
            if r.dwell >= ENTERTAIN_DWELL:
                if a == ActionType.BAR.value:
                    self._spend(r, self.PRICE_BAR, "酒吧消遣")
                else:
                    # 鱼竿只在商店买（见 ActionType.BUY 分支）。这里绝不就地补买 ——
                    # 下面只是防御：没竿就走人，绝不白送一根。
                    if "rod" not in r.items:
                        r.target_id = None
                        r.action = "idle"
                        r.dwell = 0
                        return
                    r.money += self.FISH_SELL      # 钓上一条，卖给商店
                    r.ledger.append((self.clock.day, self.FISH_SELL, "卖鱼", _SPEND_EN["卖鱼"]))
                if a == ActionType.BAR.value:
                    self._log(r, "bar", "在%s消遣（%d 元）" % (
                        self._bname(r.target_id) or "酒吧",
                        int(self.PRICE_BAR)), cooldown=120,
                        text_en="Hanging out at %s (%d yuan)" % (
                            self._bname_en(r.target_id) or "the bar",
                            int(self.PRICE_BAR)))
                else:
                    self._log(r, "fish", "在%s钓到一条，卖了 %d 元" % (
                        self._bname(r.target_id) or "池塘",
                        int(self.FISH_SELL)), cooldown=120,
                        text_en="Caught one at %s, sold for %d yuan" % (
                            self._bname_en(r.target_id) or "the pond",
                            int(self.FISH_SELL)))
                r.social = max(0.0, r.social - 30.0)
                r.mood = max(0.0, min(100.0, r.mood + 4.0))
                r.dwell = 0
                r.target_id = None
                r.action = "idle"   # 用完即走，下个 tick 重新决策
        elif a in (ActionType.GO_HOME.value, ActionType.IDLE.value):
            # 到家后立即重决策（dwell=0），让「出门前先在家上厕所」这类清晨行程
            # 能紧接着发生，而不是傻站着等 DWELL_CAP 到期。
            r.dwell = 0

    def _friends_nearby(self, r: Resident, radius: int = 6) -> int:
        """半径内的朋友数，供快脑的社会项打分。"""
        cnt = 0
        for fid in self.relations.friends_of(r.rid):
            f = self.residents[fid]
            if abs(f.x - r.x) + abs(f.y - r.y) <= radius:
                cnt += 1
        return cnt

    # ------------------------------------------------------------------ 社会交互
    def _socialize(self) -> None:
        """视野内（切比雪夫距离 ≤ SOCIAL_RADIUS）的两人有机会发生一次交互。

        一个人的「一次」是字面意思：触发后 break，不在同一 tick 里跟身边所有人
        挨个打招呼 —— 半径放大到 3 格后公园里一聚十几人，不 break 会让日志和

        关系图同时爆掉。

        规则驱动：关系越深越可能互动；结果写进关系图 + 心情 + 事件流。
        """
        day = self.clock.day
        tick = self.clock.tick
        rs = self.residents
        self._drain_a2a()
        n = len(rs)
        for i in range(n):
            a = rs[i]
            if not a.alive:
                continue
            # 通勤/赶路保护：正在前往工位/学校的人不参与闲聊——否则一出门就被邻居
            # 拦下反复「打招呼/聊天」，永远走不到工作地点（实测 09:00 在岗 0/35，
            # 且上班族 34/35 曾被聊天/如厕打断）。上学、放学同理不拦。
            a_commuting = bool(a.path) and a.action in ("work", "school")
            for j in range(i + 1, n):
                b = rs[j]
                if not b.alive:
                    continue
                # 快速剪枝：先比 x 距离（绝大多数邻居一步排除），再精确判切比雪夫距离。
                if abs(b.x - a.x) > SOCIAL_RADIUS:
                    continue
                dx = abs(a.x - b.x)
                dy = abs(a.y - b.y)
                if dx > SOCIAL_RADIUS or dy > SOCIAL_RADIUS or (dx == 0 and dy == 0):
                    continue
                # 睡觉的人不搭话：半径放大后，同一栋住宅里几张床可能相距 ≤3 格。
                if a.action == ActionType.SLEEP.value or b.action == ActionType.SLEEP.value:
                    continue
                if a_commuting or (bool(b.path) and b.action in ("work", "school")):
                    continue
                # 闲聊冷却：同一人在 12 tick 内不重复触发，避免邻里原地寒暄不停。
                if tick - a.last_talk_tick < 12 or tick - b.last_talk_tick < 12:
                    continue
                # 触发概率：陌生偶遇低，熟人更高（0.10 ~ 0.30）
                rel0 = self.relations.get(a.rid, b.rid)
                dist = max(dx, dy)
                p = (0.10 + rel0.intimacy / 400.0) * max(
                    0.2, 1.0 - SOCIAL_DIST_FALLOFF * (dist - 1))
                if self.rng.random() > p:
                    continue
                kind = ix.choose(self.rng, a.ocean, b.ocean, rel0, a.mood, b.mood)
                res = ix.apply(kind, rel0)
                # 「认识」判定：同一类非冲突交互累计 3 次（打招呼 3 次 / 聊天 3 次
                # 各算各的，互不叠加）。冲突类（争吵/打架/偷窃…）不计入——用户规格。
                met_kind = None if res.category is ix.Category.CONFLICT else res.kind.value
                self.relations.adjust(
                    a.rid, b.rid, trust=res.trust_delta,
                    intimacy=res.intimacy_delta, day=day, kind=met_kind)
                if res.married:
                    self.relations.marry(a.rid, b.rid)
                a.social = max(0.0, a.social - res.social_gain)
                b.social = max(0.0, b.social - res.social_gain)
                a.mood = max(0.0, min(100.0, a.mood + res.mood_self))
                b.mood = max(0.0, min(100.0, b.mood + res.mood_other))
                a.action = res.kind.value   # 让 UI 看到「正在互动」
                b.action = res.kind.value
                a.last_talk_tick = tick
                b.last_talk_tick = tick
                self._emit(a, b, res)
                # A2A：把这对人丢给后台线程写对白，绝不在这里等 HTTP。
                # 成品由下一轮 _drain_a2a() 取回；未启用 / 解析失败时什么都不发生。
                if self.a2a is not None and res.kind in _A2A_KINDS:
                    self.a2a.submit(_a2a_request(self, a, b, res))
                break   # 一人一 tick 只搭一次话

    def _drain_a2a(self) -> None:
        """取走后台线程已写好的对白，落进居民日志（主线程写，非阻塞）。

        - `self.a2a is None`（llm.a2a 关闭）→ 直接返回，这条路径连 deque 都不碰；
        - 没有成品 → 一次空检查，开销可忽略；
        - 有成品 → 每段一句「A：… B：…」进日志页。**不写 events**：事件流是前端
          按 kind 渲染的封闭协议，A2A 只在日志页呈现，免得前端出现未知 kind。
        """
        if self.a2a is None:
            return
        for line in self.a2a.poll():
            meta = line.meta or {}
            rid = meta.get("a")
            if not isinstance(rid, int) or not (0 <= rid < len(self.residents)):
                continue
            r = self.residents[rid]
            if not r.alive:
                continue
            text = line.render()
            # kind 用已存在的 "socialize"（前端有图标/文案）；force=True 绕过同类冷却 ——
            # 刷屏风险已经由 dialogue.py 的最小间隔与每日上限挡掉了。
            self._log(r, "socialize", text, force=True, text_en=text)

    def _log(self, r: Resident, kind: str, text: str,
             cooldown: int = 90, force: bool = False, text_en: str = "") -> None:
        """记一条居民日志。

        cooldown 是防刷屏的关键：`_apply()` 里 EAT/SLEEP/TREAT 之类是「每 tick 都
        会走到」的分支（结算后动作不一定立刻换掉），不加冷却会一分钟写 60 条，
        日志页全是回车换行没内容。同人同类在 cooldown 分钟内的重复一律丢弃。
        """
        day, tick = self.clock.day, self.clock.tick
        key = (r.rid, kind)
        if not force:
            prev = self._log_last.get(key)
            if prev is not None and prev[0] == day and tick - prev[1] < cooldown:
                return
        self._log_last[key] = (day, tick)
        self._log_seq += 1
        self.logs.append({
            "id": self._log_seq,
            "day": day,
            "tick": tick,
            "rid": r.rid,
            "name": r.name, "name_en": r.name_en,
            "kind": kind,
            "text": text,
            "text_en": text_en,
            "bid": r.target_id,
        })

    def _bname(self, bid: int | None) -> str:
        if bid is None:
            return ""
        b = next((bb for bb in self.buildings if bb.bid == bid), None)
        return (b.label or b.kind.value) if b else ""

    def _bname_en(self, bid: int | None) -> str:
        """建筑名的英文版：`_LABEL_EN` 精确翻译专名，尾部编号摘出来再拼回去。

        label 集合是封闭的（generate_town 里写死的 14 种），所以用表最准；
        查不到就退回业态值（home / office …），至少不是中文。
        """
        if bid is None:
            return ""
        b = next((bb for bb in self.buildings if bb.bid == bid), None)
        if b is None:
            return ""
        label = b.label or b.kind.value
        i = len(label)
        while i > 0 and label[i - 1].isdigit():
            i -= 1
        base, num = label[:i], label[i:]
        en = _LABEL_EN.get(base) or _LABEL_EN.get(label)
        if en is None:
            return b.kind.value
        return "%s %s" % (en, num) if num else en

    def _log_activity(self, r: Resident) -> None:
        """按「当前动作 + 已到位」记一条日常日志。没到位（在路上）不记。"""
        text = _ACT_TEXT.get(r.action)
        if not text:
            return
        if not self._at_target(r):
            return
        text_en = _ACT_TEXT_EN.get(r.action, "")
        where = self._bname(r.target_id)
        if where:
            text = "%s（%s）" % (text, where)
        where_en = self._bname_en(r.target_id)
        if text_en and where_en:
            text_en = "%s (%s)" % (text_en, where_en)
        # 上班/上学/睡觉一天一次就够；吃饭、聊天这类动作一天可能多次
        cd = 600 if r.action in ("work", "school", "sleep") else 180
        self._log(r, r.action, text, cooldown=cd, text_en=text_en)

    # ------------------------------------------------------- 统计日报（分桶采样）
    def _blank_hist(self, day: int | None = None) -> dict:
        """一天的空直方图：9 个桶，每桶形如 {"动作": {"n": 人次, "who": {rid}}}。"""
        return {"day": day,
                "buckets": [{} for _ in range(NIGHT_BUCKET + 1)]}

    def _report_day_of(self, tick: int | None = None) -> int:
        """这一刻属于哪个「统计日」。

        统计日按 06:00 起算：06:00–23:59 属当天，00:00–05:59 属前一天。
        这样 22:00→次日 06:00 的夜桶就落在同一个统计日里，不会被 00:00
        的 day 切换切成两半（这是「夜桶整段」能成立的前提）。
        """
        t = self.clock.tick if tick is None else tick
        return self.clock.day if t >= DAY_START_TICK else self.clock.day - 1

    def _bucket_of(self, tick: int | None = None) -> int:
        """桶序号 0..8：0–7 是白天每 2 小时，8 是整夜。"""
        t = (self.clock.tick if tick is None else tick) - DAY_START_TICK
        if t < 0:
            t += self.clock.ticks_per_day
        return min(t // BUCKET_MIN, NIGHT_BUCKET)

    def _sample_report(self, r: Resident) -> None:
        """每 tick 对每个活人记一笔「此刻在做什么」。

        按 `r.action` 记；但相遇交互会把 action 改写成「打招呼」「打架」这类
        中文动作名，那不是行为而是交互 —— 不在 `_ACTION_VALUES` 里就按 `intent`
        （原目标）归类，与 `_apply()` 的兜底口径一致。
        """
        if not r.alive:
            return
        a = r.action
        if a not in _ACTION_VALUES:
            a = r.intent if r.intent in _ACTION_VALUES else None
        if a is None:
            return
        cell = self._hist["buckets"][self._bucket_of()].setdefault(
            a, {"n": 0, "who": set()})
        cell["n"] += 1
        cell["who"].add(r.rid)

    def _roll_report_if_needed(self) -> None:
        """跨过 06:00 就把上一统计日封盘。"""
        rd = self._report_day_of()
        if self._hist["day"] is None:
            self._hist["day"] = rd
            return
        if rd != self._hist["day"]:
            self._seal_report(self._hist["day"])
            self._hist = self._blank_hist(rd)

    def _seal_report(self, day: int) -> None:
        """把某统计日的直方图转成可序列化快照：留内存 + 落库。"""
        data = self._report_snapshot(day)
        self.reports[day] = data
        if len(self.reports) > 60:
            for k in sorted(self.reports)[:-60]:
                self.reports.pop(k, None)
        if self._con is not None:
            try:
                storage_repo.save_daily_stats(self._con, day, data)
                self._con.commit()
            except Exception as exc:      # 落库失败不能拖垮仿真
                print(f"[storage] daily_stats failed: {exc}")

    def _report_snapshot(self, day: int) -> dict:
        """9 个桶的日报。每桶给动作排序 + 去重人数。

        两个数别混：
        - `people`（独立人数）：同一人在这 2 小时内只算一次 —— 「吃饭 xx 人」用它
        - `ticks`（人次）：每分钟累加，吃 40 分钟就是 40 —— 看投入时长用
        """
        buckets = []
        for i, cells in enumerate(self._hist["buckets"]):
            f, t = BUCKET_WINDOWS[i]
            acts = [{"action": a, "people": len(c["who"]), "ticks": c["n"]}
                    for a, c in cells.items()]
            acts.sort(key=lambda x: (-x["people"], -x["ticks"]))
            who: set = set()
            for c in cells.values():
                who |= c["who"]
            buckets.append({
                "index": i,
                "from": f,
                "to": t,
                "night": i == NIGHT_BUCKET,
                "people": len(who),
                "actions": acts,
            })
        return {"day": day, "buckets": buckets}

    def report(self, day: int | None = None) -> dict:
        """取某天的日报。不传 day 就取「正在进行的这一天」的实时快照。"""
        if day is None:
            return self._report_snapshot(self._report_day_of())
        if day in self.reports:
            return self.reports[day]
        if self._con is not None:          # 内存里没有就回库捞
            try:
                got = storage_repo.load_daily_stats(self._con, day)
                if got is not None:
                    return got
            except Exception as exc:
                print(f"[storage] daily_stats load failed: {exc}")
        return {"day": day, "buckets": []}

    def _emit(self, a: Resident, b: Resident, res) -> None:
        self.events.append({
            "day": self.clock.day,
            "tick": self.clock.tick,
            "type": "interaction",
            "kind": res.kind.value,
            "category": res.category.value,
            "actor": a.rid,
            "target": b.rid,
            "fx": res.fx,
        })

    # ------------------------------------------------------------------ 输出
    def snapshot(self) -> dict:
        return {
            "type": "frame",
            "clock": self.clock.snapshot(),
            "paused": self.paused,
            "speed": self.cfg.time.speed,
            "residents": [r.snapshot() for r in self.residents],
            "stats": self.stats(),
            "fields": self.fields.snapshot(),
            "relations": self.relations.snapshot(),
            "events": list(self.events)[-20:],
            # 日志：带 seq 供前端去重合并；每帧只给尾部 40 条（全量走 /api/logs）
            "log_seq": self._log_seq,
            "logs": list(self.logs)[-40:],
        }

    def stats(self) -> dict:
        by_action: dict[str, int] = {}
        for r in self.residents:
            by_action[r.action] = by_action.get(r.action, 0) + 1
        mood = sum(r.mood for r in self.residents) / max(1, len(self.residents))
        return {
            "population": len(self.residents),
            "mood_avg": round(mood, 1),
            "by_action": by_action,
            "buildings": len(self.buildings),
            "relations": self.relations.stats(),
        }

    def reset(self) -> None:
        self.__init__(self.cfg)
