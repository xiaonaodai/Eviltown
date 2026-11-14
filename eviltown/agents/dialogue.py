"""A2A 双回合对白合成：两个居民相遇时，让大模型写出「A 说一句 → B 接一句」。

为什么单开一个模块（而不是塞进 sim.py）：
- **绝不能阻塞 tick**：一次 HTTP 少则几百毫秒、多则几秒，而主循环每 tick 都在跑。
  所以这里只做两件事 —— 主线程 `submit()` 入队、`poll()` 取成品；真正的请求
  交给一个守护线程。主线程永远不等网络。
- **失败必须降级**：未启用 / 超时 / 返回空 / 解析不出两句 —— 一律返回 `None`，
  调用方继续用 interactions.py 里的模板文案，行为与没有这个功能时完全一致。
- **节流是硬要求**：本地 Ollama / Qwen 跑在 CPU 上时，并发几路就能把进程拖死。
  这里用「有界队列 + 最小间隔 + 每日上限」三层限制，满了直接丢请求，不排队等。
- **线程安全**：线程只往输出 deque 里放结果，事件表 / 日志一律由**主线程** `poll()`
  取走后再写 —— 写仿真状态的地方永远只有一个线程。

红线（AGENTS.md）：LLM 不进主循环。本模块不发请求时不占 CPU，挂了不影响仿真。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .llm_client import LLMClient, get_language

__all__ = [
    "DialogueRequest",
    "DialogueLine",
    "DialogueSynth",
    "parse_pair",
]

_SYSTEM = (
    "你是一个小镇居民对白生成器。给定两位居民的姓名、处境和彼此的关系，"
    "请写出两句自然的对话：A 先开口，B 接一句。要求：\n"
    "1. 每句不超过 30 个汉字（或 20 个英文单词）；\n"
    "2. 口语化、有生活气，能体现各自的处境和关系；\n"
    "3. 不要旁白、不要舞台说明、不要解释；\n"
    '4. 只输出 JSON：{"a": "A 说的话", "b": "B 接的话"}'
)

# 从模型回复里捞出第一个 JSON 对象（模型常常在 JSON 前后加客套话）
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.S)

# 行首角色前缀：A: / A： / 甲： / 说话人A： / speaker a:
_PREFIX_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?\"?(?:A|B|甲|乙|说话人\s*[AB]|speaker[\s_]?[ab])\"?\s*[:：]\s*",
    re.I,
)

# 单行对白里用来切出 B 那半句的分隔符（模型没换行时的兜底）
_SPLIT_SEPS = ("B：", "B:", "乙：", "说话人B：", "B 说：")

_MAX_LINE = 80      # 单句硬上限（字符），超了截断，避免一句吃掉整屏日志


@dataclass(frozen=True)
class DialogueRequest:
    """一次对白请求。`meta` 由调用方自填（sim 用它记 tick / 两人 rid）。"""

    speaker_a: str
    speaker_b: str
    context: str = ""          # 双方状态 + 关系的一句话描述，由调用方组装
    scene: str = ""            # 场景（如「在食堂门口」，留空 = 街头偶遇）
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DialogueLine:
    """合成成功的一段对白。"""

    speaker_a: str
    speaker_b: str
    line_a: str
    line_b: str
    meta: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        """一行文本，给日志 / 事件表用。"""
        return f"{self.speaker_a}：{self.line_a} {self.speaker_b}：{self.line_b}"


def _clean(text: Any) -> str:
    """去掉引号与角色前缀，压掉多余空白。"""
    s = str(text or "").strip()
    s = s.strip("\"'“”‘’「」").strip()
    s = _PREFIX_RE.sub("", s).strip()
    s = s.strip("\"'“”‘’「」").strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return ""
    return s if len(s) <= _MAX_LINE else s[:_MAX_LINE].rstrip()


def _pick(obj: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def parse_pair(text: str | None) -> tuple[str, str] | None:
    """把模型回复解析成 (A 的话, B 的话)。解析不出就返回 None。

    三级兜底：整体 JSON → 文本里的 JSON 片段 → 带角色前缀的两行 / 单行分隔。
    级级都很宽松：模型多数时候不老实，但「两句人话」通常还是给得出来的。
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    # ① JSON（模型按 _SYSTEM 要求输出时的主路径）
    candidates: list[str] = [raw]
    m = _JSON_OBJ_RE.search(raw)
    if m and m.group(0).strip() != raw:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        a = _clean(_pick(data, ("a", "A", "line_a", "speaker_a_line", "甲")))
        b = _clean(_pick(data, ("b", "B", "line_b", "speaker_b_line", "乙")))
        if a and b:
            return a, b

    # ② 单行里带 B 前缀：`A：你好 B：吃了吗`
    if "\n" not in raw:
        for sep in _SPLIT_SEPS:
            if sep in raw:
                left, right = raw.split(sep, 1)
                a, b = _clean(left), _clean(right)
                if a and b:
                    return a, b

    # ③ 按行取前两行（`A：你好\nB：吃了吗`，或干脆没有前缀的两行）
    picked: list[str] = []
    for ln in raw.splitlines():
        c = _clean(ln)
        if c:
            picked.append(c)
        if len(picked) >= 2:
            break
    if len(picked) == 2 and picked[0] != picked[1]:
        return picked[0], picked[1]
    return None


