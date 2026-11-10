"""服务端：仿真在后台线程跑，浏览器只是看客。

- REST：/api/state 快照、/api/map 静态地图、/api/control 控制、/api/resident/{id} 个体详情、
  /api/building/{bid}/hours 设置建筑营业时间（热更新）
- WebSocket：/ws 定时推帧（仿真速度与推帧频率解耦）
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from eviltown.agents.sim import Simulation
from eviltown.config import Config, load_config
from eviltown.world.grid import Terrain

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

# 1× 时每帧推进多少 tick。20 帧/秒 ⇒ 0.25 就是每秒 5 个仿真分钟（一天约 4.8 分钟）。
# 旧实现每帧固定推 1 tick ⇒ 1× 其实是 20 分钟/秒、一天 72 秒就被跑完，快到看不清。
SPEED_TICKS_PER_FRAME = 0.25


class Runner:
    """把仿真放进后台线程，主线程只做 IO。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sim = Simulation(cfg)
        self.lock = threading.Lock()
        self.speed = float(cfg.time.speed)
        self.paused = False
        self._acc = 0.0          # 浮点倍率的余量：0.3× 时每帧攒 0.3 tick，攒满才推进
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        import time

        while not self._stop.is_set():
            with self.lock:
                if not self.paused:
                    # 倍率可以是小数：「无限倍速」(<=0) 仍走每帧预算上限
                    sp = (self.speed if self.speed > 0
                          else float(self.cfg.time.max_ticks_per_frame))
                    self._acc += sp * SPEED_TICKS_PER_FRAME
                    n = int(self._acc)      # 攒不够 1 tick 就跳过（慢放靠这个实现）
                    self._acc -= n
                    if n > 0:
                        self.sim.tick(n)
            # 速度越高，每帧让出的时间越少
            time.sleep(0.02 if self.speed >= 20 else 0.05)

    def set_speed(self, s: float) -> None:
        with self.lock:
            self.speed = float(s)
            self._acc = 0.0          # 换挡时丢掉余量，免得慢放切快放时攒着一堆 tick 突进
            self.sim.cfg.time.speed = self.speed

    def step(self, n: int = 1) -> None:
        with self.lock:
            self.sim.tick(n)

    def set_paused(self, p: bool) -> None:
        self.paused = p
        with self.lock:
            self.sim.paused = p

    def snapshot(self) -> dict:
        with self.lock:
            return self.sim.snapshot()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sim.close()
        except Exception:
            pass


runner: Runner | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner
    runner = Runner(load_config())
    runner.start()
    try:
        yield
    finally:
        runner.stop()


app = FastAPI(title="EvilTown", lifespan=lifespan)
if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
async def index():
    f = WEB / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return JSONResponse({"error": "web/index.html 缺失"}, status_code=500)


@app.get("/api/state")
async def state():
    return runner.snapshot()


def _points_path() -> "Path":
    """点位文件的实际路径：优先 config.yaml 的 furniture.points_path，否则 points.json。

    注：早先这里直接引用模块级 `cfg`，但 `cfg` 只是 lifespan 里的局部变量，
    每 次调用都抛 NameError 被 except 吞掉 → 配置项形同虚设，永远回落 points.json。
    """
    from eviltown.config import load_config

    path = Path("points.json")
    try:
        path = Path(load_config().furniture.points_path)
    except Exception:
        pass
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


@app.get("/api/furniture-points")
async def furniture_points():
    """建筑点位文件（编辑器启动时回显已有点位）。

    读不到就返回空 kinds —— 编辑器照常能用，只是从空白开始摆。
    """
    import json

    try:
        return json.loads(_points_path().read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "kinds": {}}


@app.post("/api/furniture-points")
async def save_furniture_points(payload: dict):
    """把编辑器摆好的点位写回 points.json —— 省掉「下载 → 手动搬文件」那一步。

    安全约束：
    - 必须带 kinds，防止空 payload 把已有配置冲掉；
    - 写前把旧文件备份成 points.json.bak（只留一份，够回滚即可）；
    - 路径不接受客户端指定，只用服务端解析出的那一个文件。

    保存后**立即热应用**（`Simulation.reload_furniture`）：只重绑家具与床位，
    不重建仿真 —— 居民、关系、时钟、事件全部保留，无需重启。
    """
    import json

    if not isinstance(payload.get("kinds"), dict):
        return JSONResponse({"ok": False, "error": "payload 缺少 kinds 字段"},
                            status_code=400)

    path = _points_path()
    try:
        if path.exists():
            path.with_suffix(".json.bak").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    applied = None
    if runner is not None:
        # ⚠️ 这里必须传 `load_points()` 摊平后的 {业态: [点位, ...]}。
        # payload["kinds"] 是编辑器格式 {业态: {image, grid, points}}（多套了一层），
        # bind_furniture 拿到嵌套 dict 后会在 `for q in pts` 里迭代字符串键，
        # 随即 `q.get()` 抛 AttributeError → 500 → 前端误报「保存失败」，
        # 而文件其实早已写盘（重启后又生效），极难排查。
        from eviltown.world.buildings import load_points

        with runner.lock:
            applied = runner.sim.reload_furniture(load_points(path))
    return {"ok": True, "path": str(path), "applied": applied,
            "note": "已热应用，仿真继续运行"}


