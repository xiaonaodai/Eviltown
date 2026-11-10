"""统一的 LLM 客户端 —— 愿望 / 日记 / 聊天三条线共用一处。

为什么单开一个模块（而不是各自 urllib）：
- **失败处理必须一致**：超时、连接拒绝、返回非 JSON、模型胡说 —— 一律返回 None，
  调用方各自降级（模板愿望 / 模板日记 / 不生成对白），绝不让异常冒进主循环。
- **密钥只能有一个来源**：环境变量 > 项目根 `.env` > config.yaml 的 api_key。
  开源仓库里 `.env` 不进 git，`.env.example` 只有键名。
- **语言是生成参数**：跟随界面语言（zh / en），由调用方传或 `set_language()` 统一设置。

红线（AGENTS.md）：LLM 不进主循环。本模块只发请求、返回文本，
不解析业务结构、不读写仿真状态、不阻塞 tick。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # 项目根

# 当前生成语言：由前端切换界面语言时热更新（server 层调 set_language）
_LANG = "zh"
_LANG_LOCK = threading.Lock()

_LANG_HINT = {
    "zh": "全部用简体中文写，不要夹英文单词。",
    "en": "Write everything in natural English. Do not use Chinese characters.",
}


def set_language(code: str) -> str:
    """设置生成语言（zh / en）。返回实际生效的值。"""
    code = "en" if str(code).lower().startswith("en") else "zh"
    global _LANG
    with _LANG_LOCK:
        _LANG = code
    return code


def get_language() -> str:
    return _LANG


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 .env 读取（不引第三方依赖）。已存在的环境变量**不覆盖**。"""
    p = path or (ROOT / ".env")
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if not k:
                continue
            out[k] = v
            os.environ.setdefault(k, v)      # 显式环境变量优先
    except Exception:  # noqa: BLE001
        return {}
    return out


class LLMClient:
    """一个配置 → 一个客户端。每次调用独立超时，失败返回 None。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = str(getattr(cfg, "base_url", "") or "").rstrip("/")
        self.model = str(getattr(cfg, "model", "") or "")
        self.api_key = str(getattr(cfg, "api_key", "") or "")
        self.timeout = int(getattr(cfg, "timeout", 20) or 20)

    @property
    def available(self) -> bool:
        """enabled + 有地址 + 有模型名。密钥可空：本地 llama.cpp / Ollama 免鉴权。"""
        if not getattr(self.cfg, "enabled", False):
            return False
        return bool(self.base_url and self.model)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, system: str, user: str, *, lang: str | None = None,
             max_tokens: int = 512, temperature: float = 0.8) -> str | None:
        """单轮对话。返回文本，任何失败都返回 None（调用方降级）。"""
        hint = _LANG_HINT.get(lang or get_language(), _LANG_HINT["zh"])
        return self.chat_messages(
            [{"role": "system", "content": f"{system}\n\n{hint}"},
             {"role": "user", "content": user}],
            max_tokens=max_tokens, temperature=temperature)

    def chat_messages(self, messages: list[dict], *, max_tokens: int = 512,
                      temperature: float = 0.8) -> str | None:
        if not self.available:
            return None
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens,
                "stream": False}
        try:
            req = urllib.request.Request(
                self.base_url + "/chat/completions",
                data=json.dumps(body).encode("utf-8"), headers=self._headers())
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            txt = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            return str(txt).strip() if txt else None
        except Exception:  # noqa: BLE001  超时/拒连/坏 JSON 一律降级
            return None

    def probe(self) -> tuple[bool, str]:
        """连通性自检：给设置页/命令行用。返回 (ok, 说明)。"""
        if not self.available:
            return False, "未启用或缺少 base_url / model"
        try:
            req = urllib.request.Request(self.base_url + "/models",
                                         headers=self._headers())
            with urllib.request.urlopen(req, timeout=min(self.timeout, 10)) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            ids = [m.get("id") for m in (data.get("data") or [])]
            return True, f"OK · 可用模型 {ids or '(空)'}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
