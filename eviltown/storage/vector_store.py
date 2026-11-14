"""长期记忆的向量检索：sqlite-vec → 纯 Python 余弦 → 关键词，**三级降级**。

为什么要有三级：
- **sqlite-vec**（可选依赖）：真做 ANN，量大时才划算；
- **纯 Python 余弦**：有 embedding 但没装 sqlite-vec 时的兜底 —— 小镇记忆在
  几千条量级，一次遍历毫秒级，够用；
- **关键词**：连 embedding 都拿不到（没配密钥 / 模型不认 /v1/embeddings / 断网）
  时，按中英混合分词后的词面重合度排序。**永远有结果，绝不抛异常。**

红线（沿用 AGENTS.md「LLM 不进主循环」）：本模块**不调用 LLM**。embedding 由
调用方算好传进来（可能为 None），所以存储层能在完全离线的测试里跑。

表结构见 storage/db.py 的 `memory_events`；本模块只读写那一张表。
"""
from __future__ import annotations

import math
import re
import sqlite3
from array import array
from dataclasses import dataclass

# 英文/数字整词，或单个汉字 —— 中文没有分词库也能靠「单字 + 2-gram」凑出词感。
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

# 高频虚词：不参与词面重合，否则「的了/and/the」能把任何两句话凑成相关。
_STOP = frozenset(
    "的 了 就 和 与 也 在 是 我 你 他 她 它 们 这 那 有 没 很 又 都 还 把 被 给 到 上 下 "
    "a an the and or of to in on at is are was were for with".split()
)

# 全表扫描上限：记忆是「最近的记得清楚」的东西，超过这个量就没必要全看。
_SCAN_LIMIT = 4000


def tokenize(text: str) -> list[str]:
    """中英混合分词：英文/数字整词小写；中文单字 + 相邻 2-gram（保留词频）。"""
    raw = _TOKEN_RE.findall((text or "").lower())
    out: list[str] = []
    prev_cjk: str | None = None
    for t in raw:
        out.append(t)                      # 单字/整词本身也算
        if len(t) == 1 and not t.isascii():  # 中文：与上一个汉字拼 2-gram
            if prev_cjk is not None:
                out.append(prev_cjk + t)
            prev_cjk = t
        else:
            prev_cjk = None
    return [t for t in out if t not in _STOP]


def keyword_score(query: str, text: str) -> float:
    """词面重合度（0..1）：交集/√(查询长度)，再按命中密度微调。"""
    q = set(tokenize(query))
    if not q:
        return 0.0
    t = tokenize(text)
    if not t:
        return 0.0
    hit = q & set(t)
    if not hit:
        return 0.0
    # 长文本天然更容易撞词，用 √len 归一化；再叠一点「命中占比」，短清单更靠前。
    density = len(hit) / max(1.0, math.sqrt(len(t)))
    return min(1.0, len(hit) / math.sqrt(len(q)) * 0.8 + density * 0.2)


def to_blob(vec) -> bytes:
    """float 列表 → float32 小端字节串（x86/ARM 均为小端，故不显式指定顺序）。"""
    return array("f", [float(x) for x in vec]).tobytes()


def from_blob(blob: bytes | None) -> list[float]:
    """float32 字节串 → list[float]；坏数据返回空列表（调用方按「没有 embedding」处理）。"""
    if not blob:
        return []
    try:
        a = array("f")
        a.frombytes(blob)
        return a.tolist()
    except Exception:  # noqa: BLE001  长度不是 4 的倍数等
        return []


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；任一侧为空/维度不一致/零向量 → 0.0（不抛异常）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


@dataclass
class Hit:
    """一条被召回的记忆。score 已归一化到 0..1，越高越相关。"""

    id: int
    day: int
    tick: int
    kind: str
    text: str
    actor: int | None
    place: str | None
    weight: float
    score: float

    def as_dict(self) -> dict:
        return {
            "id": self.id, "day": self.day, "tick": self.tick, "kind": self.kind,
            "text": self.text, "actor": self.actor, "place": self.place,
            "weight": self.weight, "score": round(self.score, 4),
        }