_BG_MOD = None          # 懒加载的 tools/remove_bg.py


def _bg_module():
    """懒加载抠图核心（tools/remove_bg.py）—— 它依赖 PIL/scipy，别拖累服务启动。"""
    global _BG_MOD
    if _BG_MOD is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "eviltown_remove_bg", ROOT / "tools" / "remove_bg.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BG_MOD = mod
    return _BG_MOD


@app.post("/api/cutout")
async def cutout(payload: dict):
    """给某张贴图去背景，写出透明 PNG（支持「棋盘格假透明」）。

    body: {"rel": "home/home_01.jpg"}
    可选: {"seeds": [{"fx":0.1,"fy":0.2,"r":0.02}, ...], "tol": 32}
      —— 手动模式：前端在图上涂的背景点（归一化坐标 + 笔刷半径比例），
         后端从涂抹点做区域生长，不再依赖「与四边连通」的自动猜测。
         满幅插画 / 棋盘格底这类自动模式啃不动的图，走这条路。
    - 只允许 web/assets/buildings 目录内的文件，路径由服务端校验；
    - 源文件**绝不覆盖**：jpg → 同名 .png，png → <stem>_alpha.png；
    - 成功后改 manifest.json 指向新 PNG（编辑器重拉 manifest 即可看到）；
    - 自动模式抠不干净（四边透明率不够）就报错、不写文件，避免产出空白图。
    """
    rel = str(payload.get("rel") or "").strip().replace("\\", "/")
    if not rel:
        return JSONResponse({"ok": False, "error": "缺少 rel"}, status_code=400)

    root = (WEB / "assets" / "buildings").resolve()
    src = (root / rel).resolve()
    if root not in src.parents or not src.is_file():
        return JSONResponse({"ok": False, "error": "路径不合法或文件不存在"},
                            status_code=400)
    strokes = payload.get("seeds") or None        # 手动模式：涂抹点
    if strokes is not None and not isinstance(strokes, list):
        return JSONResponse({"ok": False, "error": "seeds 需要是数组"}, status_code=400)
    try:
        mod = _bg_module()
        dst = mod.out_path_for(src)
        info = mod.process_one(src, dst, payload.get("tol"), 0.8, 2, False,
                               "auto", strokes, bool(payload.get("grow", True)))
    except Exception as exc:                      # 缺依赖 / 坏图，都如实回传
        return JSONResponse({"ok": False, "error": f"抠图异常：{exc}"}, status_code=500)

    # numpy 标量 / 数组不能直接进 JSON
    info = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in info.items()}
    if not info.get("ok"):
        return JSONResponse({"ok": False,
                             "error": "底色和主体分不开（四边没清干净），未生成文件"
                             if not strokes else "涂抹点没圈出任何区域，检查是不是全涂在主体上了",
                             "info": info}, status_code=200)

    new_rel = dst.resolve().relative_to(root).as_posix()
    if new_rel != rel:
        mod.update_manifest(root, {rel: new_rel})
    return {"ok": True, "rel": new_rel, "info": info}


@app.get("/api/map")
async def map_data():
    """导出静态地图：地形位图（base64）+ 建筑列表。前端初始化时取一次即可。

    - tiles：w*h 字节串的 base64，每字节为 Terrain 值（见 world/grid.py）。
    - legend：Terrain 名 -> 值，供前端上色，避免两边硬编码不一致。
    """
    assert runner
    with runner.lock:
        grid = runner.sim.grid
        tiles = base64.b64encode(grid.export_tiles()).decode("ascii")
        buildings = [
            {
                "bid": b.bid,
                "kind": b.kind.value,
                "x": b.x,
                "y": b.y,
                "w": b.w,
                "h": b.h,
                "entrance": list(b.entrance),
                "label": b.label,
                "workers": b.workers,
                "capacity": b.capacity,
                # 营业时间（仿真小时，可跨夜）：前端点击建筑改的就是这两个值
                "opens": b.opens,
                "closes": b.closes,
                # 马桶格（建筑内白点）：前端静态层画成白色小圆点
                "toilets": [list(t) for t in b.toilets],
                # 本栋实际使用的贴图（唯一真相）：前端按它加载，不再自己轮转选图
                "tex": b.tex,
            }
            for b in runner.sim.buildings
        ]
    return {
        "width": grid.w,
        "height": grid.h,
        "tile_px": runner.cfg.world.tile_px,
        "legend": {t.name: int(t) for t in Terrain},
        "tiles": tiles,
        "buildings": buildings,
    }


