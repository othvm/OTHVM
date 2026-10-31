"""Configuration and paths for the OTHVM desktop pet.

The pet never stores secrets in its own config.  It reads the MCP API key and
the Gemini key from ``mcp_server/config.json`` (git-ignored) or environment
variables, and keeps only user-facing preferences here.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
CONFIG_FILE = BASE_DIR / "config.json"
EXAMPLE_FILE = BASE_DIR / "config.example.json"
MCP_CONFIG_FILE = ROOT / "mcp_server" / "config.json"
ASSET_DIR = BASE_DIR / "assets"
CACHE_DIR = BASE_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
ARTIFACT_DIR = BASE_DIR / "artifacts"

DEFAULTS: dict[str, Any] = {
    "mcp_base_url": "http://127.0.0.1:8765",
    "pet_scale": 1.0,
    "always_on_top": True,
    "show_state_label": True,
    "show_global_activity": True,
    "start_hidden": False,
    "pet_visible": True,
    "position": None,
    "window_width": 380,
    "window_height": 470,
    "frame_ms": 40,
    "input_device": "",
    "tts": {
        "enabled": True,
        "engine": "edge",
        "voice": "ar-EG-SalmaNeural",
        "rate": "+0%",
        "volume": "+0%",
        "max_chars": 600,
        "sapi_voice": "",
        "allow_sapi_fallback": True,
    },
    "stt": {
        "engine": "gemini",
        "model": "gemini-3.8-flash",
        "language": "ar-EG",
        "max_seconds": 30,
        "auto_send": True,
        "openai_base_url": "",
        "openai_model": "whisper-1",
        "gemini_timeout_seconds": 15,
        "gemini_retries": 1,
        "local_fallback": True,
        "local_model": "base",
        "local_device": "cpu",
        "local_compute_type": "int8",
        "local_beam_size": 1,
        "local_vad": True,
        "local_timeout_seconds": 120,
        "local_load_timeout_seconds": 900,
        "local_preload": True,
        "local_download_root": "",
    },
    "wake_word": {
        "enabled": False,
        "phrase": "othvm",
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
    return _deep_merge(DEFAULTS, data if isinstance(data, dict) else {})


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def mcp_config() -> dict:
    if not MCP_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(MCP_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def api_key() -> str:
    return (os.environ.get("OTHVM_MCP_API_KEY")
            or str(mcp_config().get("api_key") or "")).strip()


def gemini_key() -> str:
    return (os.environ.get("GEMINI_API_KEY")
            or str(mcp_config().get("gemini_api_key") or "")).strip()


def agent_id() -> str:
    return str(mcp_config().get("agent_id") or "").strip()


def ensure_dirs() -> None:
    for path in (CACHE_DIR, LOG_DIR, ARTIFACT_DIR):
        path.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "tts").mkdir(parents=True, exist_ok=True)


def pythonw() -> str:
    candidate = Path(os.sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else os.sys.executable)
