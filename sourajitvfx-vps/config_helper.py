"""Persistent configuration and environment helpers for SOURAJITVFX VPS."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


def load_dotenv(path: Path | None = None) -> None:
    """Load simple KEY=VALUE pairs without overriding existing environment values."""
    env_path = path or (BASE_DIR / ".env")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        LOGGER.exception("Unable to read .env file")
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()
DATA_DIR = (
    Path(os.environ["DATA_DIR"]).expanduser().resolve()
    if os.environ.get("DATA_DIR", "").strip()
    else BASE_DIR
)
CONFIG_PATH = DATA_DIR / "config.json"
CONFIG_LOCK = asyncio.Lock()


def initialize_config() -> None:
    """Create storage directories and an empty config file if needed."""
    for directory in (DATA_DIR / "bin", DATA_DIR / "instances", DATA_DIR / "downloads"):
        directory.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        _atomic_write({"vps": {}, "users": []})


def _read_config() -> dict[str, Any]:
    """Read and validate the on-disk JSON config, recovering safely if malformed."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"vps": {}, "users": []}
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read config.json; using an empty in-memory config")
        return {"vps": {}, "users": []}
    if not isinstance(data, dict) or not isinstance(data.get("vps", {}), dict):
        LOGGER.error("Invalid config.json structure; using an empty in-memory config")
        return {"vps": {}, "users": []}
    data.setdefault("vps", {})
    if not isinstance(data.get("users"), list):
        LOGGER.error("Invalid users list in config.json; replacing it with an empty list")
        data["users"] = []
    return data


def _atomic_write(data: dict[str, Any]) -> None:
    """Atomically replace config.json using a temporary file in the same directory."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CONFIG_PATH.parent,
            prefix=".config-", suffix=".tmp", delete=False
        ) as temporary:
            json.dump(data, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(CONFIG_PATH)
    except OSError:
        LOGGER.exception("Could not write config.json")
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


async def get_config() -> dict[str, Any]:
    """Return a copy of the current config under the async config lock."""
    async with CONFIG_LOCK:
        return _read_config()


async def save_config(data: dict[str, Any]) -> None:
    """Persist the supplied config under the async config lock."""
    async with CONFIG_LOCK:
        await asyncio.to_thread(_atomic_write, data)


async def mutate_config(mutator: Any) -> dict[str, Any]:
    """Apply a synchronous mutator atomically and return the updated config."""
    async with CONFIG_LOCK:
        data = _read_config()
        mutator(data)
        await asyncio.to_thread(_atomic_write, data)
        return data