@app.get("/api/resident/{rid}")
async def resident(rid: int):
    assert runner
    with runner.lock:
        if 0 <= rid < len(runner.sim.residents):
            return runner.sim.residents[rid].snapshot()
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/logs")
async def logs(rid: int = 0, day: int = 0, limit: int = 400):
    """居民日志（「每人每天干了什么」）。

    rid=0 → 全城；day=0 → 所有天。返回按时间正序的尾部 limit 条，
    前端另有 WS 增量（snapshot.logs），这里只在打开日志页/切人时拉一次全量。
    """
    assert runner
    limit = max(1, min(2000, int(limit)))
    with runner.lock:
        items = list(runner.sim.logs)
    if rid:
        items = [e for e in items if e["rid"] == rid]
    if day:
        items = [e for e in items if e["day"] == day]
    return {"total": len(items), "logs": items[-limit:]}


@app.get("/api/report")
async def report(day: int = 0):
    """统计日报：白天每 2 小时一个桶，夜里整段一个桶（用户口径）。

    day=0 → 正在进行的这一天（实时快照，尚未封盘）；day=N → 第 N 个统计日。
    统计日按 06:00 起算（06:00 → 次日 05:59），夜桶 22:00–06:00 才连得起来。
    """
    assert runner
    with runner.lock:
        data = runner.sim.report(day or None)
        data["days"] = sorted(runner.sim.reports)   # 下拉用：已封盘的统计日
        return data


@app.get("/api/fields/{channel}")
async def field_map(channel: str):
    """导出某个 CA 场的 0..255 位图（前端热力图用）。"""
    assert runner
    with runner.lock:
        if channel not in runner.sim.fields.specs:
            return JSONResponse(
                {"error": "unknown channel",
                 "channels": list(runner.sim.fields.specs)}, status_code=404)
        return {
            "channel": channel,
            "width": runner.sim.fields.w,
            "height": runner.sim.fields.h,
            "peak": round(runner.sim.fields.peak(channel), 4),
            "live": runner.sim.fields.live_cells(channel),
            "bitmap": list(runner.sim.fields.export(channel)),
        }


@app.post("/api/building/{bid}/hours")
async def set_building_hours(bid: int, payload: dict):
    """设置某栋建筑的营业时间（热更新，下一 tick 立即生效）。

    body: {"opens": 8.0, "closes": 21.0}（小时，支持跨夜如 22→6；
    opens==closes 或 0→24 表示全天开放）。住宅/公园/公共厕所默认全天，
    也允许用户改成有限时段（由其自行决定）。
    """
    assert runner
    try:
        opens = float(payload["opens"])
        closes = float(payload["closes"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "需要 opens / closes 数字字段"}, status_code=400)
    if not (0.0 <= opens <= 24.0 and 0.0 <= closes <= 24.0):
        return JSONResponse({"error": "时间需在 0..24 之间"}, status_code=400)
    with runner.lock:
        ok = runner.sim.set_building_hours(bid, opens, closes)
    if not ok:
        return JSONResponse({"error": "bid 不存在"}, status_code=404)
    return {"ok": True, "bid": bid, "opens": opens, "closes": closes}


@app.post("/api/control")
async def control(payload: dict):
    assert runner
    action = payload.get("action")
    if action == "pause":
        runner.set_paused(True)
    elif action == "resume":
        runner.set_paused(False)
    elif action == "step":
        runner.step(int(payload.get("n", 1)))
    elif action == "speed":
        runner.set_speed(float(payload.get("value", 1)))
    elif action == "reset":
        with runner.lock:
            runner.sim.reset()
    return {"ok": True, "speed": runner.speed, "paused": runner.paused}


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    assert runner
    try:
        while True:
            await sock.send_text(json.dumps(runner.snapshot()))
            await asyncio.sleep(runner.cfg.server.frame_interval_ms / 1000)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    c = load_config()
    uvicorn.run(app, host=c.server.host, port=c.server.port, log_level="info")