class DialogueSynth:
    """对白合成器：有界队列 + 单守护线程 + 双重节流。

    典型用法（sim.py）::

        synth.submit(DialogueRequest("张三", "李四", ctx, meta={"tick": t}))
        ...
        for line in synth.poll():          # 主线程，同一 tick 里取，非阻塞
            self.events.append(...)

    `cfg` 传 `config.LLMCfg`；`enabled` 由 `llm.enabled + llm.a2a + 地址 + 模型名` 共同决定，
    任何一项缺了就整体关掉，`submit()` 直接返回 False（调用方无需关心为什么没开）。
    """

    def __init__(self, cfg, *, max_queue: int = 16, min_interval: float = 6.0,
                 max_per_day: int = 120, max_tokens: int = 200,
                 temperature: float = 0.9):
        self.cfg = cfg
        self._switch = bool(getattr(cfg, "a2a", False))   # llm.a2a 总开关
        self._client = LLMClient(cfg)
        self._max_queue = max(1, int(max_queue))
        self._min_interval = max(0.0, float(min_interval))
        self._max_per_day = max(0, int(max_per_day))
        self._max_tokens = max(32, int(max_tokens))
        self._temperature = float(temperature)

        self._q: queue.Queue[DialogueRequest] = queue.Queue(maxsize=self._max_queue)
        self._done: deque[DialogueLine] = deque(maxlen=self._max_queue * 4)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._last_ts = 0.0          # 上一次成功生成的时间（monotonic）
        self._day_key = time.strftime("%Y%m%d")
        self._count = 0              # 今日成功条数
        self._dropped = 0            # 队列满被丢掉的请求数

    # ---- 状态 -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        # 总开关 + 客户端可用性缺一不可：a2a 关着时连 submit 都必须直接拒绝，
        # 否则「默认关」只是 sim 不建合成器，一旦别处建了就偷偷发请求了。
        return bool(self._switch and self._client.available)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            ready = len(self._done)
        return {
            "enabled": self.enabled,
            "queued": self._q.qsize(),
            "ready": ready,
            "generated": self._count,
            "dropped": self._dropped,
        }

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or not self.enabled:
            return
        self._thread = threading.Thread(
            target=self._loop, name="a2a-dialogue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)      # 唤醒可能等在 get() 上的线程
        except queue.Full:
            pass

    # ---- 主线程接口（都不阻塞）-------------------------------------------

    def submit(self, req: DialogueRequest) -> bool:
        """入队一次请求。返回 False = 没启用 / 队列满（被丢），调用方继续走模板。"""
        if not self.enabled or self._stop.is_set():
            return False
        if self._thread is None:
            self.start()
        try:
            self._q.put_nowait(req)
        except queue.Full:
            self._dropped += 1
            return False
        return True

    def poll(self, limit: int = 8) -> list[DialogueLine]:
        """取走已合成的对白（主线程调用）。没有成品就返回空列表。"""
        out: list[DialogueLine] = []
        with self._lock:
            while self._done and len(out) < limit:
                out.append(self._done.popleft())
        return out

    # ---- 后台线程 ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                req = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if req is None:               # stop() 的唤醒信号
                break
            try:
                line = self._generate(req)
            except Exception:  # noqa: BLE001 —— 后台线程绝不能因单次失败整体死掉
                line = None
            if line is not None:
                with self._lock:
                    self._done.append(line)

    def _allow_now(self) -> bool:
        """节流：跨天重置计数 → 日上限 → 最小间隔（在 worker 里等，等待可被 stop 打断）。"""
        key = time.strftime("%Y%m%d")
        if key != self._day_key:
            self._day_key, self._count = key, 0
        if self._max_per_day and self._count >= self._max_per_day:
            return False
        gap = self._min_interval - (time.monotonic() - self._last_ts)
        if gap > 0 and self._stop.wait(min(gap, self._min_interval)):
            return False                  # 等节流期间被 stop 了
        return True

    def _build_prompt(self, req: DialogueRequest) -> str:
        scene = req.scene or "在街头偶遇"
        ctx = req.context.strip() or "两人此前并不熟。"
        lang = get_language()
        if lang == "en":
            head = (f"[Scene] {scene}\n[A] {req.speaker_a}\n"
                    f"[B] {req.speaker_b}\n[Context] {ctx}\n")
            return head + "Write the two lines now (A speaks first, B replies)."
        head = (f"【场景】{scene}\n【A】{req.speaker_a}\n"
                f"【B】{req.speaker_b}\n【处境】{ctx}\n")
        return head + "现在写这两句对白（A 先开口，B 接话）。"

    def _generate(self, req: DialogueRequest) -> DialogueLine | None:
        if not self._allow_now():
            return None
        text = self._client.chat(
            _SYSTEM, self._build_prompt(req),
            max_tokens=self._max_tokens, temperature=self._temperature)
        pair = parse_pair(text)
        if pair is None:
            return None
        self._last_ts = time.monotonic()
        self._count += 1
        a, b = pair
        return DialogueLine(req.speaker_a, req.speaker_b, a, b, dict(req.meta))
