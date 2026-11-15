"""日记复盘（reflection）→ 软偏好 回归测试：不依赖 LLM、不写真实库。

覆盖五条硬约定：
- **默认关闭即零行为**：cfg=None / enabled=False 时不发请求、不动居民状态；
- **严格校验**：非 JSON、未知 kind、非法 bonus ⇒ 整批丢弃（半对半错更难排查）；
- bonus 被 clamp 到 [-cap, cap]，条数受 max_items 限制，0 分等于没说；
- 失败**不覆盖**已有偏好；成功只保留最近 2 天；
- **规则优先**：快饿晕时，复盘偏好再大也顶不翻 EAT；脏数据不能让 decide 崩。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from eviltown.agents.brain import ActionType, decide
from eviltown.agents.narrative import _reflect_all, normalize_biases
from eviltown.agents.sim import Simulation

WANDER = ActionType.WANDER.value


def reflect_cfg(**kw) -> SimpleNamespace:
    """与 config.ReflectCfg 字段对齐的最小配置。"""
    base = dict(enabled=True, max_items=2, bonus_cap=6.0)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    """假 LLM：记录调用次数，固定回同一段文本。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat(self, sys_p, usr_p, lang="zh", **kw):
        self.calls += 1
        return self.reply


def make_sim() -> Simulation:
    """最小可跑仿真：storage=None 让复盘落库那步直接返回。"""
    cfg = SimpleNamespace(
        population=4,
        world=SimpleNamespace(width=120, height=80),
        time=SimpleNamespace(ticks_per_day=1440, start_day=1,
                             start_tick=360, speed=1),
        storage=None,
    )
    return Simulation(cfg)


def make_r(**kw) -> SimpleNamespace:
    base = dict(name="阿明", name_en="Ming", rid=1, journal_bias=[])
    base.update(kw)
    return SimpleNamespace(**base)


# ------------------------------------------------------------ 关闭 = 零行为
def test_disabled_is_completely_silent():
    """cfg=None 与 enabled=False 都必须「不调 LLM、不改状态」。"""
    sim = make_sim()
    r = make_r()
    client = FakeClient('{"biases":[{"kind":"%s","bonus":3,"reason":"x"}]}' % WANDER)

    _reflect_all(sim, [(r, ["去公园"], "今天")], day=3, client=client,
                 lang="zh", cfg=None)
    _reflect_all(sim, [(r, ["去公园"], "今天")], day=3, client=client,
                 lang="zh", cfg=reflect_cfg(enabled=False))

    assert client.calls == 0, "关闭时不允许发任何 LLM 请求"
    assert r.journal_bias == [], "关闭时不允许改动居民状态"


# ------------------------------------------------------------ 严格校验
def test_non_json_reply_dropped():
    assert normalize_biases("我觉得今天挺好的", 6.0, 2, 3) == []


def test_unknown_kind_drops_whole_batch():
    txt = '{"biases":[{"kind":"fly_to_moon","bonus":-3,"reason":"想飞"}]}'
    assert normalize_biases(txt, 6.0, 2, 3) == [], "未知 kind 必须整批丢弃"


def test_bad_bonus_drops_whole_batch():
    txt = '{"biases":[{"kind":"%s","bonus":"很多","reason":"x"}]}' % WANDER
    assert normalize_biases(txt, 6.0, 2, 3) == []


def test_bonus_is_clamped_to_cap():
    txt = '{"biases":[{"kind":"%s","bonus":99,"reason":"太爱逛"}]}' % WANDER
    got = normalize_biases(txt, 2.5, 2, 3)
    assert len(got) == 1
    assert got[0]["bonus"] == pytest.approx(2.5), "必须 clamp 到 +cap"
    assert got[0]["day"] == 3


def test_max_items_respected():
    txt = ('{"biases":[{"kind":"%s","bonus":1,"reason":"a"},'
           '{"kind":"%s","bonus":1,"reason":"b"},'
           '{"kind":"%s","bonus":1,"reason":"c"}]}' % (WANDER, WANDER, WANDER))
    assert len(normalize_biases(txt, 6.0, 2, 3)) == 2


def test_zero_bonus_is_ignored():
    txt = '{"biases":[{"kind":"%s","bonus":0,"reason":"无感"}]}' % WANDER
    assert normalize_biases(txt, 6.0, 2, 3) == []


# ------------------------------------------------------------ 落盘语义
def test_reflect_replaces_bias_and_keeps_recent_two_days():
    sim = make_sim()
    old = {"kind": WANDER, "bonus": 1.0, "day": 1, "note": "旧"}
    r = make_r(journal_bias=[old])
    client = FakeClient('{"biases":[{"kind":"%s","bonus":-2,"reason":"逛腻了"}]}' % WANDER)

    _reflect_all(sim, [(r, [], "日记")], day=5, client=client, lang="zh",
                 cfg=reflect_cfg())

    assert client.calls == 1
    assert len(r.journal_bias) == 1, "day=1 的旧偏好应被丢掉（只留最近 2 天）"
    assert r.journal_bias[0]["day"] == 5
    assert r.journal_bias[0]["bonus"] == -2.0


def test_bad_reply_keeps_existing_bias():
    """整批丢弃时不能把已有偏好清空。"""
    sim = make_sim()
    old = {"kind": WANDER, "bonus": 1.0, "day": 3, "note": "旧"}
    r = make_r(journal_bias=[old])
    client = FakeClient("随便说两句")

    _reflect_all(sim, [(r, [], "")], day=4, client=client, lang="zh",
                 cfg=reflect_cfg())

    assert r.journal_bias == [old], "解析失败必须保留原偏好不变"


# ------------------------------------------------------------ 决策层
def test_rules_beat_reflection_hunger_wins():
    """规则优先：快饿晕时，复盘偏好再大也顶不翻紧急裁决。"""
    sim = make_sim()
    r = sim.residents[0]
    r.hunger = 95.0
    r.journal_bias = [{"kind": ActionType.SHOP.value, "bonus": 999.0,
                       "day": 1, "note": ""}]

    act = decide(r, sim.buildings, 12.0, False, sim.rng)
    assert act.kind == ActionType.EAT, "复盘不能顶翻紧急裁决"


def test_garbage_bias_never_crashes_decide():
    """脏数据（非 dict、非数字 bonus、缺 kind）只能被跳过，不能让决策抛异常。"""
    sim = make_sim()
    r = sim.residents[0]
    r.journal_bias = ["abc", 42, {"kind": None}, {"kind": WANDER, "bonus": "x"}]

    act = decide(r, sim.buildings, 12.0, False, sim.rng)
    assert act is not None


def test_bonus_cap_follows_config():
    """配置里的 bonus_cap 必须真的传达到决策侧（不能写死 6.0）。"""
    sim = make_sim()
    r = make_r()
    client = FakeClient('{"biases":[{"kind":"%s","bonus":99,"reason":"x"}]}' % WANDER)

    _reflect_all(sim, [(r, [], "日记")], day=2, client=client, lang="zh",
                 cfg=reflect_cfg(bonus_cap=2.5))

    assert r.journal_cap == pytest.approx(2.5), "封顶要跟着配置走"
    assert r.journal_bias[0]["bonus"] == pytest.approx(2.5)