class VectorStore:
    """`memory_events` 的读写门面。三级后端在构造时探测一次，之后固定。

    后端属性 `backend` ∈ {"vec", "cosine", "keyword"}，供测试与诊断断言 ——
    没有 sqlite-vec / 没有 embedding 的环境必须是 "keyword"，行为可预期。
    """

    def __init__(self, con: sqlite3.Connection, *, use_vec: bool = True,
                 dim: int | None = None):
        self.con = con
        self.dim = int(dim) if dim else None
        self._use_vec = bool(use_vec)
        self.backend = "keyword"
        self.vec_note = ""
        if self._use_vec and self.dim:
            self.backend = self._try_vec(self.dim) or "keyword"

    def ensure_dim(self, dim: int) -> None:
        """首次拿到 embedding 时才定维度：定完顺手试着把后端升级到 sqlite-vec。

        为什么要延迟：embedding 模型决定维度（如 1024），配置里不该写死一个
        数字 —— 写死了换模型就静默错。维度已知才能建 vec0 虚拟表。
        """
        if dim <= 0 or self.dim == dim:
            return
        self.dim = int(dim)
        if self._use_vec and self.backend == "keyword":
            self.backend = self._try_vec(self.dim) or "keyword"

    # ------------------------------------------------------------ 后端探测
    def _try_vec(self, dim: int) -> str | None:
        """尝试加载 sqlite-vec 并建虚拟表。任何一步失败都安静退回 None。"""
        try:
            import sqlite_vec  # type: ignore  # 可选依赖，没装是常态
        except Exception:  # noqa: BLE001
            self.vec_note = "sqlite-vec 未安装"
            return None
        try:
            self.con.enable_load_extension(True)
            sqlite_vec.load(self.con)
        except Exception as e:  # noqa: BLE001  发行版 python 可能禁用了 load_extension
            self.vec_note = f"加载 sqlite-vec 失败：{e}"
            return None
        try:
            self.con.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec "
                f"USING vec0(embedding float[{dim}])")
            return "vec"
        except Exception as e:  # noqa: BLE001
            self.vec_note = f"建 memory_vec 虚拟表失败：{e}"
            return None

    # ------------------------------------------------------------ 写入
    def add(self, *, day: int, tick: int, kind: str, text: str,
            actor: int | None = None, target: int | None = None,
            place: str | None = None, weight: float = 1.0,
            embedding: list[float] | None = None) -> int:
        """写一条记忆，返回 memory_events.id。text 为空直接返回 -1（不写噪声）。"""
        text = (text or "").strip()
        if not text:
            return -1
        blob = to_blob(embedding) if embedding else None
        if embedding:
            self.ensure_dim(len(embedding))   # 维度由模型决定，见 ensure_dim
        cur = self.con.execute(
            "INSERT INTO memory_events (day, tick, kind, text, actor, target, "
            "place, weight, embedding) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(day), int(tick), kind, text, actor, target, place,
             float(weight), blob))
        mid = int(cur.lastrowid)
        if self.backend == "vec" and blob is not None:
            try:
                self.con.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (mid, sqlite_vec_serialize(embedding)))
            except Exception:  # noqa: BLE001  ANN 索引写失败不该拖垮主线
                self.backend = "cosine"
                self.vec_note = "写 memory_vec 失败，已退回纯 Python 余弦"
        return mid

    # ------------------------------------------------------------ 检索
    def search(self, query: str, *, query_vec: list[float] | None = None,
               k: int = 5, actor: int | None = None,
               since_day: int | None = None,
               scan_limit: int = _SCAN_LIMIT) -> list[Hit]:
        """按相关度取前 k 条。任何后端失败都逐级降级，最差也能给关键词结果。"""
        rows = self._rows(actor=actor, since_day=since_day, scan_limit=scan_limit)
        if not rows:
            return []
        if query_vec and self.backend == "vec":
            hits = self._search_vec(query_vec, rows, k)
            if hits is not None:
                return hits
            self.backend = "cosine"     # 一次性退回，后续调用不再试
        if query_vec and self.backend == "cosine":
            return self._search_cosine(query_vec, rows, k)
        return self._search_keyword(query, rows, k)

    def _rows(self, *, actor: int | None, since_day: int | None,
              scan_limit: int) -> list[tuple]:
        sql = ("SELECT id, day, tick, kind, text, actor, place, weight, embedding "
               "FROM memory_events")
        where, params = [], []
        if actor is not None:
            where.append("(actor IS NULL OR actor = ?)")
            params.append(int(actor))
        if since_day is not None:
            where.append("day >= ?")
            params.append(int(since_day))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY day DESC, id DESC LIMIT ?"
        params.append(int(scan_limit))
        return self.con.execute(sql, params).fetchall()

    @staticmethod
    def _mk(row, score: float) -> Hit:
        return Hit(id=row[0], day=row[1] or 0, tick=row[2] or 0, kind=row[3] or "",
                   text=row[4] or "", actor=row[5], place=row[6],
                   weight=float(row[7] or 1.0), score=float(score))

    def _search_cosine(self, query_vec: list[float], rows: list[tuple],
                       k: int) -> list[Hit]:
        scored = []
        for row in rows:
            sim = cosine(query_vec, from_blob(row[8]))
            if sim > 0.0:
                scored.append(self._mk(row, sim))
        # 余弦可能是负数（夹角 > 90°），这里只留正相关，0 分不占位。
        scored.sort(key=lambda h: (h.score, h.weight), reverse=True)
        return scored[:k]

    def _search_keyword(self, query: str, rows: list[tuple], k: int) -> list[Hit]:
        scored = []
        for row in rows:
            s = keyword_score(query, row[4] or "")
            if s > 0.0:
                scored.append(self._mk(row, s))
        scored.sort(key=lambda h: (h.score, h.weight, h.day), reverse=True)
        return scored[:k]

    def _search_vec(self, query_vec: list[float], rows: list[tuple],
                    k: int) -> list[Hit] | None:
        """sqlite-vec 的 MATCH 检索；语法/版本不兼容时返回 None 让上层降级。"""
        try:
            found = self.con.execute(
                "SELECT rowid, distance FROM memory_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (sqlite_vec_serialize(query_vec), int(k) * 4)).fetchall()
        except Exception:  # noqa: BLE001
            return None
        if not found:
            return None
        dist = {int(r[0]): float(r[1]) for r in found}
        lo = min(dist.values())
        hi = max(dist.values())
        span = (hi - lo) or 1.0
        out = []
        for row in rows:
            d = dist.get(int(row[0]))
            if d is None:
                continue
            # 距离越小越像：min-max 反归一化到 0..1，和另外两级同口径。
            out.append(self._mk(row, 1.0 - (d - lo) / span))
        out.sort(key=lambda h: h.score, reverse=True)
        return out[:k]

    # ------------------------------------------------------------ 诊断
    def stats(self) -> dict:
        n = self.con.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        return {"backend": self.backend, "rows": int(n), "dim": self.dim,
                "note": self.vec_note}


def sqlite_vec_serialize(vec) -> bytes:
    """sqlite-vec 要的序列化格式（float32 小端）。延迟导入，没装也不影响关键词路径。"""
    try:
        from sqlite_vec import serialize_float32  # type: ignore
        return serialize_float32([float(x) for x in vec])
    except Exception:  # noqa: BLE001
        return to_blob(vec)
