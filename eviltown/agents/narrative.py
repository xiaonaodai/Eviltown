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
from .memory_rag import query_from_state

WISH_CN = {
    "wealth": "攒钱", "fame": "出名", "love": "被爱",
    "craft": "手艺", "study": "学识",
}
WISH_EN = {
    "wealth": "money", "fame": "recognition", "love": "love",
    "craft": "craft", "study": "learning",
}


# ==================================================== 日记复盘（reflection）→ 软偏好
# 一天一次、只发生在 on_day_end 的 daemon 线程里（红线：LLM 不进主循环）。
# 关闭（ReflectCfg.enabled=False）时 _reflect_all 立刻返回：不发请求、不写库、
# 不动居民状态 —— 行为与没有这个功能时逐位一致。
_ROOT = Path(__file__).resolve().parent.parent.parent


def _warn(msg: str, *args) -> None:
    """复盘的告警日志。本模块历来不打日志，这里延迟 import 免得改动顶层依赖。"""
    import logging
    logging.getLogger(__name__).warning(msg, *args)


def _reflect_kinds() -> list[str]:
    """ActionType 白名单。延迟 import：narrative 在 sim 早期就被导入，避免环依赖。"""
    try:
        from .brain import ActionType
    except Exception:  # noqa: BLE001
        return []
    return [k.value for k in ActionType]


# ---------------------------------------- 反思层 2：性格微调（traits，默认关）
# 层 1 只改「明天更想干什么」的软权重；这一层动的是 OCEAN 六维**本身**，所以规矩更严：
# 白名单六键、单项小幅、结果夹在 [0,1]、每天先向出生基线回弹，且**复用层 1 的那一次
# chat**（不许新增 LLM 调用）。
TRAIT_KEYS = ("openness", "conscientiousness", "extraversion",
              "agreeableness", "neuroticism", "greed")

_TRAIT_PROMPT_ZH = (
    "\n另外：如果今天的事让你在性格上有了一点变化，就在**同一个** JSON 对象里加一份 "
    'traits，形如 {"biases":[], "traits":{"conscientiousness":0.02}}。\n'
    "traits 的键只能取 openness / conscientiousness / extraversion / agreeableness / "
    "neuroticism / greed，值是 -{d:g}~{d:g} 之间的小数（正=更强，负=更弱），最多 2 个键；"
    '没有想改的就给空对象 {{"traits":{{}}}}。')
_TRAIT_PROMPT_EN = (
    "\nAlso: if today shifted your personality a little, add traits to the **same** "
    'JSON object, e.g. {"biases":[], "traits":{"conscientiousness":0.02}}.\n'
    "Traits keys may only be openness / conscientiousness / extraversion / "
    "agreeableness / neuroticism / greed; values are small numbers in [-{d:g}, {d:g}] "
    "(positive = stronger, negative = weaker), at most 2 keys; if nothing changed "
    'output {"traits":{}}.')


def _parse_reflect_json(txt, quiet: bool = False) -> dict | None:
    """把模型回复里那段 JSON 对象抠出来：biases 与 traits **共用这一次解析**。

    返回 None = 这段回复整体不可用（没有对象 / 不是合法 JSON / 顶层不是对象）。
    粒度由调用方决定：traits 坏了不影响 biases，反过来也一样。
    """
    if not txt:
        return None
    raw = str(txt).strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        if not quiet:
            _warn("reflect: 回复里没有 JSON 对象，整批丢弃：%.80s", raw)
        return None
    import json
    try:
        obj = json.loads(raw[a:b + 1])
    except Exception:  # noqa: BLE001
        if not quiet:
            _warn("reflect: JSON 解析失败，整批丢弃：%.80s", raw)
        return None
    return obj if isinstance(obj, dict) else None


