"""交互动作：相遇时的「发生了什么」。

设计对齐 DESIGN.md §6：20+ 交互，分三类——
  - friendly  友好（打招呼/聊天/拥抱…）
  - romantic  中性/浪漫（调情/表白/求婚/结婚）
  - conflict  冲突（争吵/打架/偷窃…，稀少但存在）

每个交互是一份「规格」(`InteractionSpec`)：门槛（需要多亲的关系）、
权重、人格偏好、关系/心情增减、社交满足量。`choose()` 按关系+人格+心情
加权抽样，`apply()` 算出效果与事件。规则是主体，LLM 不参与。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .relations import Relationship
from .resident import Ocean


class Category(str, Enum):
    FRIENDLY = "friendly"
    ROMANTIC = "romantic"
    CONFLICT = "conflict"


class InteractionKind(str, Enum):
    # --- 友好 ---
    GREET = "打招呼"
    CHAT = "聊天"
    HIGH_FIVE = "击掌"
    HUG = "拥抱"
    HOLD_HANDS = "牵手"
    GIFT = "送礼物"
    INVITE = "邀请同行"
    EAT_TOGETHER = "一起吃饭"
    HELP = "帮忙"
    COMFORT = "安慰"
    SHARE_FOOD = "分享食物"
    STORY = "讲故事"
    # --- 浪漫 ---
    FLIRT = "调情"
    DATE = "邀约"
    CONFESS = "表白"
    PROPOSE = "求婚"
    MARRY = "结婚"
    # --- 冲突 ---
    QUARREL = "争吵"
    INSULT = "辱骂"
    SHOVE = "推搡"
    FIGHT = "打架"
    STEAL = "偷窃"
    ROB = "抢劫"
    RUMOR = "造谣"


@dataclass
class InteractionSpec:
    kind: InteractionKind
    category: Category
    # 门槛：低于此值不会发生
    min_intimacy: float = 0.0
    min_trust: float = -100.0
    # 基础权重（相对）
    weight: float = 1.0
    # 关系增减（对双方对称施加）
    trust: float = 0.0
    intimacy: float = 0.0
    # 心情增减：发起者 / 接受者
    mood_self: float = 0.0
    mood_other: float = 0.0
    # 社交需求满足量（0..100 递减）
    social_gain: float = 8.0
    # 人格偏好：OCEAN 字段 -> 权重系数（正=该人格更爱做）
    bias: dict[str, float] = field(default_factory=dict)
    # 前端特效提示
    fx: str = "spark"
    requires_marriage: bool = False


# 人格字段短名 -> Ocean 属性名
_OCEAN = {
    "o": "openness", "c": "conscientiousness", "e": "extraversion",
    "a": "agreeableness", "n": "neuroticism",
}


def _bias_factor(ocean: Ocean, bias: dict[str, float]) -> float:
    """把 OCEAN 折算成一个 ≥0 的权重倍率。"""
    raw = 0.0
    for short, coef in bias.items():
        raw += getattr(ocean, _OCEAN[short]) * coef
    return max(0.0, 1.0 + raw)


SPECS: dict[InteractionKind, InteractionSpec] = {
    # ---------------- 友好 ----------------
    InteractionKind.GREET: InteractionSpec(
        InteractionKind.GREET, Category.FRIENDLY, weight=6.0,
        trust=0.5, intimacy=0.4, mood_self=0.4, mood_other=0.5, social_gain=6,
        bias={"e": 0.8}, fx="wave"),
    InteractionKind.CHAT: InteractionSpec(
        InteractionKind.CHAT, Category.FRIENDLY, weight=4.0,
        trust=0.8, intimacy=1.0, mood_self=0.8, mood_other=0.8, social_gain=12,
        bias={"e": 0.9, "o": 0.4}, fx="bubble"),
    InteractionKind.HIGH_FIVE: InteractionSpec(
        InteractionKind.HIGH_FIVE, Category.FRIENDLY, min_intimacy=10, weight=2.5,
        trust=0.6, intimacy=1.2, mood_self=1.2, mood_other=1.2, social_gain=8,
        bias={"e": 0.9}, fx="star"),
    InteractionKind.HUG: InteractionSpec(
        InteractionKind.HUG, Category.FRIENDLY, min_intimacy=30, weight=2.0,
        trust=1.2, intimacy=2.0, mood_self=1.5, mood_other=1.8, social_gain=14,
        bias={"a": 0.8}, fx="heart"),
    InteractionKind.HOLD_HANDS: InteractionSpec(
        InteractionKind.HOLD_HANDS, Category.FRIENDLY, min_intimacy=50, weight=1.5,
        trust=1.0, intimacy=2.2, mood_self=1.6, mood_other=1.6, social_gain=13,
        bias={"a": 0.6, "e": 0.4}, fx="heart"),
    InteractionKind.GIFT: InteractionSpec(
        InteractionKind.GIFT, Category.FRIENDLY, min_intimacy=15, weight=1.2,
        trust=2.0, intimacy=1.5, mood_self=0.6, mood_other=2.5, social_gain=10,
        bias={"a": 0.7, "c": 0.4}, fx="gift"),
    InteractionKind.INVITE: InteractionSpec(
        InteractionKind.INVITE, Category.FRIENDLY, min_intimacy=20, weight=1.5,
        trust=0.8, intimacy=1.4, mood_self=0.8, mood_other=0.9, social_gain=11,
        bias={"e": 0.6}, fx="arrow"),
    InteractionKind.EAT_TOGETHER: InteractionSpec(
        InteractionKind.EAT_TOGETHER, Category.FRIENDLY, min_intimacy=20, weight=1.6,
        trust=0.9, intimacy=1.6, mood_self=1.2, mood_other=1.2, social_gain=15,
        bias={"a": 0.5, "e": 0.3}, fx="food"),
    InteractionKind.HELP: InteractionSpec(
        InteractionKind.HELP, Category.FRIENDLY, min_intimacy=15, weight=1.4,
        trust=2.2, intimacy=1.3, mood_self=0.7, mood_other=1.6, social_gain=10,
        bias={"a": 1.0, "c": 0.4}, fx="check"),
    InteractionKind.COMFORT: InteractionSpec(
        InteractionKind.COMFORT, Category.FRIENDLY, min_intimacy=20, weight=1.6,
        trust=1.8, intimacy=1.6, mood_self=0.9, mood_other=3.0, social_gain=13,
        bias={"a": 1.0, "n": 0.4}, fx="hug"),
    InteractionKind.SHARE_FOOD: InteractionSpec(
        InteractionKind.SHARE_FOOD, Category.FRIENDLY, min_intimacy=25, weight=1.0,
        trust=1.4, intimacy=1.2, mood_self=0.6, mood_other=1.8, social_gain=9,
        bias={"a": 0.8}, fx="food"),
    InteractionKind.STORY: InteractionSpec(
        InteractionKind.STORY, Category.FRIENDLY, min_intimacy=25, weight=1.1,
        trust=0.8, intimacy=1.3, mood_self=0.9, mood_other=1.2, social_gain=12,
        bias={"o": 0.9, "e": 0.4}, fx="bubble"),
    # ---------------- 浪漫 ----------------
    InteractionKind.FLIRT: InteractionSpec(
        InteractionKind.FLIRT, Category.ROMANTIC, min_intimacy=35, min_trust=0,
        weight=1.2, trust=0.6, intimacy=2.0, mood_self=1.4, mood_other=1.0,
        social_gain=10, bias={"e": 0.7, "o": 0.5}, fx="heart"),
    InteractionKind.DATE: InteractionSpec(
        InteractionKind.DATE, Category.ROMANTIC, min_intimacy=45, min_trust=10,
        weight=1.0, trust=1.2, intimacy=2.6, mood_self=1.6, mood_other=1.6,
        social_gain=15, bias={"e": 0.5, "o": 0.4}, fx="heart"),
    InteractionKind.CONFESS: InteractionSpec(
        InteractionKind.CONFESS, Category.ROMANTIC, min_intimacy=60, min_trust=25,
        weight=0.8, trust=2.0, intimacy=4.0, mood_self=2.0, mood_other=2.2,
        social_gain=16, bias={"o": 0.5}, fx="heart"),
    InteractionKind.PROPOSE: InteractionSpec(
        InteractionKind.PROPOSE, Category.ROMANTIC, min_intimacy=75, min_trust=45,
        weight=0.5, trust=3.0, intimacy=3.0, mood_self=3.0, mood_other=3.5,
        social_gain=18, bias={"c": 0.4}, fx="ring"),
    InteractionKind.MARRY: InteractionSpec(
        InteractionKind.MARRY, Category.ROMANTIC, min_intimacy=80, min_trust=50,
        weight=0.4, trust=5.0, intimacy=5.0, mood_self=6.0, mood_other=6.0,
        social_gain=20, fx="ring"),
    # ---------------- 冲突 ----------------
    InteractionKind.QUARREL: InteractionSpec(
        InteractionKind.QUARREL, Category.CONFLICT, weight=1.0,
        trust=-2.5, intimacy=-1.0, mood_self=-1.5, mood_other=-2.5, social_gain=3,
        bias={"n": 0.9}, fx="anger"),
    InteractionKind.INSULT: InteractionSpec(
        InteractionKind.INSULT, Category.CONFLICT, weight=0.7,
        trust=-4.0, intimacy=-1.5, mood_self=-1.0, mood_other=-4.0, social_gain=2,
        bias={"n": 0.8, "a": -0.6}, fx="anger"),
    InteractionKind.SHOVE: InteractionSpec(
        InteractionKind.SHOVE, Category.CONFLICT, weight=0.5,
        trust=-5.0, intimacy=-2.0, mood_self=-1.5, mood_other=-4.5, social_gain=2,
        bias={"n": 0.8, "a": -0.7}, fx="anger"),
    InteractionKind.FIGHT: InteractionSpec(
        InteractionKind.FIGHT, Category.CONFLICT, weight=0.35,
        trust=-8.0, intimacy=-3.0, mood_self=-3.0, mood_other=-7.0, social_gain=1,
        bias={"n": 0.9, "a": -0.9}, fx="anger"),
    InteractionKind.STEAL: InteractionSpec(
        InteractionKind.STEAL, Category.CONFLICT, weight=0.25,
        trust=-9.0, intimacy=-3.0, mood_self=0.5, mood_other=-6.0, social_gain=1,
        bias={"a": -1.1, "c": -0.6}, fx="anger"),
    InteractionKind.ROB: InteractionSpec(
        InteractionKind.ROB, Category.CONFLICT, weight=0.15,
        trust=-12.0, intimacy=-4.0, mood_self=0.5, mood_other=-8.0, social_gain=1,
        bias={"a": -1.3, "c": -0.8}, fx="anger"),
    InteractionKind.RUMOR: InteractionSpec(
        InteractionKind.RUMOR, Category.CONFLICT, weight=0.3,
        trust=-5.0, intimacy=-2.0, mood_self=-0.5, mood_other=-4.0, social_gain=2,
        bias={"a": -0.8, "n": 0.5}, fx="anger"),
}


def choose(
    rng: random.Random, a: Ocean, b: Ocean, rel: Relationship,
    mood_a: float, mood_b: float,
) -> InteractionKind:
    """在门槛通过的交互里，按「关系 × 人格 × 心情」加权抽样。"""
    avg_mood = (mood_a + mood_b) / 2.0
    cands: list[tuple[InteractionKind, float]] = []
    for kind, s in SPECS.items():
        if s.requires_marriage and not rel.married:
            continue
        if rel.intimacy < s.min_intimacy or rel.trust < s.min_trust:
            continue
        w = s.weight
        w *= _bias_factor(a, s.bias) * _bias_factor(b, s.bias)
        if s.category is Category.CONFLICT:
            # 心情越差越易冲突（0.4x ~ 1.8x）
            w *= 0.4 + (100.0 - avg_mood) / 100.0 * 1.4
        else:
            # 心情越好越易友好（0.5x ~ 1.5x）
            w *= 0.5 + avg_mood / 100.0
        if w <= 0:
            continue
        cands.append((kind, w))
    if not cands:
        return InteractionKind.GREET
    total = sum(w for _, w in cands)
    pick = rng.random() * total
    acc = 0.0
    for kind, w in cands:
        acc += w
        if pick <= acc:
            return kind
    return cands[-1][0]


@dataclass
class InteractionResult:
    """一次交互结算后的效果包，供 sim 落账。"""
    kind: InteractionKind
    category: Category
    trust_delta: float
    intimacy_delta: float
    mood_self: float
    mood_other: float
    social_gain: float
    married: bool
    fx: str


def apply(kind: InteractionKind, rel: Relationship) -> InteractionResult:
    """按规格算出对关系与心情的增减。关系数值的真实写入交给 RelationGraph。"""
    s = SPECS[kind]
    # 关系越深，同样的友好行为边际递减（防止刷满）
    damp = 1.0 if s.category is not Category.FRIENDLY else max(0.35, 1.0 - rel.intimacy / 130.0)
    return InteractionResult(
        kind=kind,
        category=s.category,
        trust_delta=s.trust * (damp if s.trust > 0 else 1.0),
        intimacy_delta=s.intimacy * damp,
        mood_self=s.mood_self,
        mood_other=s.mood_other,
        social_gain=s.social_gain,
        married=(kind is InteractionKind.MARRY),
        fx=s.fx,
    )
