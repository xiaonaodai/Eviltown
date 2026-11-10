"""配置加载。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# ${VAR} 或 ${VAR:-默认值}：环境变量未设置/为空时取默认值
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class WorldCfg:
    width: int = 240
    height: int = 160
    tile_px: int = 6
    field_stride: int = 1   # CA 场每 N tick 演化一次（调大可提速，1 为最精确）


@dataclass
class TimeCfg:
    ticks_per_day: int = 1440
    start_day: int = 1
    start_tick: int = 360
    speed: int = 1
    max_ticks_per_frame: int = 400


@dataclass
class LLMCfg:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "local-model"
    api_key: str = ""
    timeout: int = 20
    intensity: str = "realistic"
    # --- 生成类功能（M5 慢脑）：全部可关，不配密钥就自动走模板 ---
    language: str = "zh"      # 生成语言 zh/en（网页切语言时热更新）
    diary: bool = True        # 每天收尾为居民写一段日记
    chat: bool = False        # 聊天/说话交给 LLM 的总开关（默认关）
    chat_residents: int = 1   # 自动挑几个人开聊天；0 = 一个都不开


@dataclass
class StorageCfg:
    db_path: str = "data/eviltown.db"
    reports_dir: str = "data/reports"
    record_events: bool = True


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8000
    frame_interval_ms: int = 200


@dataclass
class FurnitureCfg:
    """建筑点位配置：由 web/furniture-editor.html 导出，是马桶/床位的**唯一来源**。

    没有这个文件 → 建筑内不生成任何马桶/床位（绝不回落到随机生成）。
    """
    points_path: str = "points.json"


@dataclass
class Config:
    world: WorldCfg = field(default_factory=WorldCfg)
    population: int = 50
    time: TimeCfg = field(default_factory=TimeCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    server: ServerCfg = field(default_factory=ServerCfg)
    furniture: FurnitureCfg = field(default_factory=FurnitureCfg)


def _build(cls, data: dict[str, Any] | None):
    data = data or {}
    return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _expand_env(value: str) -> str:
    """展开字符串里的 ${VAR} / ${VAR:-默认值}：未设置或为空时取默认值，没有默认值则取空串。"""

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return os.environ.get(name) or (default if default is not None else "")

    return _ENV_RE.sub(repl, value)


def _expand_env_tree(node: Any) -> Any:
    """递归展开配置里的环境变量引用（字符串 / 字典 / 列表），其余类型原样返回。"""
    if isinstance(node, str):
        return _expand_env(node)
    if isinstance(node, dict):
        return {k: _expand_env_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env_tree(v) for v in node]
    return node


def load_config(path: str | Path | None = None) -> Config:
    # .env 先读进来，下面 _expand_env 才能取到 ${EVILTOWN_LLM_KEY}。
    # 延迟导入：config 是底层模块，不在顶层依赖 agents。
    from .agents.llm_client import load_dotenv
    load_dotenv()
    path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _expand_env_tree(raw)     # ${VAR} / ${VAR:-默认} → 环境变量值（如 llm.api_key）
    return Config(
        world=_build(WorldCfg, raw.get("world")),
        population=raw.get("population", 50),
        time=_build(TimeCfg, raw.get("time")),
        llm=_build(LLMCfg, raw.get("llm")),
        storage=_build(StorageCfg, raw.get("storage")),
        server=_build(ServerCfg, raw.get("server")),
        furniture=_build(FurnitureCfg, raw.get("furniture")),
    )