def _num(v) -> float | None:
    """把字段值转成有限 float；不是数字就返回 None（脏数据只跳过，绝不抛）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def normalize_traits(raw, max_delta: float) -> dict[str, dict]:
    """把 traits 校验成 `{维度: {"delta": float, "note": str}}`。

    与 normalize_biases 的严格程度**故意不同**：
    * 白名单外的键 **逐条丢弃**（多一个乱键不构成整批不可信）；
    * 超幅 **clamp** 到 ±max_delta 而不是丢弃（「方向对、力度大」最常见）；
    * 只有 traits 整体不是对象时才整批丢 —— 那种回复对任何一维都没有信息量。
    `raw` 可以是已解析的 dict（含 "traits" 键，或本身就是 traits 映射），也可以是原文。
    带 note 是为了让 memory_events 那句「性格：conscientiousness +0.020（…）」可追溯。
    """
    obj = raw
    if isinstance(obj, str):
        obj = _parse_reflect_json(obj) or {}
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        _warn("reflect: traits 不是对象（%s），整批丢弃", type(obj).__name__)
        return {}
    items = obj["traits"] if "traits" in obj else obj
    if items is None:
        return {}
    if not isinstance(items, dict):
        _warn("reflect: traits 类型不对（%s），整批丢弃", type(items).__name__)
        return {}
    cap = abs(_num(max_delta) or 0.0)
    out: dict[str, dict] = {}
    for k, v in items.items():
        key = str(k).strip()
        if key not in TRAIT_KEYS:
            _warn("reflect: 未知性格维度 %r 不在白名单，丢弃", key)
            continue
        note = ""
        if isinstance(v, dict):                      # {"delta":0.02,"reason":"…"} 也认
            note = str(v.get("reason") or v.get("note") or "").strip()[:40]
            v = v.get("delta", v.get("value"))
        d = _num(v)
        if d is None:
            _warn("reflect: %s 的 delta 不是数字（%r），丢弃该维", key, v)
            continue
        d = max(-cap, min(cap, d))                   # 超幅 clamp，不丢
        if not d:
            continue                                 # 0 等于没变
        out[key] = {"delta": round(d, 4), "note": note}
    return out


def _apply_traits(sim, day: int, r, traits: dict,
                  max_delta: float, regress: float) -> list[tuple[str, float, str]]:
    """把当天的小幅性格改变落到 `r.ocean`，返回实际改动 `[(维度, delta, 理由)]`。

        v = base + (v - base) * (1 - regress)    # ① 先向出生基线回弹
        v = clamp(v + delta, 0, 1)               # ② 再叠加当天改变并夹回量纲

    为什么**每天**都回弹（哪怕今天没有信号）：只累加的话，跑几天性格就漂到极端；
    回弹让它成为「慢变量」—— 在出生基线附近起伏，而不是被某一天的事改写一辈子。
    * `ocean_base is None` ⇒ 第一次应用，深拷贝当前 ocean 当基线；
    * 居民没有 ocean（测试假对象 / 老数据）⇒ 什么都不做：宁可不改，也不造假性格。
    （sim / day 保留在签名里：与层 1 的调用形态对齐，将来要用它落日志也方便。）
    """
    ocean = getattr(r, "ocean", None)
    if ocean is None:
        return []
    import copy
    base = getattr(r, "ocean_base", None)
    if base is None:
        base = copy.deepcopy(ocean)
        try:
            r.ocean_base = base
        except Exception:  # noqa: BLE001
            return []
    reg = min(1.0, max(0.0, _num(regress) or 0.0))
    cap = abs(_num(max_delta) or 0.0)
    out: list[tuple[str, float, str]] = []
    for k in TRAIT_KEYS:                     # 固定顺序：事件文本稳定、便于 diff
        ent = (traits or {}).get(k) or {}
        d = max(-cap, min(cap, _num(ent.get("delta")) or 0.0))
        cur = _num(getattr(ocean, k, None))
        if cur is None:
            continue
        b = _num(getattr(base, k, None))
        if b is None:
            b = cur
        v = b + (cur - b) * (1.0 - reg)       # ① 回弹到出生基线
        v = min(1.0, max(0.0, v + d))         # ② 叠加当天改变 + 区间 clamp
        setattr(ocean, k, round(v, 4))
        if d:
            out.append((k, d, str(ent.get("note") or "")))
    return out


def normalize_biases(txt: str, cap: float, max_items: int, day: int) -> list[dict]:
    """把模型的复盘回复校验成 journal_bias。**严格：任一条不合法 ⇒ 整批丢弃**。

    为什么整批丢而不是「能用的先用」：偏好会叠加到决策打分上，半对半错的批次
    最难排查；宁可这天的复盘不生效（行为等同关闭），也要保证生效的每一条都可解释。
    """
    if not txt or max_items <= 0:
        return []
    obj = _parse_reflect_json(txt)
    if obj is None:
        return []
    items = obj.get("biases")
    if not isinstance(items, list):
        _warn("reflect: 缺少 biases 数组，整批丢弃：%.80s", str(txt).strip())
        return []
    kinds = _reflect_kinds()
    try:
        cap = abs(float(cap))
    except (TypeError, ValueError):
        cap = 0.0
    out: list[dict] = []
    for it in items:
        if len(out) >= max_items:
            break
        if not isinstance(it, dict):
            _warn("reflect: biases 里有非对象项，整批丢弃")
            return []
        kind = str(it.get("kind") or "").strip()
        try:
            bonus = float(it.get("bonus"))
        except (TypeError, ValueError):
            _warn("reflect: bonus 不是数字（%r），整批丢弃", it.get("bonus"))
            return []
        if bonus != bonus or bonus in (float("inf"), float("-inf")):
            _warn("reflect: bonus 非有限数（%r），整批丢弃", bonus)
            return []
        if kind not in kinds:
            _warn("reflect: 未知 kind=%r 不在 ActionType 白名单，整批丢弃", kind)
            return []
        bonus = max(-cap, min(cap, bonus))      # clamp 到 [-cap, cap]
        if not bonus:
            continue                            # 0 分等于没说
        out.append({"kind": kind, "bonus": round(bonus, 3), "day": int(day),
                    "note": str(it.get("reason") or "").strip()[:60]})
    return out


def _reflect_prompt(r, items: list[str], diary: str, lang: str,
                    cap: float, max_items: int) -> tuple[str, str]:
    """复盘 prompt：只许输出 JSON。给人看的语言跟随界面语言。"""
    kinds = " / ".join(_reflect_kinds())
    if lang == "en":
        sys_p = ("You review one villager's day and output JSON only — no prose, "
                 "no code fence.\n"
                 'Shape: {"biases":[{"kind":"...","bonus":-3,"reason":"one line"}]}\n'
                 f"kind must be one of: {kinds}.\n"
                 f"bonus is a number in [-{cap:g}, {cap:g}]: positive = wants more of it "
                 f"tomorrow, negative = wants less. At most {max_items} entries; "
                 'if nothing stands out output {"biases":[]}.')
        user = f"Person: {(r.name_en or r.name)}\n"
        if diary:
            user += f"Diary today:\n{diary}\n"
        if items:
            user += "Today's log:\n" + "\n".join(f"- {x}" for x in items)
        return sys_p, user
    sys_p = ("你替一位小镇居民做一天结束时的复盘，只输出 JSON：不要解释，不要代码围栏。\n"
             '格式：{"biases":[{"kind":"...","bonus":-3,"reason":"一句话"}]}\n'
             f"kind 只能取：{kinds}。\n"
             f"bonus 取 [-{cap:g}, {cap:g}] 之间的数字：正数=明天更想做，负数=明天更不想做。"
             f"最多 {max_items} 条；没特别的可说就输出 {{\"biases\":[]}}。")
    user = f"人物：{r.name}\n"
    if diary:
        user += f"今天的日记：\n{diary}\n"
    if items:
        user += "今天的记录：\n" + "\n".join(f"- {x}" for x in items)
    return sys_p, user


def _write_reflect_event(sim, day: int, r, biases: list[dict]) -> None:
    """复盘结果落一条 memory_events（kind=reflect）：明天召回时才「记得」自己复盘过。"""
    st = getattr(getattr(sim, "cfg", None), "storage", None)
    if st is None or not getattr(st, "record_events", True):
        return
    text = "复盘：" + "；".join(f"{b['kind']}{b['bonus']:+.1f}（{b['note']}）" for b in biases)
    tick = int(getattr(sim, "tick", 0) or 0)
    try:
        from ..storage import db as db_mod
        rel = getattr(st, "db_path", "data/eviltown.db")
        path = Path(rel) if Path(rel).is_absolute() else _ROOT / rel
        con = db_mod.connect(path)
        try:
            con.execute(
                "INSERT INTO memory_events (day, tick, kind, text, actor, target, place, weight)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (int(day), tick, "reflect", text[:400],
                 int(getattr(r, "rid", 0) or 0), None, None, 1.0),
            )
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        _warn("reflect: 写 memory_events 失败（不影响决策与日记）")


def _reflect_all(sim, snapshot: list[tuple], day: int, client, lang: str, cfg) -> None:
    """日记写完后（同线程）对每人做一次复盘，产出几条软偏好。

    * cfg 缺失 / enabled=False ⇒ 立刻返回（零行为变化）；
    * 每人的偏好**整体替换**（list 赋值原子），且只保留最近 2 天；
    * 任何异常都吞掉：复盘失败不能影响日记与主循环。
    """
    if cfg is None or not bool(getattr(cfg, "enabled", False)):
        return
    try:
        cap = float(getattr(cfg, "bonus_cap", 6.0) or 0.0)
        max_items = int(getattr(cfg, "max_items", 2) or 0)
    except (TypeError, ValueError):
        return
    if max_items <= 0:
        return
    for r, items, diary in snapshot:
        try:
            sys_p, usr_p = _reflect_prompt(r, items, diary, lang, cap, max_items)
            txt = client.chat(sys_p, usr_p, lang=lang, max_tokens=220, temperature=0.5)
        except Exception:  # noqa: BLE001
            continue
        got = normalize_biases(txt, cap, max_items, day)
        if got:                             # 整批丢弃 ⇒ 保持原偏好不变
            keep: list[dict] = []
            for e in (getattr(r, "journal_bias", None) or []):
                if not isinstance(e, dict):
                    continue
                try:
                    if int(e.get("day", -99)) >= int(day) - 1:
                        keep.append(e)
                except (TypeError, ValueError):
                    continue
            r.journal_bias = keep + got      # list 赋值原子，只留最近 2 天
            r.journal_cap = cap              # 让 brain 的封顶跟配置联动
            _write_reflect_event(sim, day, r, got)
        # --- 反思层 2：性格微调（同一份回复、同一次 chat，绝不额外发请求）---
        # 顺序很关键：层 1 已经落地，traits 坏了只丢 traits，不回头影响偏好。
        if not bool(getattr(cfg, "personality", False)):
            continue
        try:
            max_delta = abs(float(getattr(cfg, "max_delta", 0.03) or 0.0))
            regress = float(getattr(cfg, "regress", 0.05) or 0.0)
        except (TypeError, ValueError):
            max_delta, regress = 0.03, 0.05
        obj = _parse_reflect_json(txt, quiet=True)
        raw_traits = obj.get("traits") if isinstance(obj, dict) else None
        traits = normalize_traits(raw_traits if raw_traits is not None else {}, max_delta)
        # 即使今天没有信号也要走一遍：性格每天都要向出生基线回弹一点。
        applied = _apply_traits(sim, day, r, traits, max_delta, regress)
        if applied:
            _write_trait_event(sim, day, r, applied)


def _write_trait_event(sim, day: int, r, applied: list[tuple[str, float, str]]) -> None:
    """性格改变落一条 memory_events（kind=reflect_trait）：明天召回时才「记得」自己变了。"""
    if not applied:
        return
    st = getattr(getattr(sim, "cfg", None), "storage", None)
    if st is None or not getattr(st, "record_events", True):
        return
    text = "性格：" + "；".join(
        f"{k} {d:+.3f}" + (f"（{n}）" if n else "") for k, d, n in applied)
    tick = int(getattr(sim, "tick", 0) or 0)
    try:
        from ..storage import db as db_mod
        rel = getattr(st, "db_path", "data/eviltown.db")
        path = Path(rel) if Path(rel).is_absolute() else _ROOT / rel
        con = db_mod.connect(path)
        try:
            con.execute(
                "INSERT INTO memory_events (day, tick, kind, text, actor, target, place, weight)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (int(day), tick, "reflect_trait", text[:400],
                 int(getattr(r, "rid", 0) or 0), None, None, 1.0),
            )
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        _warn("reflect: 写 memory_events(kind=reflect_trait) 失败（不影响决策与日记）")


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
    def _prompt(self, r, items: list[str], lang: str,
                memories: list[str] | None = None) -> tuple[str, str]:
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
            if memories:
                user += "\n你还记得：\n" + "\n".join(f"- {m}" for m in memories)
        else:
            system = ("You write the private diary of an ordinary small-town resident. "
                      "First person, 40-80 words, plain and concrete. No moralising, "
                      "no summary of life. Output only the diary text.")
            user = f"Person: {(r.name_en or r.name)}, {', '.join(who)}.\nToday:\n{acts}"
            if memories:
                user += "\nYou also remember:\n" + "\n".join(f"- {m}" for m in memories)
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
                    # 长期记忆（RAG）：给日记 prompt 塞 1~2 条「你还记得…」，不阻塞主循环。
                    mems: list[str] = []
                    _h = getattr(sim, "history", None)
                    if _h is not None and _h.enabled:
                        try:
                            mems = _h.remember_lines(r.name, now_day=day, r=r)
                        except Exception:  # noqa: BLE001
                            mems = []
                    sys_p, usr_p = self._prompt(r, items, lang, mems)
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
