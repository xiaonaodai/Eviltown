"""长期愿望：慢脑（LLM）在 EvilTown 的第一个落点。

设计红线（AGENTS.md「LLM 不进主循环」）：
- **一次生成**：只在出生时跑，不参与 tick；
- **可降级**：模型不可用 / 超时 / 格式错 → 退回模板，`source` 如实标注；
- **不阻塞**：先人手一份模板愿望（瞬时），后台线程再逐批用 LLM 覆盖；
- **只产数据不产决策**：LLM 写 text + 选 kind，真正影响行为的是规则层
  （brain 按 kind 给效用加权，权重小到盖不过饿困累）。

为什么 kind 必须归一：自由文本没法进打分函数。LLM 负责「这人有故事」，
规则负责「这人今天干什么」——两层各干各的，谁也不越界。
"""
from __future__ import annotations

import json
import random
import re
import threading
import urllib.request
from dataclasses import asdict, dataclass

# 规则层只认这 5 个钩子（改这里必须同步 brain.WISH_BONUS）
WISH_KINDS = ("wealth", "fame", "love", "craft", "study")

KIND_CN = {"wealth": "攒钱", "fame": "出名", "love": "感情",
           "craft": "手艺", "study": "学识"}

# 每类的目标区间（进度单位见 _progress 的注释）
TARGET_RANGE = {
    "wealth": (8000.0, 40000.0),   # 元
    "fame": (8.0, 25.0),           # 熟人个数
    "love": (1.0, 2.0),            # 亲密关系个数
    "craft": (20.0, 80.0),         # 钓鱼次数
    "study": (0.3, 0.9),           # 学识 0..1
}


@dataclass
class Wish:
    """一个人的长期愿望。`source` 是诚实标记：llm=模型写的，template=兜底。"""
    kind: str = "study"
    text: str = ""
    target: float = 0.5
    progress: float = 0.0
    source: str = "template"
    born_tick: int = 0
    done: bool = False

    @property
    def ratio(self) -> float:
        if self.target <= 0:
            return 1.0
        return max(0.0, min(1.0, self.progress / self.target))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ratio"] = round(self.ratio, 3)
        d["kind_cn"] = KIND_CN.get(self.kind, self.kind)
        return d


# ---------------------------------------------------------------- 模板兜底
_TEMPLATES = {
    "wealth": ["想在镇上攒够一笔钱，开一家自己的小店",
               "想让存折上的数字厚到不用看价钱点菜",
               "想攒够钱，把家里那间破屋换成一栋新房"],
    "fame": ["想成为全镇都认识的人，走到哪都有人打招呼",
             "想出名一次，让所有人都记得这个名字",
             "想当上镇里的话事人，说话有人听"],
    "love": ["想找到一个能一起过日子的人",
             "想和某个人成为特别的关系",
             "想有人在家里等他回来"],
    "craft": ["想成为钓鱼高手，把池塘里的鱼都钓一遍",
              "想把一门手艺练到闭着眼睛都会",
              "想把手上的活做成别人学不来的样子"],
    "study": ["想多读点书，不再是个糊涂人",
              "想在学堂里学到真本事，靠脑子挣钱",
              "想让人提起他时说一句『那人有学问』"],
}


def _pick_kind(ocean, rng: random.Random) -> str:
    """按性格给 5 类愿望加权：财迷偏钱、外向偏心出名、随和偏感情…"""
    weights = [
        (ocean.greed, "wealth"),
        (ocean.extraversion, "fame"),
        (ocean.agreeableness, "love"),
        (ocean.openness, "craft"),
        (ocean.conscientiousness, "study"),
    ]
    kinds = [k for _, k in weights]
    ws = [max(0.02, w) for w, _ in weights]
    return rng.choices(kinds, weights=ws, k=1)[0]


def _scale_target(kind: str, ocean, rng: random.Random) -> float:
    lo, hi = TARGET_RANGE[kind]
    # 性格把目标往区间一端推：越财迷越想攒更多、越外向越想认识更多人…
    drive = {"wealth": ocean.greed, "fame": ocean.extraversion,
             "love": ocean.agreeableness, "craft": ocean.openness,
             "study": ocean.conscientiousness}[kind]
    t = rng.uniform(0.0, 1.0) * 0.65 + drive * 0.35
    return round(lo + (hi - lo) * t, 2)


def template_wish(r, rng: random.Random, tick: int = 0) -> Wish:
    kind = _pick_kind(r.ocean, rng)
    return Wish(kind=kind, text=rng.choice(_TEMPLATES[kind]),
                target=_scale_target(kind, r.ocean, rng),
                source="template", born_tick=tick)


