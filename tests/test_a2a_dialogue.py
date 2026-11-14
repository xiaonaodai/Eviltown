"""A2A 双回合对白的回归测试（不联网、不依赖任何真实模型）。

覆盖四条约定：
- **解析**：JSON / 单行分隔 / 两行 —— 三级兜底；胡说八道一律 None；
- **降级**：模型返回 None、空串、跑题 → `_generate` 返回 None，绝不抛；
- **节流**：队列满丢请求（不阻塞调用方）、每日上限硬停；
- **关闭时零行为变化**：`llm.a2a=false` → `Simulation.a2a is None`，tick 与旧版一致。

所有模型调用都用 monkeypatch 换掉 `LLMClient.chat`，因此离线可跑、不耗 token。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from eviltown.agents import dialogue as dlg
from eviltown.agents import interactions as ix
from eviltown.agents.dialogue import DialogueRequest, DialogueSynth, parse_pair
from eviltown.agents.sim import Simulation, _A2A_KINDS
from eviltown.config import LLMCfg


def _req(n: int = 0) -> DialogueRequest:
    return DialogueRequest("张三", "李四", "两人都是农夫，在田边碰上", meta={"n": n})


def _synth(**kw) -> DialogueSynth:
    """开着 a2a 的合成器；默认去掉节流等待，让用例跑得快。"""
    kw.setdefault("min_interval", 0.0)
    kw.setdefault("max_per_day", 0)
    return DialogueSynth(LLMCfg(a2a=True), **kw)


def _sim_cfg(a2a: bool = False) -> SimpleNamespace:
    """带 llm 段的最小仿真配置（字段与 config.py 对齐，storage=None 不落库）。"""
    return SimpleNamespace(
        population=8,
        world=SimpleNamespace(width=80, height=60),
        time=SimpleNamespace(ticks_per_day=1440, start_day=1,
                             start_tick=360, speed=1),
        storage=None,
        llm=LLMCfg(a2a=a2a),
    )


# ------------------------------------------------------------------ 解析
def test_parse_json():
    assert parse_pair('{"a": "今天好热", "b": "是啊，汗都出来了"}') == \
        ("今天好热", "是啊，汗都出来了")


def test_parse_json_with_chatter_around_it():
    raw = '好的，这是对话：{"a": "下班了", "b": "一起走"} 希望你喜欢'
    assert parse_pair(raw) == ("下班了", "一起走")


def test_parse_two_prefixed_lines():
    assert parse_pair("A：你好\nB：吃了吗") == ("你好", "吃了吗")


def test_parse_single_line_with_b_prefix():
    assert parse_pair("A：你好 B：吃了吗") == ("你好", "吃了吗")


@pytest.mark.parametrize("raw", [None, "", "   ", "随便说点什么",
                                 '{"a": "只有 A 一句"}'])
def test_parse_garbage_is_none(raw):
    assert parse_pair(raw) is None


# ------------------------------------------------------------------ 开关与队列
def test_disabled_by_default():
    s = DialogueSynth(LLMCfg())          # a2a 默认 False
    assert s.enabled is False
    assert s.submit(_req()) is False     # 不开 → 不排队，调用方继续走模板
    assert s.stats()["queued"] == 0


def test_queue_full_drops_without_blocking(monkeypatch):
    monkeypatch.setattr(DialogueSynth, "start", lambda self: None)   # 不起线程
    s = DialogueSynth(LLMCfg(a2a=True), max_queue=1)
    assert s.submit(_req(1)) is True
    assert s.submit(_req(2)) is False    # 满了直接丢，不排队等
    assert s.stats()["dropped"] == 1


# ------------------------------------------------------------------ 生成与降级
def test_generate_none_when_model_returns_none(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat", lambda *a, **k: None)
    s = _synth()
    assert s._generate(_req()) is None
    assert s.stats()["generated"] == 0


def test_generate_none_when_unparsable(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat", lambda *a, **k: "我觉得他们应该聊聊")
    assert _synth()._generate(_req()) is None


def test_generate_ok_and_meta_passthrough(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat",
                        lambda *a, **k: '{"a": "收成不错", "b": "还行吧"}')
    s = _synth()
    line = s._generate(_req(7))
    assert line is not None
    assert (line.line_a, line.line_b) == ("收成不错", "还行吧")
    assert line.render() == "张三：收成不错 李四：还行吧"
    assert line.meta["n"] == 7
    assert s.stats()["generated"] == 1


def test_daily_cap_stops_generation(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat",
                        lambda *a, **k: '{"a": "嗯", "b": "哦"}')
    s = _synth(max_per_day=1)
    assert s._generate(_req()) is not None
    assert s._generate(_req()) is None   # 到日上限，硬停
    assert s.stats()["generated"] == 1


# ------------------------------------------------------------------ 线程
def test_thread_produces_line(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat",
                        lambda *a, **k: '{"a": "早", "b": "早啊"}')
    s = _synth()
    s.start()
    try:
        assert s.submit(_req()) is True
        deadline = time.time() + 3
        out = []
        while time.time() < deadline and not out:
            out = s.poll()
            time.sleep(0.02)
        assert out, "后台线程 3 秒内没有产出"
        assert out[0].line_a == "早"
        assert s.poll() == []            # 取走即清空，不重复消费
    finally:
        s.stop()


def test_stop_is_prompt(monkeypatch):
    monkeypatch.setattr(dlg.LLMClient, "chat", lambda *a, **k: None)
    s = _synth()
    s.start()
    t = s._thread
    assert t is not None and t.daemon is True
    s.stop()
    t.join(2)
    assert not t.is_alive()


# ------------------------------------------------------------------ 与仿真的挂接
def test_simulation_a2a_off_by_default():
    sim = Simulation(_sim_cfg())
    assert sim.a2a is None               # 不开就不建，旧行为完全不变
    sim.tick(60)
    assert all(r.alive for r in sim.residents)


def test_drain_writes_resident_log():
    sim = Simulation(_sim_cfg(a2a=True))
    assert sim.a2a is not None
    rid = sim.residents[0].rid
    sim.a2a._done.append(
        dlg.DialogueLine("谢逊", "张翠山", "风紧", "扯呼", {"a": rid, "b": rid}))
    n0 = len(sim.logs)
    sim._drain_a2a()
    assert len(sim.logs) == n0 + 1
    entry = sim.logs[-1]
    assert entry["rid"] == rid and "风紧" in entry["text"]


def test_a2a_kinds_are_real_interactions():
    assert _A2A_KINDS
    assert all(isinstance(k, ix.InteractionKind) for k in _A2A_KINDS)
