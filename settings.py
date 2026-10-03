"""Persisted user preferences, stored beside the executable.

Under PyInstaller `__file__` points inside the bundle, which is unpacked to a
temp directory that is wiped on exit, so a frozen build must anchor on
sys.executable instead.

The alpha range and the default come from theme rather than being restated
here. Two copies of a bound are two answers to "what is the lowest usable
opacity", and nothing would report the disagreement: the loader would reject a
value the wheel had just produced, or accept one the panel clamps away. theme
is the project's home for design tokens and settings is a consumer of them, so
importing it is what makes the pair impossible rather than merely checked.

Loading sanitises: a file that cannot be read, parsed or trusted degrades to
usable settings instead of raising, because the caller is a GUI startup path
with no way to recover. A single bad field costs you that field, not the file.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import theme

logger = logging.getLogger("widget.settings")

CONFIG_NAME = "settings.json"

MIN_ALPHA = theme.MIN_ALPHA
MAX_ALPHA = theme.MAX_ALPHA


@dataclass
class Settings:
    x: int | None = None
    y: int | None = None
    alpha: float = theme.DEFAULT_ALPHA
    always_on_top: bool = True
    log_history: bool = False


def config_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / CONFIG_NAME


def _kind(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    # bool is a subclass of int, so `"x": true` would otherwise load as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer or null, got {_kind(value)}")
    return value


def _alpha(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {_kind(value)}")
    # Rejects out-of-range values and NaN, which compares false against
    # everything. Rejected rather than clamped: a clamped value would leave
    # the file and the loaded state disagreeing.
    if not MIN_ALPHA <= value <= MAX_ALPHA:
        raise ValueError(f"expected {MIN_ALPHA} to {MAX_ALPHA}, got {value!r}")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected true or false, got {_kind(value)}")
    return value


VALIDATORS: dict[str, Callable[[object], object]] = {
    "x": _optional_int,
    "y": _optional_int,
    "alpha": _alpha,
    "always_on_top": _bool,
    "log_history": _bool,
}


def _usable_fields(raw: dict) -> dict:
    """Keep the fields that survive validation, dropping any that do not.

    Unknown keys are skipped and bad fields fall back to their dataclass
    default, so one bad value never costs the user the rest of the file.
    """
    accepted = {}
    for name, value in raw.items():
        check = VALIDATORS.get(name)
        if check is None:
            continue
        try:
            accepted[name] = check(value)
        except ValueError as exc:
            logger.warning(
                "ignoring unusable setting %s=%r (%s); using the default", name, value, exc
            )
    return accepted


def load_settings(path: Path | None = None) -> Settings:
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError, RecursionError) as exc:
        # ValueError covers json.JSONDecodeError and the UnicodeDecodeError
        # raised by a file that is not UTF-8, e.g. one written in cp1251 or
        # UTF-16 by an older build. RecursionError is not a ValueError -- it is
        # a RuntimeError -- and json.loads raises it once the nesting is deeper
        # than the C scanner's stack, which a file someone else wrote can
        # trivially be. A file is not a reason to fail startup.
        logger.warning("settings unreadable at %s (%s); using defaults", target, exc)
        return Settings()

    if not isinstance(raw, dict):
        logger.warning("settings at %s are not an object; using defaults", target)
        return Settings()

    # Every key below is a real field name, so construction cannot raise.
    return Settings(**_usable_fields(raw))


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    target = path or config_path()
    try:
        target.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write settings to %s: %s", target, exc)
        return False
    return True