# ---------------------------------------------------------------- LLM 生成
_SYS = (
    "你是「邪恶小镇」居民的内心独白作者。为给出的每个居民写一条长期愿望："
    "他这辈子最想做成的一件事。要具体、口语化、20~40 个汉字，"
    "能看出这个人的性格与处境（职业、年龄、脾气）。\n"
    "只输出一个 JSON 数组，不要任何解释、不要 markdown 代码块。每项格式：\n"
    '{"rid": 编号, "kind": "wealth|fame|love|craft|study", "target": 数字, "text": "愿望"}\n'
    "kind：wealth=攒钱、fame=出名、love=感情、craft=手艺、study=学识。\n"
    "target 单位：wealth=元(8000~40000)、fame=认识的人数(8~25)、"
    "love=亲密关系个数(1~2)、craft=钓鱼次数(20~80)、study=学识0~1(0.3~0.9)。"
)


def _person_line(r) -> str:
    o = r.ocean
    job = getattr(r.job, "value", str(r.job))
    return (f"rid={r.rid} {r.name} {r.sex.value} {r.age}岁 职业={job} "
            f"外向{o.extraversion:.2f} 随和{o.agreeableness:.2f} "
            f"尽责{o.conscientiousness:.2f} 开放{o.openness:.2f} "
            f"神经质{o.neuroticism:.2f} 财迷{o.greed:.2f} 存款{int(r.money)}元")


def _headers(cfg) -> dict:
    """有密钥才发 Authorization：本地服务免鉴权，空 key 别发空 Bearer。"""
    h = {"Content-Type": "application/json"}
    key = str(getattr(cfg, "api_key", "") or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h

def _post(cfg, messages, max_tokens: int = 2048) -> str:
    body = {"model": cfg.model, "messages": messages,
            "temperature": 0.85, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        cfg.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(cfg),
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _parse_array(txt: str) -> list:
    """从回复里抠出 JSON 数组：容忍 ```json 围栏、前后废话。"""
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", txt.strip())
    a, b = t.find("["), t.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = json.loads(t[a:b + 1])
    except Exception:  # noqa: BLE001
        return []
    return arr if isinstance(arr, list) else []


def llm_wishes(chunk, cfg) -> dict[int, Wish]:
    """一批居民一次请求。任何异常都返回空字典（调用方保留模板愿望）。"""
    lines = "\n".join(_person_line(r) for r in chunk)
    try:
        txt = _post(cfg, [{"role": "system", "content": _SYS},
                          {"role": "user", "content": "请为这些人各写一条愿望：\n" + lines}],
                    max_tokens=max(512, 180 * len(chunk)))
    except Exception:  # noqa: BLE001
        return {}

    out: dict[int, Wish] = {}
    by_rid = {r.rid: r for r in chunk}
    for i, item in enumerate(_parse_array(txt)):
        if not isinstance(item, dict):
            continue
        try:
            rid = int(item.get("rid", i))
        except Exception:  # noqa: BLE001
            continue
        r = by_rid.get(rid)
        if r is None:
            continue
        kind = str(item.get("kind", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        if kind not in WISH_KINDS or not text:
            continue                      # 校验不过 ⇒ 这条留模板
        lo, hi = TARGET_RANGE[kind]
        try:
            target = float(item.get("target", 0))
        except Exception:  # noqa: BLE001
            target = 0.0
        if not (lo * 0.5 <= target <= hi * 2):   # 明显离谱的数值回落到区间中值
            target = round((lo + hi) / 2, 2)
        out[rid] = Wish(kind=kind, text=text[:120], target=target,
                        source="llm", born_tick=0)
    return out


def attach_wishes(residents, llm_cfg, rng: random.Random, tick: int = 0,
                  batch: int = 10, on_batch=None) -> threading.Thread | None:
    """出生时给人手一份模板愿望，然后（若启用）后台线程逐批换成 LLM 版。

    返回后台线程（未启用/无居民时返回 None），调用方可以不等它。
    线程是 daemon：进程退出即随主线程结束，不会拖住服务。
    """
    if not residents:
        return None
    for r in residents:
        r.wish = template_wish(r, rng, tick)
    if not getattr(llm_cfg, "enabled", False):
        return None

    snapshot = list(residents)      # 拷贝：线程里不碰会变的列表

    def _work():
        for i in range(0, len(snapshot), batch):
            chunk = snapshot[i:i + batch]
            got = llm_wishes(chunk, llm_cfg)
            if not got:
                continue                # 这批失败 ⇒ 保留模板，别的批次照跑
            for r in chunk:
                w = got.get(r.rid)
                if w is not None:
                    w.born_tick = tick
                    r.wish = w          # 引用赋值是原子的，主循环读到新旧都自洽
            if on_batch:
                try:
                    on_batch(chunk)
                except Exception:  # noqa: BLE001
                    pass

    th = threading.Thread(target=_work, name="wish-llm", daemon=True)
    th.start()
    return th
