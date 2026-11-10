"""批量抠图：去掉建筑贴图的纯色 / 渐变背景，输出带 alpha 的 PNG。

为什么不是「把所有白色变透明」
    建筑本身常有白墙、白窗。按颜色一刀切会把墙面挖出洞。
    所以这里用**连通性**：只有「与画布边缘相连通」的近似背景色才算背景。

算法（纯 Pillow + numpy + scipy，不联网、不装模型）
    1. 颜色距离（逐通道最大差）≤ tol 的像素记为「背景候选」；
    2. 对候选做连通域标记，只保留**碰到画布四边**的连通域 → 真正的背景；
    3. 用背景区域的中位数反过来校准底色，重复 2~3 轮（应对渐变底）；
    4. tol 不靠猜：从 10 开始逐级加严（10→16→24→34→46→60），
       取第一个「透明占比落在 2%~85%」的档位；
       若最小档位就超过 85%（整张图都像底色，通常是没有干净背景的场景图），
       **直接放弃并报错，不写文件** —— 宁可不动，也不生成一张空白图；
    5. alpha 羽化：对透明度做高斯模糊后重映射，边缘不再是硬锯齿；
    6. 颜色去污：半透明像素的 RGB 用「最近的前景像素」替换，消掉白边。

输出约定
    - 源文件保留，绝不覆盖；
    - 源 .jpg → 输出同名 .png；源本身是 .png → 输出 <stem>_alpha.png；
    - 可选 --update-manifest 把 manifest.json 的 image 指向新 PNG。

用法
    python tools/remove_bg.py                       # 处理全部
    python tools/remove_bg.py --only home_01        # 只处理某一张
    python tools/remove_bg.py --tol 24              # 手动指定阈值（跳过自动试探）
    python tools/remove_bg.py --update-manifest     # 同时改写 manifest.json
    python tools/remove_bg.py --preview sheet.png   # 额外输出对比联系表
    python tools/remove_bg.py --dry-run             # 只统计不写文件
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TOL_LADDER = (10.0, 16.0, 24.0, 34.0, 46.0, 60.0)   # 逐级放宽
MAX_FRAC = 0.85          # 整图透明占比上限：超过说明把建筑主体也吃掉了
MIN_BORDER = 0.90        # 四边透明率达到这个值，才算「背景真的清干净」


# --------------------------------------------------------------------------- #
# 背景判定
# --------------------------------------------------------------------------- #
def estimate_bg(arr: np.ndarray, border: int = 3) -> np.ndarray:
    """用最外一圈像素的中位数估计底色（对局部杂色不敏感）。"""
    parts = [arr[:border].reshape(-1, arr.shape[2]),
             arr[-border:].reshape(-1, arr.shape[2]),
             arr[:, :border].reshape(-1, arr.shape[2]),
             arr[:, -border:].reshape(-1, arr.shape[2])]
    edge = np.concatenate(parts, axis=0).astype(np.float32)
    return np.median(edge, axis=0)


def bg_candidates(arr: np.ndarray, bg: np.ndarray, tol: float) -> np.ndarray:
    """逐通道最大差 ≤ tol 视为「像背景」。"""
    diff = np.abs(arr.astype(np.float32) - bg).max(axis=2)
    return diff <= tol


def edge_connected(mask: np.ndarray) -> np.ndarray:
    """只保留与画布四边连通的掩码区域。"""
    lab, n = ndimage.label(mask)
    if n == 0:
        return np.zeros_like(mask, dtype=bool)
    labels = set(np.unique(lab[0, :])) | set(np.unique(lab[-1, :]))
    labels |= set(np.unique(lab[:, 0])) | set(np.unique(lab[:, -1]))
    labels.discard(0)
    if not labels:
        return np.zeros_like(mask, dtype=bool)
    return np.isin(lab, list(labels))


def build_bg_mask(arr: np.ndarray, tol: float, passes: int) -> tuple[np.ndarray, np.ndarray]:
    """给定阈值求背景掩码；用已判定背景的颜色反向校准底色，兼容渐变底。"""
    bg = estimate_bg(arr)
    mask = np.zeros(arr.shape[:2], dtype=bool)
    for _ in range(max(1, passes)):
        mask = edge_connected(bg_candidates(arr, bg, tol))
        if not mask.any():
            break
        new_bg = np.median(arr[mask], axis=0).astype(np.float32)
        if float(np.abs(new_bg - bg).max()) < 1.5:
            bg = new_bg
            break
        bg = new_bg
    return mask, bg


def border_transparency(mask: np.ndarray, border: int = 2) -> float:
    """四条最外边的透明率 —— 背景真正抠干净时，它应接近 1。"""
    b = np.concatenate([mask[:border].ravel(), mask[-border:].ravel(),
                        mask[:, :border].ravel(), mask[:, -border:].ravel()])
    return float(b.mean())


def _kmeans_colors(px: np.ndarray, k: int = 3, iters: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """对一小撮像素做极简 k-means（逐通道最大差当距离），返回 (中心, 各簇占比)。

    只为「外圈到底有几种底色」服务：采样上限 2 万像素，避免大图上白跑。
    """
    px = px.astype(np.float32)
    step = max(1, len(px) // 20000)
    smp = px[::step]
    c = smp[np.linspace(0, len(smp) - 1, k).astype(int)].copy()
    lab = np.zeros(len(smp), dtype=int)
    for _ in range(iters):
        d = np.stack([np.abs(smp - ci).max(axis=1) for ci in c], axis=1)
        lab = d.argmin(axis=1)
        for j in range(k):
            m = lab == j
            if m.any():
                c[j] = np.median(smp[m], axis=0)
    cnt = np.bincount(lab, minlength=k).astype(np.float32)
    return c, cnt / max(1.0, float(cnt.sum()))


def checker_centers(arr: np.ndarray, border: int = 6) -> list[np.ndarray] | None:
    """检测「棋盘格假透明」：外圈由两种面积相当、明显不同的颜色交替铺成。

    这类图原本是透明 png，被存成 jpg / 截图后，软件用来表示透明的格子
    **变成了真像素**（白 + 浅灰，或被调过色的米白 + 深棕）。
    颜色键只认一种底色，所以永远抠不动 —— 必须两色一起当背景。

    返回 [色A, 色B]；不像棋盘格（单色底 / 渐变底）就返回 None。
    """
    parts = [arr[:border].reshape(-1, 3), arr[-border:].reshape(-1, 3),
             arr[:, :border].reshape(-1, 3), arr[:, -border:].reshape(-1, 3)]
    edge = np.concatenate(parts, axis=0)
    centers, fracs = _kmeans_colors(edge, k=3)
    order = np.argsort(-fracs)
    c1, c2 = centers[order[0]], centers[order[1]]
    if fracs[order[1]] < 0.12:                    # 第二种色只是零星噪点
        return None
    if float(np.abs(c1 - c2).max()) < 40.0:       # 两色几乎重合 = 单色底
        return None
    return [c1, c2]


def bg_candidates_multi(arr: np.ndarray, centers: list[np.ndarray], tol: float) -> np.ndarray:
    """多底色版色键：离**任意一个**底色足够近，就算背景候选。"""
    d = np.stack([np.abs(arr.astype(np.float32) - c).max(axis=2) for c in centers], axis=0)
    return d.min(axis=0) <= tol


def choose_mask(arr: np.ndarray, tol: float | None, passes: int, checker: str = "auto"):
    """自动挑阈值：取「四边清得最干净、又没把整张图吃掉」的那一档。

    判据用**四边透明率**而不是整图透明占比：
    满幅插画可能只有几个点的透明，四边却一块都没清 —— 整图占比会把这种情况误判为成功。

    checker: auto=单色抠不动时再试棋盘格（默认）；on=直接按棋盘格；off=只走单色。
    返回 (mask, bg, tol, 四边透明率)。
    """
    # 棋盘格的两种底色（不是棋盘格则为 None）
    cs = None if checker == "off" else checker_centers(arr)

    def multi_sweep(base_bt: float):
        """在 TOL_LADDER 上找「四边最干净且没吃掉整图」的双色掩码。"""
        out = None
        for t in TOL_LADDER:
            m = edge_connected(bg_candidates_multi(arr, cs, t))
            if float(m.mean()) > MAX_FRAC:        # 整张图都像底色，再放宽更糟
                break
            bt = border_transparency(m)
            if out is None or bt >= out[3]:
                out = (m, cs[0], t, bt)
            if bt >= MIN_BORDER:                  # 已经够干净，不必继续放宽
                break
        return out

    if tol is not None:
        mask, bg = build_bg_mask(arr, tol, passes)
        bt = border_transparency(mask)
        if cs is not None:
            m2 = edge_connected(bg_candidates_multi(arr, cs, tol))
            b2 = border_transparency(m2)
            if b2 > bt:
                return m2, cs[0], float(tol), b2
        return mask, bg, float(tol), bt

    best = None
    for t in TOL_LADDER:
        mask, bg = build_bg_mask(arr, t, passes)
        frac = float(mask.mean())
        if frac > MAX_FRAC:            # 已经把整张图吃掉，再加大更糟
            if best is None:
                best = (mask, bg, t, border_transparency(mask))
            break
        bt = border_transparency(mask)
        if best is None or bt >= best[3]:
            best = (mask, bg, t, bt)
    assert best is not None

    if cs is not None and (checker == "on" or best[3] < MIN_BORDER):
        alt = multi_sweep(best[3])
        if alt is not None and alt[3] > best[3]:
            best = alt
    return best


# --------------------------------------------------------------------------- #
# 手动模式：人涂在哪儿，哪儿就是背景
# --------------------------------------------------------------------------- #
MANUAL_TOL = 32     # 手动模式默认容差（比自动宽：手涂有误差，且要盖住棋盘格的两种灰）


def _strokes_to_seeds(strokes: list[dict], w: int, h: int,
                      dense: bool = False) -> list[tuple[int, int]]:
    """把前端涂点（归一化 fx/fy + 半径比例 r）栅格化成像素种子。

    前端一次拖动会送来上百个点，这里按半径铺满圆圈并去重 —— 直接返回
    一圈圈的圆心会让「涂粗笔」只剩细线，边缘反而抠不干净。
    """
    pts: set[tuple[int, int]] = set()
    for s in strokes:
        try:
            cx = int(round(float(s["fx"]) * (w - 1)))
            cy = int(round(float(s["fy"]) * (h - 1)))
            rad = max(1, int(round(float(s.get("r", 0.012)) * w)))
        except (KeyError, TypeError, ValueError):
            continue
        step = 1 if dense else max(1, rad // 3)   # dense：实心铺满，圆内不留孔洞
        for dy in range(-rad, rad + 1, step):
            for dx in range(-rad, rad + 1, step):
                if dx * dx + dy * dy <= rad * rad:
                    x, y = cx + dx, cy + dy
                    if 0 <= x < w and 0 <= y < h:
                        pts.add((x, y))
    return sorted(pts)


def _seed_ref_colors(px: np.ndarray, tol: float, max_refs: int = 8) -> list[np.ndarray]:
    """把「人涂到的像素」的颜色归并成少量参考色（生长用）。

    与 `_kmeans_colors` 的分工：那边的输入是画布外圈（自动猜底色），
    这边是涂抹像素本身。贪心归并更贴近"我到底涂到了哪些颜色" ——
    细笔画的字色必须留在参考色里，不能被大面积底色挤掉。
    """
    if px.size == 0:
        return []
    q = (px.astype(np.int16) // 8) * 8            # 8 级量化：压掉 jpg 噪点
    uniq, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1]              # 大面积色优先
    thr = max(float(tol), 12.0)                   # 略宽于 tol：同一种"黑"有抗锯齿抖动

    groups: list[list] = []                       # [[加权颜色和, 权重], ...]
    for idx in order:
        c = uniq[idx].astype(np.float32)
        wt = float(counts[idx])
        for g in groups:
            if float(np.abs(c - g[0] / g[1]).max()) <= thr:
                g[0] = g[0] + c * wt
                g[1] += wt
                break
        else:
            groups.append([c * wt, wt])
    groups.sort(key=lambda g: g[1], reverse=True)
    return [g[0] / g[1] for g in groups[:max_refs]]


def manual_mask(arr: np.ndarray, seeds: list[tuple[int, int]], tol: float,
                max_refs: int = 8, grow: bool = True) -> np.ndarray:
    """按用户涂抹的种子点求背景掩码。

    自动模式靠「与画布四边连通」猜背景，遇到满幅插画（房子占满画面、
    四边都是草地）就失灵，或者只能啃掉边缘一圈。手动模式改由人指定。

    两种口径，由 grow 决定：

    - grow=True（相似性涂抹）：从涂抹点按颜色向外生长。
        1. 参考色取**涂抹像素自身的颜色**（贪心聚类成 ≤ max_refs 簇）。
           ⚠ 不能用「7×7 邻域中位数」：细笔画的字/描边只有 1~3 像素宽，
           邻域里绝大部分是底色，中位数会算出白底 —— 于是长的是白底，
           笔画本身被当成前景留下，表现就是「涂了没反应、字还在」。
        2. 与任一参考色逐通道差 ≤ tol 的像素记为候选；
        3. 连通域标记后，只保留包含种子点的连通域；
        4. **最后并上种子像素本身**：涂过的地方无条件删除（细笔画兜底）。
      代价：如果被涂的那片颜色恰好和墙面同族且连在一起，会顺墙蔓延。
      那是「相似」的语义，不是 bug —— 想要准确就关掉 grow。

    - grow=False（具体涂抹）：只删笔刷真正覆盖到的像素，颜色一概不看、
      不与邻居生长。**物理上不可能蔓延**，代价是要多涂几笔。
    """
    h, w = arr.shape[:2]
    seed_mask = np.zeros((h, w), dtype=bool)
    if not seeds:
        return seed_mask
    seed_mask[np.array([y for _x, y in seeds]),
              np.array([x for x, _y in seeds])] = True

    if not grow:
        return seed_mask

    f = arr.astype(np.float32)
    px = np.array([f[y, x] for (x, y) in seeds], dtype=np.float32)
    refs = _seed_ref_colors(px, tol, max_refs)

    similar = np.zeros((h, w), dtype=bool)
    for ref in refs:
        similar |= np.abs(f - ref).max(axis=2) <= tol
    similar |= seed_mask                  # 涂过的地方一定算背景

    lab, n = ndimage.label(similar)
    if n == 0:
        return seed_mask
    labels = {int(lab[y, x]) for (x, y) in seeds if lab[y, x] > 0}
    if not labels:
        return seed_mask
    return np.isin(lab, list(labels)) | seed_mask



# --------------------------------------------------------------------------- #
# 边缘处理
# --------------------------------------------------------------------------- #
def feather_alpha(mask: np.ndarray, sigma: float) -> np.ndarray:
    """背景=0 前景=1，轻微高斯羽化后重映射，保留边缘锐度。"""
    a = np.where(mask, 0.0, 1.0).astype(np.float32)
    if sigma <= 0:
        return a
    a = ndimage.gaussian_filter(a, sigma=sigma)
    return np.clip((a - 0.18) / 0.64, 0.0, 1.0)


def decontaminate(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """半透明像素的 RGB 用最近的前景像素替换 —— 否则边缘残留背景白边。"""
    fg = alpha > 0.02
    if not fg.any():
        return rgb
    idx = ndimage.distance_transform_edt(~fg, return_distances=False, return_indices=True)
    near = rgb[idx[0], idx[1]]
    out = rgb.copy()
    out[(alpha > 0.02) & (alpha < 0.985)] = near[(alpha > 0.02) & (alpha < 0.985)]
    return out


# 历次抠图产物加过的后缀。命名策略换过一次，这里都要能剥掉。
_OUT_SUFFIXES = ("_alpha", "_cutout", "_cut2", "_cut")


def _base_stem(stem: str) -> str:
    """剥掉抠图产物后缀，回到原始名（home_01_cut_cut2 → home_01）。"""
    changed = True
    while changed:
        changed = False
        for suf in _OUT_SUFFIXES:
            if stem.endswith(suf) and len(stem) > len(suf):
                stem = stem[: -len(suf)]
                changed = True
                break
    return stem


def out_path_for(src: pathlib.Path) -> pathlib.Path:
    """算出这次抠图写到哪个文件 —— **返回值绝不会等于源文件**。

    为什么不能同名覆盖（对 x_alpha.png 再抠又吐回 x_alpha.png）：
      1. 一次写坏，上一版产物就没了，没法重来；
      2. 浏览器按 URL 缓存图片：文件名不变时，画布和缩略图拿到的还是旧图，
         表现就是「又抠一遍画面没反应」「刚抠掉的那块又冒出来」「怎么涂都抠不掉」。
    所以源是 png 时在 <原名>_cut.png / <原名>_cut2.png 之间轮换，每次都换 URL。
    """
    base = _base_stem(src.stem)
    if src.suffix.lower() != ".png":
        return src.with_name(base + ".png")      # jpg -> 同名 png，天然不同名
    # 源是 png：依次找第一个没被占用的 _cut / _cut2 / _cut3 ...
    # 为什么不再固定轮换 _cut 与 _cut2：第三次起两者都已存在，会反复覆盖同一个
    # 文件，浏览器缓存 + 编辑器回看都会拿旧图，表现就是「抠了没反应」。
    for i in range(1, 100):
        name = base + ("_cut" if i == 1 else f"_cut{i}")
        cand = src.with_name(name + ".png")
        if not cand.exists() and cand.name != src.name:
            return cand
    return src.with_name(base + "_cut99.png")


def process_one(src: pathlib.Path, dst: pathlib.Path, tol: float | None,
                feather: float, passes: int, dry_run: bool,
                checker: str = "auto",
                strokes: list[dict] | None = None,
                grow: bool = True) -> dict:
    _im = Image.open(src)
    im = _im.convert("RGB")
    # 源图本身已带透明（对抠过的 PNG 再抠）时必须保留原有透明：convert("RGB")
    # 会把已抠掉的部分填实，结果是「越抠越白、白忙一场」。
    orig_alpha = None
    if _im.mode in ("RGBA", "LA", "PA") or (_im.mode == "P" and "transparency" in _im.info):
        orig_alpha = np.array(_im.convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
    arr = np.array(im, dtype=np.uint8)

    if strokes:
        # 手动模式：人涂在哪儿，哪儿就是背景 —— 不再靠四边连通性猜。
        # ok 也不再看「四边透明率」，只要真的圈出了区域就算成功。
        tol_used = float(tol) if tol else MANUAL_TOL
        seeds = _strokes_to_seeds(strokes, im.width, im.height, dense=not grow)
        mask = manual_mask(arr, seeds, tol_used, grow=grow)
        bg = estimate_bg(arr)
        bt = None
        ok = bool(mask.any())
        mode = "manual"
    else:
        mask, bg, tol_used, bt = choose_mask(arr, tol, passes, checker)
        ok = bt >= MIN_BORDER
        mode = "auto"

    # 手动涂抹是「点哪删哪」的精确活，不做羽化：羽化会把细笔画的小字打成
    # 半透明残影（alpha 0.3~0.6），肉眼看还是「字还在」，其实只是变淡了。
    alpha = feather_alpha(mask, 0.0 if strokes else feather)
    if orig_alpha is not None:
        alpha = np.minimum(alpha, orig_alpha)
    rgb = decontaminate(arr, alpha)

    total = mask.size
    info = {
        "file": str(src.name),
        "size": f"{im.width}x{im.height}",
        "bg": tuple(int(v) for v in bg),
        "tol": tol_used,
        "border": bt,
        "transparent": float((alpha < 0.02).sum()) / total,
        "semi": float(((alpha >= 0.02) & (alpha < 0.985)).sum()) / total,
        "mode": mode,
        "ok": ok,
    }
    if ok and not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.dstack([rgb, np.round(alpha * 255).astype(np.uint8)]), "RGBA") \
             .save(dst, "PNG", optimize=True)
    return info


# --------------------------------------------------------------------------- #
# 联系表：棋盘格底，透明处一眼可见（人工检查用）
# --------------------------------------------------------------------------- #
def make_preview(pairs: list[tuple[pathlib.Path, pathlib.Path]], out_path: pathlib.Path,
                 cell: int = 180, cols: int = 6) -> None:
    rows = (len(pairs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (40, 44, 50))
    tile = 12
    for y in range(0, sheet.height, tile):
        for x in range(0, sheet.width, tile):
            if (x // tile + y // tile) % 2 == 0:
                sheet.paste((70, 74, 80), (x, y, min(x + tile, sheet.width), min(y + tile, sheet.height)))

    for i, (src, dst) in enumerate(pairs):
        img = Image.open(dst).convert("RGBA")
        img.thumbnail((cell - 10, cell - 10))
        ox = (i % cols) * cell + (cell - img.width) // 2
        oy = (i // cols) * cell + (cell - img.height) // 2
        sheet.paste(img, (ox, oy), img)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "PNG")
    print(f"联系表 → {out_path}")


# --------------------------------------------------------------------------- #
# manifest 改写
# --------------------------------------------------------------------------- #
def update_manifest(root: pathlib.Path, changed: dict[str, str]) -> int:
    """把 manifest.json 里指向旧文件的路径换成新 PNG。

    注意本项目 manifest 是 `{"kinds": {"home": ["home/home_01.jpg", ...]}}` 这种
    **列表**结构，没有 `image` 字段 —— 所以不认死键名，凡是字符串值命中就替换。
    """
    mf = root / "manifest.json"
    if not mf.exists() or not changed:
        return 0
    data = json.loads(mf.read_text(encoding="utf-8"))
    n = 0

    def walk(node):
        nonlocal n
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and node in changed:
            n += 1
            return changed[node]
        return node

    data = walk(data)
    mf.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return n


# --------------------------------------------------------------------------- #
def collect_sources(root: pathlib.Path, only: str | None) -> list[pathlib.Path]:
    """同一张图只取一个源：有 .jpg 就别拿上次生成的 .png 再处理一遍。"""
    by_stem: dict[str, pathlib.Path] = {}
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in EXTS:
            continue
        if only and only not in p.name:
            continue
        key = str(p.with_suffix("").relative_to(root)).replace("\\", "/")
        cur = by_stem.get(key)
        if cur is None or (cur.suffix.lower() == ".png" and p.suffix.lower() != ".png"):
            by_stem[key] = p
    # 丢掉上一次生成的 _alpha.png（那是产物，不是源）
    return sorted(
        p for p in by_stem.values()
        if not any(p.stem.endswith(s) for s in _OUT_SUFFIXES)  # 产物不当源
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="建筑贴图批量抠图（连通性色键）")
    ap.add_argument("--root", default="web/assets/buildings", help="贴图根目录")
    ap.add_argument("--tol", type=float, default=None, help="手动阈值（默认自动试探 10→60）")
    ap.add_argument("--feather", type=float, default=0.8, help="alpha 羽化半径，0=关闭")
    ap.add_argument("--passes", type=int, default=2, help="背景色校准轮数")
    ap.add_argument("--only", default=None, help="只处理文件名包含该串的图")
    ap.add_argument("--update-manifest", action="store_true", help="改写 manifest.json 指向新 PNG")
    ap.add_argument("--preview", default=None, help="输出对比联系表到该路径")
    ap.add_argument("--checker", choices=("auto", "on", "off"), default="auto",
                    help="棋盘格假透明：auto=单色抠不动时自动启用（默认）/ on=强制走 / off=关闭")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root)
    if not root.exists():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 2

    srcs = collect_sources(root, args.only)
    if not srcs:
        print("没有找到图片", file=sys.stderr)
        return 1

    changed: dict[str, str] = {}
    pairs: list[tuple[pathlib.Path, pathlib.Path]] = []
    failed: list[str] = []
    for src in srcs:
        try:
            dst = out_path_for(src)
            info = process_one(src, dst, args.tol, args.feather, args.passes,
                               args.dry_run, args.checker)
        except Exception as e:                       # 单张失败不影响整批
            print(f"✗ {src.name}: {e}", file=sys.stderr)
            failed.append(src.name)
            continue

        mark = "✓" if info["ok"] else "✗"
        print(f"{mark} {info['file']:24s} {info['size']:10s} 底={str(info['bg']):16s} "
              f"tol={info['tol']:4.0f} 四边={info['border']:5.0%} "
              f"透明={info['transparent']:6.2%} 半透明={info['semi']:5.2%}")
        if info["ok"]:
            pairs.append((src, dst))
            if dst.name != src.name:
                changed[str(src.relative_to(root)).replace("\\", "/")] = \
                    str(dst.relative_to(root)).replace("\\", "/")
        else:
            failed.append(src.name)

    ok_n = len(pairs)
    print(f"\n成功 {ok_n}/{len(srcs)} 张"
          + ("（dry-run，未写文件）" if args.dry_run else ""))
    if failed:
        print(f"跳过 {len(failed)} 张（底色与主体无法区分，未生成文件）：")
        for f in failed:
            print(f"  - {f}")

    if args.preview and pairs:
        make_preview(pairs, pathlib.Path(args.preview))

    if args.update_manifest and changed:
        print(f"manifest.json 更新 {update_manifest(root, changed)} 条路径")

    return 0 if ok_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
