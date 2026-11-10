"""居民叙事层：日记 + 当天记录 → 一份 Markdown。

为什么单独一层、且只在**日终**跑：
- 日记是「慢脑」产物，一天一条就够，放在 tick 里会把主循环拖死；
- 每人生成失败（没配密钥、超时、模型胡说）就用模板拼接，**永远有内容**；
- 产物是给人看的：`data/reports/diary/Day<N>.md`，用任何编辑器打开即可。

红线：LLM 不进主循环。本模块的 LLM 调用全部发生在 daemon 线程里，
结果写进内存字典，md 在生成完后再重写一次。
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from .llm_client import LLMClient, get_language

WISH_CN = {
    "wealth": "攒钱", "fame": "出名", "love": "被爱",
    "craft": "手艺", "study": "学识",
}
WISH_EN = {
    "wealth": "money", "fame": "recognition", "love": "love",
    "craft": "craft", "study": "learning",
}


def _day_items(sim, rid: int, day: int, limit: int = 14) -> list[str]:
    """从 sim.logs 里捞某居民当天的记录（新→旧，取最近的 limit 条再翻正）。"""
    out: list[str] = []
    for e in reversed(sim.logs):
        if e.get("rid") != rid or e.get("day") != day:
            continue
        txt = str(e.get("text") or "").strip()
        if not txt:
            continue
        if "晚上" in txt or "今天" in txt:
            # 带时刻的优先原样
            out.append(txt)
        else:
            out.append(txt)
        if len(out) >= limit:
            break
    out.reverse()
    return out


class DiaryBook:
    """一天一次的日记本。`days[day][rid] -> 文本`，`source` 记 llm/template。"""

    def __init__(self, llm_cfg):
        self.cfg = llm_cfg
        self.days: dict[int, dict[int, str]] = {}
        self.sources: dict[int, dict[int, str]] = {}
        self._busy: set[int] = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 模板
    def template(self, r, items: list[str], lang: str = "zh") -> str:
        """没有 LLM 时的日记：把当天记录串成一段人话。"""
        wish = getattr(r, "wish", None)
        wish_txt = ""
        if wish is not None:
            wish_txt = (f"我最近的心思都在「{wish.text}」上。" if lang == "zh"
                        else f"My mind keeps going back to one thing: {wish.text}.")
        if not items:
            return ("今天没什么特别的，一天就这么过去了。" if lang == "zh"
                    else "Nothing much happened today. The day just went by.")
        head = items[:6]
        if lang == "zh":
            body = "；".join(head)
            tail = "。日子大概就是这样一点一点过去的。" if len(items) <= 6 \
                else f"。除此之外还发生了些小事，都记不太清了。"
            return f"{wish_txt}今天{body}{tail}"
        body = "; ".join(head)
        return (f"{wish_txt} Today I {body}. That is how the days pass, "
                f"one small thing after another.")

    # ---------------------------------------------------------------- LLM
    def _prompt(self, r, items: list[str], lang: str) -> tuple[str, str]:
        wish = getattr(r, "wish", None)
        o = getattr(r, "ocean", None)
        who = []
        who.append(f"{r.age}岁" if lang == "zh" else f"{r.age} years old")
        if o is not None:
            who.append(
                f"外向{o.extraversion:.1f} 亲和{o.agreeableness:.1f} "
                f"神经质{o.neuroticism:.1f}" if lang == "zh" else
                f"extraversion {o.extraversion:.1f}, agreeableness "
                f"{o.agreeableness:.1f}, neuroticism {o.neuroticism:.1f}")
        if wish is not None:
            who.append(("愿望：" + wish.text) if lang == "zh" else ("wish: " + wish.text))
        acts = "\n".join(f"- {x}" for x in items) or (
            "- 没什么事发生" if lang == "zh" else "- nothing in particular")
        if lang == "zh":
            system = ("你在为一座小镇上的普通居民写**私人日记**。"
                      "用第一人称，写 60~120 字，口语、具体、别抒情、别总结人生。"
                      "只输出日记正文，不要标题、不要引号。")
            user = f"人物：{r.name}，{'，'.join(who)}。\n今天发生的事：\n{acts}"
        else:
            system = ("You write the private diary of an ordinary small-town resident. "
                      "First person, 40-80 words, plain and concrete. No moralising, "
                      "no summary of life. Output only the diary text.")
            user = f"Person: {(r.name_en or r.name)}, {', '.join(who)}.\nToday:\n{acts}"
        return system, user

    def on_day_end(self, sim, day: int) -> None:
        """日终：先模板填满（立即可导出），再后台用 LLM 覆盖。"""
        if day < 0:
            return
        llm_cfg = self.cfg
        want_diary = bool(getattr(llm_cfg, "diary", True)) if llm_cfg else False
        lang = get_language()

        with self._lock:
            self.days[day] = {}
            self.sources[day] = {}
        snapshot: list[tuple] = []
        for r in sim.residents:
            items = _day_items(sim, r.rid, day)
            txt = self.template(r, items, lang)
            self.days[day][r.rid] = txt
            self.sources[day][r.rid] = "template"
            snapshot.append((r, items, txt))

        # 先落一版 md，保证「就算 LLM 全挂，用户也有东西看」
        try:
            self.export_md(sim, day)
        except Exception:  # noqa: BLE001
            pass

        if not want_diary:
            return
        client = LLMClient(llm_cfg)
        if not client.available:
            return
        with self._lock:
            if day in self._busy:
                return
            self._busy.add(day)

        def worker() -> None:
            try:
                for r, items, _ in snapshot:
                    sys_p, usr_p = self._prompt(r, items, lang)
                    txt = client.chat(sys_p, usr_p, lang=lang,
                                      max_tokens=320, temperature=0.95)
                    if not txt:
                        continue
                    txt = txt.strip().strip('"').strip("「」")
                    if len(txt) < 12:
                        continue
                    with self._lock:
                        self.days[day][r.rid] = txt
                        self.sources[day][r.rid] = "llm"
            finally:
                with self._lock:
                    self._busy.discard(day)
                try:
                    self.export_md(sim, day)   # 生成完再重写一次
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=worker, name=f"diary-d{day}", daemon=True).start()

    # ---------------------------------------------------------------- 导出
    def export_md(self, sim, day: int) -> Path:
        st = getattr(sim.cfg, "storage", None)
        rel = getattr(st, "reports_dir", "data/reports") if st else "data/reports"
        root = Path(__file__).resolve().parent.parent.parent
        out_dir = (Path(rel) if Path(rel).is_absolute() else root / rel) / "diary"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"Day{day}.md"
        lang = get_language()
        with self._lock:
            texts = dict(self.days.get(day, {}))
            srcs = dict(self.sources.get(day, {}))
        lines: list[str] = []
        if lang == "zh":
            lines.append(f"# EvilTown · 第 {day} 天")
            lines.append("")
            lines.append(f"> 生成于 {datetime.now():%Y-%m-%d %H:%M} · "
                         f"{len(sim.residents)} 位居民 · "
                         f"日记 {sum(1 for s in srcs.values() if s == 'llm')} 篇由模型生成，"
                         f"其余为模板")
        else:
            lines.append(f"# EvilTown · Day {day}")
            lines.append("")
            lines.append(f"> Generated {datetime.now():%Y-%m-%d %H:%M} · "
                         f"{len(sim.residents)} residents · "
                         f"{sum(1 for s in srcs.values() if s == 'llm')} diaries written by the model, "
                         f"the rest templated")
        lines.append("")
        for r in sim.residents:
            wish = getattr(r, "wish", None)
            tag = WISH_CN if lang == "zh" else WISH_EN
            if wish is not None:
                pct = int(round(max(0.0, min(1.0, wish.ratio)) * 100))
                head = (f"`{tag.get(wish.kind, wish.kind)}` {wish.text}（{pct}%）"
                        if lang == "zh" else
                        f"`{tag.get(wish.kind, wish.kind)}` {wish.text} ({pct}%)")
            else:
                head = "—"
            title = (f"## {r.name}" if lang == "zh" else f"## {(r.name_en or r.name)}")
            lines.append(title)
            lines.append("")
            lines.append(("**愿望**：" if lang == "zh" else "**Wish**: ") + head)
            lines.append("")
            body = texts.get(r.rid) or "—"
            lines.append(("**日记**：" if lang == "zh" else "**Diary**: ") + body)
            src = srcs.get(r.rid, "template")
            lines.append("")
            items = _day_items(sim, r.rid, day, limit=10)
            if items:
                lines.append(("**当天记录**：" if lang == "zh" else "**Log**: "))
                lines.append("")
                for it in items:
                    lines.append(f"- {it}")
                lines.append("")
            lines.append(f"<!-- source: {src} -->")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
