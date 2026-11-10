"""居民个体。

人格用 OCEAN 五因子（0..1）；记忆档案本体在 M5 接入 LLM 后落库。
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .wish import Wish


class Sex(str, Enum):
    M = "M"
    F = "F"


class Job(str, Enum):
    UNEMPLOYED = "unemployed"
    FARMER = "farmer"
    WORKER = "worker"
    SHOPKEEPER = "shopkeeper"
    TEACHER = "teacher"
    STUDENT = "student"
    CHILD = "child"
    DOCTOR = "doctor"


# 职业 -> 主要活动建筑类型
JOB_BUILDING = {
    Job.FARMER: "office",
    Job.WORKER: "office",
    Job.SHOPKEEPER: "shop",
    Job.TEACHER: "school",
    Job.DOCTOR: "hospital",
    Job.STUDENT: "school",
    Job.CHILD: "school",
}

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
GIVEN_M = ["强", "伟", "军", "磊", "勇", "涛", "明", "超", "刚", "平", "辉", "斌"]
GIVEN_F = ["芳", "静", "敏", "燕", "丽", "娟", "霞", "艳", "玲", "萍", "婷", "雪"]

# 英文名池（2026-11-11）：**只取名、不带姓**（用户口径），与中文名各自独立随机，
# 不做「同姓译同姓」的配对。中英两个名绑在同一个人身上（Resident.name_en）。
# 按性别分开，只为保住「Amy 是女、Ben 是男」的观感，别无他用。
NAME_EN_M = ['Ben', 'David', 'Jack', 'Mike', 'Sam', 'Tom', 'Eric', 'Leo', 'Ryan', 'Adam', 'Chris', 'Daniel', 'Kevin', 'Jason', 'Brian', 'Alex', 'Peter', 'Henry', 'Oscar', 'Felix', 'Hugo', 'Ivan', 'Mark', 'Paul']
NAME_EN_F = ['Amy', 'Emma', 'Lily', 'Grace', 'Chloe', 'Nina', 'Sophie', 'Lucy', 'Anna', 'Mia', 'Ruby', 'Ivy', 'Jane', 'Kate', 'Rose', 'Helen', 'Alice', 'Clara', 'Diana', 'Emily', 'Fiona', 'Julia', 'Laura', 'Nora']



@dataclass
class Ocean:
    openness: float = 0.5
    conscientiousness: float = 0.5
    extraversion: float = 0.5
    agreeableness: float = 0.5
    neuroticism: float = 0.5
    # 第六维：财迷（0 = 钱乃身外物，1 = 一分钱掰两半）。
    # 不在标准 OCEAN 五维里，但它给「钱」这条线一个性格入口：
    # 同样花 60 块，财迷的人心疼得多；存款见底时他比谁都想上班。
    greed: float = 0.4

    @classmethod
    def random(cls, rng: random.Random) -> "Ocean":
        return cls(**{k: rng.random() for k in cls.__dataclass_fields__})


@dataclass
class Resident:
    rid: int
    name: str
    sex: Sex
    age: int
    job: Job
    ocean: Ocean
    home_id: int
    work_id: int | None

    # --- 运行时状态 ---
    # 个人固定入户格（= 他自己的床位）：由 sim._spawn 一次性分配，终身不变。
    # 同一栋住宅的住户各自错开一格，避免同户数人叠在同一格上。
    home_tile: tuple[int, int] | None = None
    x: int = 0
    y: int = 0
    # 格内小数偏移（0..1）：站在这格的哪个位置。
    # 由 points.json 的点位偏移决定 —— 编辑器里设在哪，人就站哪，而不是永远格心。
    off_x: float = 0.5
    off_y: float = 0.5
    prev_x: int = 0
    prev_y: int = 0
    path: list[tuple[int, int]] = field(default_factory=list)
    action: str = "idle"
    # 「我要去做什么」——与 action 分开：交互会把 action 改写成「打招呼」，
    # 但人仍在朝原目标走，结算必须认 intent（否则到店也办不成事）。
    intent: str = ""
    target_id: int | None = None
    dwell: int = 0              # 在当前目标建筑停留的 tick 数（DWELL_CAP 后换活动）
    # 持久睡眠状态：交互会把 action 改写成「聊天/打架」等，不能再拿 action 字符串当睡眠判据
    sleeping: bool = False
    # 冻结看门狗：连续「无路径且不在目标」的 tick 数，超阈值强制重新决策
    stuck: int = 0
    # 上次发生社会交互的 tick：用于「闲聊冷却」，避免同一对邻居原地反复打招呼。
    last_talk_tick: int = -999
    # 到访过的建筑次数统计（bid -> 次数）：供目的地选择偏好「没去过的地方」，
    # 避免每个居民一辈子只锁死最近的那家餐馆/厕所/商店（人均只用到 5~6 栋的主因）。
    visited: dict[int, int] = field(default_factory=dict)
    # 当天已挣的工资（逐 tick 累加，跨日时由 _end_of_day 汇总成账本里的一笔）
    wage_today: float = 0.0

    # 本次如厕占用的马桶格（建筑内的白点）；None = 未占用（正在排队或未在如厕）。
    # 由 sim._toilet_spot 预约分配、sim._release_toilet 释放。
    toilet_tile: tuple[int, int] | None = None

    # --- 就业 / 经济（2026-11-10 新增）---
    # 岗位不再出生分配，而是「自己找」：work_id 随时可因求职/辞职/岗位消失而变，
    # job_tile 是当前雇主楼里**具体哪个工位**（来自 points.json 的 desk 点位，
    # 每格 = 一个岗位；编辑器加/删点位后立刻生效）。
    job_tile: tuple[int, int] | None = None
    # 钱包（元）：初始一万，吃饭/泡吧/买渔具/看病从这里扣，上班按小时进账。
    money: float = 10000.0
    # 财务压力（0..100）：**独立于心情/压力**的一条线。心情只由上班累积、睡觉放松
    # 消解（见 sim._decay），钱的焦虑一概不掺进去 —— 否则「花钱→心情差(=压力大)
    # →压力闸门放行→去酒吧→花更多」会变成自我强化的正反馈（用户 2026-11-10 指出）。
    # 两个来源：① 存款低于「焦虑线」的底噪（穷就一直慌，钱不回来就不消失）；
    #            ② 每笔消费的临时肉痛（按分钟消退，一小时淡掉 0.6 分）。
    # 唯一出口：抬上班效用（见 brain 的 work_u），不写 mood。
    fin_stress: float = 0.0
    # 财富轨迹：每 2 小时采样一次 (tick, 余额)，最多留 85 个点 = 84×120 tick = 7 天。
    # 只用来算「这一周净流失了多少」—— 财务焦虑看的是趋势，不是某个绝对水位
    # （稳态经济下余额永远在一万上下，绝对水位那条线一辈子触发不了）。
    money_trail: deque = field(default_factory=lambda: deque(maxlen=85))
    # 学识（0..1）：只在教室里长。学历高的人时薪谈得更高（sim 的在岗计薪）。
    education: float = 0.0
    # 行业经验（业态取值 -> 累计小时）：在哪个行当干过多久。
    # 本行业经验越厚，再回到同业态时薪越高 —— 「越做越专」而不是每次从头来。
    experience: dict[str, float] = field(default_factory=dict)
    # 随身物品（{"rod": 1} = 有渔具）。钓鱼前必须先有渔具，否则得先去商店买。
    items: dict[str, int] = field(default_factory=dict)
    # 连续待业累计（单位为「天」的零头）：没钱的人会越待越想去找活干，
    # 否则全镇可能集体躺平 —— 那看着像实现坏了，而不是涌现。
    jobless_days: float = 0.0
    # 最近若干笔收支（tick, 项目, 金额），前端详情面板显示用，只留尾部。
    ledger: list[tuple[int, float, str, str]] = field(default_factory=list)  # (day, 金额, 中文项, 英文项)
    # --- 长期愿望（M5 慢脑产物，见 agents/wish.py）---
    # 出生时先给模板（source=template），LLM 可用则后台线程逐批换成模型写的（source=llm）。
    # 只有 kind 参与规则层打分，text 纯叙事（红线：LLM 不进主循环）。
    wish: "Wish | None" = None

    # 英文名（只取名、不带姓）：切英文时全站（地图标签 / 日志 / 详情 / 下拉 / 事件流）
    # 都用它；空串时退回拼音。中文名 name 仍是唯一真相，存档与落库都只用中文名。
    # ⚠️ 必须放在字段区**末尾**：name 后面紧跟无默认值的 sex / age，
    #    dataclass 不允许「有默认值字段」排在「无默认值字段」之前。
    name_en: str = ""


    # 需求 0..100（越高越急；M3 完善衰减/满足曲线）
    # ⚠️ hunger 是**内部**字段名，语义为「饥饿度」：0=饱、100=极饿。
    #    对外（UI / API / 落库）一律以「饱食度 satiety = 100 - hunger」呈现，
    #    即「高=饱、低=饿」（用户 2026-09-30 要求）。改名会牵动 DECAY、阈值
    #    (_NEED_GATE/_CRITICAL_NEED)、测试断言与 DB 列/历史库，故只在出口层换算一次，
    #    内部驱动逻辑（越饿分越高）保持不变。
    hunger: float = 20.0
    bladder: float = 20.0
    energy: float = 90.0
    social: float = 40.0

    mood: float = 70.0          # 心情 0..100
    alive: bool = True

    # --- 健康 / 生病（2026-11-10 新增，见 NEEDS_MODEL.md）---
    # 健康度由 4 项需求合成（饿、过劳、压力、憋尿），每分钟朝目标值缓慢趋近，
    # 不是被某个事件直接扣 —— 具体公式见 NEEDS_MODEL.md §6。
    health: float = 100.0
    # 病中剩余天数：>0 = 正在生病，每天 09:00–12:00 去医院治疗。
    sick_days: int = 0
    stress_days: int = 0        # 连续「高压日」天数（一天高压 ≥ 4 小时算一天）
    high_stress_min: int = 0    # 当天处于高压（stress ≥ 70）的分钟数，跨日清零
    # 连续高压累计（分钟）：stress ≥ 85 时累加，低于就清零；满 720 分钟诱发疾病。
    stress_minutes: int = 0

    # --- 派生 ---
    @property
    def is_working(self) -> bool:
        return self.work_id is not None

    @property
    def satiety(self) -> float:
        """饱食度 0..100：100=饱（不饿）、0=饿。= 100 − 内部饥饿度。"""
        return 100.0 - self.hunger

    @property
    def fatigue(self) -> float:
        """劳累度 0..100：100 − 体力。高=累。

        方向与 satiety 一致（高=急），供决策层统一按「越大越急」比较。
        底层仍写 energy，前端与存档不用动。
        """
        return 100.0 - self.energy

    @property
    def stress(self) -> float:
        """压力 0..100：100 − 心情。高=压抑。

        用户口径：「压力值过大，也就是心情值过小」—— 同一根轴的两面，
        底层仍写 mood。压力**不由时间自然产生**，只由上班/上学累积，
        由睡觉与各种放松行为消解（见 NEEDS_MODEL.md §2/§5）。
        """
        return 100.0 - self.mood

    def need_urgency(self) -> float:
        """最紧迫需求（0..100），供快脑打分。"""
        return max(self.hunger, self.bladder, 100 - self.energy, self.social * 0.7)

    def snapshot(self) -> dict:
        return {
            "id": self.rid, "name": self.name, "name_en": self.name_en,
            "sex": self.sex.value, "age": self.age,
            # 坐标 = 格子 + 格内偏移：前端按这个画，点位设在哪就落在哪（不再压回格心）。
            "job": self.job.value,
            "x": round(self.x + self.off_x, 2), "y": round(self.y + self.off_y, 2),
            "action": self.action,
            # 路径非空 = 仍在前往目标的路上。action 在决策瞬间就被设成「目标动作」，
            # 若不加此字段，人在半路就显示「吃饭」；UI 用 moving 区分「在途」与「已到达做事」。
            "moving": bool(self.path),
            "mood": round(self.mood, 1),
            # 健康 / 生病（NEEDS_MODEL.md §6–§7）：前端详情面板用
            "health": round(self.health, 1),
            "sick_days": self.sick_days,
            # 对外统一用「饱食度 satiety」（高=饱、低=饿）；hunger 为内部驱动字段，
            # 仍随快照带出以兼容旧前端旧库，新代码请一律读 satiety。
            "need": {"satiety": round(self.satiety, 1), "bladder": round(self.bladder, 1),
                     "energy": round(self.energy, 1), "social": round(self.social, 1),
                     "hunger": round(self.hunger, 1),
                     "fatigue": round(self.fatigue, 1),
                     "stress": round(self.stress, 1)},
            "ocean": {k: round(v, 2) for k, v in vars(self.ocean).items()},
            # —— 货币系统（M7）：让界面上能看见谁有钱、谁欠债、钱花在哪 ——
            "money": round(self.money, 1),
            "debt": self.money < 0,
            "greed": round(self.ocean.greed, 2),
            "fin_stress": round(self.fin_stress, 1),
            "education": round(self.education, 3),
            "exp_total": round(sum(self.experience.values()), 1),
            "work_id": self.work_id,
            "job_tile": list(self.job_tile) if self.job_tile else None,
            # 最近 8 笔收支（倒序）：[仿真日, 金额, 项目名]
            "ledger": [[d, round(a, 1), w, we] for d, a, w, we in self.ledger[-8:][::-1]],
            # 长期愿望：kind_cn 给中文标签，ratio 给进度条，source 诚实标注 llm/template
            "wish": self.wish.to_dict() if self.wish is not None else None,
        }


def make_name(rng: random.Random, sex: Sex) -> str:
    given = rng.choice(GIVEN_M if sex == Sex.M else GIVEN_F)
    return rng.choice(SURNAMES) + given


def make_name_en(rng: random.Random, sex: Sex) -> str:
    """英文名：**只用名、不带姓**（用户 2026-11-11 口径）。

    与 make_name 完全独立抽签 —— 不按姓氏对应，也不要求 50 人互不重名。
    """
    return rng.choice(NAME_EN_M if sex == Sex.M else NAME_EN_F)
