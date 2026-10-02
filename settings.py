"""Persisted user preferences, stored beside the executable.

Under PyInstaller `__file__` points inside the bundle, which is unpacked to a
temp directory that is wiped on exit, so a frozen build must anchor on
sys.executable instead.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger("widget.settings")

CONFIG_NAME = "settings.json"


@dataclass
class Settings:
    x: int | None = None
    y: int | None = None
    alpha: float = 0.80
    always_on_top: bool = True
    acrylic: bool = False
    log_history: bool = False


def config_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / CONFIG_NAME


def load_settings(path: Path | None = None) -> Settings:
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("settings unreadable at %s (%s); using defaults", target, exc)
        return Settings()

    if not isinstance(raw, dict):
        logger.warning("settings at %s are not an object; using defaults", target)
        return Settings()

    known = {f.name for f in fields(Settings)}
    try:
        return Settings(**{k: v for k, v in raw.items() if k in known})
    except TypeError as exc:
        logger.warning("settings had unusable types (%s); using defaults", exc)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    target = path or config_path()
    try:
        target.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write settings to %s: %s", target, exc)
        return False
    return True
