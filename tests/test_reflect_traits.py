"""反思层 2：性格微调（traits）回归测试。

与层 1 的区别：这一层动的是 `r.ocean` 六维**本身**，所以只测四件事——
**默认关就一点不动**、**白名单外丢弃**、**幅度与量纲都夹住**、**会朝出生基线回弹**。
外加两条接口约定：坏 JSON 不能连累 biases，一次 chat 要同时供出 biases 与 traits。
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from eviltown.agents.narrative import TRAIT_KEYS, _apply_traits, _reflect_all, normalize_traits
from eviltown.agents.resident import Ocean
from test_reflect import FakeClient, make_r, make_sim

WANDER = "wander"


def traits_cfg(**kw) -> SimpleNamespace:
    """与 config.ReflectCfg 对齐的最小配置（personality 默认开，方便测）。"""
    base = dict(enabled=True, max_items=2, bonus_cap=6.0,
                personality=True, max_delta=0.03, regress=0.05)
    base.update(kw)
    return SimpleNamespace(**base)


def make_rr(**kw):
    """在层 1 的居民桩上补 OCEAN 与基线：_apply_traits 认的就是这两个属性。"""
    r = make_r(**kw)
    r.ocean = Ocean()
    r.ocean_base = None
    return r


def snap(r) -> dict:
    return {k: getattr(r.ocean, k) for k in TRAIT_KEYS}


def test_disabled_leaves_ocean_untouched():
    """personality=False ⇒ 性格一位都不动（哪怕模型给了 traits）。"""
    sim, r = make_sim(), make_rr()
    before = snap(r)
    client = FakeClient('{"biases":[],"traits":{"conscientiousness":0.03}}')

    _reflect_all(sim, [(r, [], "日记")], day=2, client=client, lang="zh",
                 cfg=traits_cfg(personality=False))

    assert snap(r) == before, "关闭时不许改性格"
    assert r.ocean_base is None, "关闭时连基线都不该快照"


def test_whitelist_drops_unknown_keys():
    got = normalize_traits({"traits": {"magic": 0.03, "openness": 0.02}}, 0.03)
    assert set(got) == {"openness"}


def test_delta_is_clamped_to_max_delta():
    got = normalize_traits({"traits": {"openness": 0.9, "greed": -0.9}}, 0.03)
    assert got["openness"]["delta"] == pytest.approx(0.03)
    assert got["greed"]["delta"] == pytest.approx(-0.03)


def test_value_stays_inside_unit_range():
    r = make_rr()
    r.ocean.openness = 0.99
    r.ocean_base = copy.deepcopy(r.ocean)

    _apply_traits(None, 3, r, {"openness": {"delta": 0.03}}, 0.03, 0.05)

    assert 0.0 <= r.ocean.openness <= 1.0


def test_regresses_toward_birth_baseline():
    """无信号连续多天后，偏离的维度要回到基线附近（性格是慢变量）。"""
    r = make_rr()
    r.ocean_base = copy.deepcopy(r.ocean)
    base = r.ocean.conscientiousness
    r.ocean.conscientiousness = base + 0.4          # 人为推歪

    prev = r.ocean.conscientiousness
    for day in range(80):                            # 0.4 × 0.95^80 ≈ 0.0066
        _apply_traits(None, day, r, {}, 0.03, 0.05)
        assert r.ocean.conscientiousness < prev      # 每天都朝基线挪一点
        prev = r.ocean.conscientiousness

    assert abs(r.ocean.conscientiousness - base) < 0.01, "80 天应基本回弹到基线"


def test_bad_json_affects_neither_biases_nor_ocean():
    sim, r = make_sim(), make_rr()
    before = snap(r)
    client = FakeClient("模型今天乱说话，没有 JSON")

    _reflect_all(sim, [(r, [], "日记")], day=2, client=client, lang="zh",
                 cfg=traits_cfg())

    assert r.journal_bias == []
    assert snap(r) == before


def test_single_call_supplies_both_biases_and_traits():
    """同一次 chat 的产物：偏好生效 + 性格微调生效，且只调用一次。"""
    sim, r = make_sim(), make_rr()
    client = FakeClient(
        '{"biases":[{"kind":"%s","bonus":2,"reason":"想出去走走"}],'
        '"traits":{"extraversion":0.02,"reason":"今天和人聊得开心"}}' % WANDER
    )

    _reflect_all(sim, [(r, [], "日记")], day=2, client=client, lang="zh",
                 cfg=traits_cfg())

    assert client.calls == 1, "层 2 不许新增 LLM 调用"
    assert [b["kind"] for b in r.journal_bias] == [WANDER]
    assert r.ocean.extraversion == pytest.approx(0.52, abs=1e-9)
