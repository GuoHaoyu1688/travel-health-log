"""Configuration and validated daily metric values."""

import json
import math
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BP_SOURCE = "https://www.heart.org/en/health-topics/high-blood-pressure/blood-pressure-explained"
DISCLAIMER = "Personal records and descriptive associations only; not diagnosis or medical advice."
METRICS = {
    "sleep": ("hours", 0, 24),
    "sleep_quality": ("score/5", 1, 5),
    "steps": ("count", 0, 200000),
    "weight": ("kg", 1, 600),
    "systolic": ("mmHg", 40, 300),
    "diastolic": ("mmHg", 20, 200),
    "heart_rate": ("bpm", 20, 250),
    "water": ("liters", 0, 20),
    "alcohol": ("self-defined drinks", 0, 50),
    "coffee": ("cups", 0, 30),
    "exercise": ("minutes", 0, 1440),
    "mood": ("score/5", 1, 5),
    "meals": ("count", 0, 10),
    "bedtime": ("HH:MM", None, None),
    "last_meal": ("HH:MM", None, None),
    "exercise_type": ("text", None, None),
}
INTEGER_METRICS = {"steps", "sleep_quality", "mood", "meals", "systolic", "diastolic", "heart_rate"}


class LogError(Exception):
    def __init__(self, code: str, message: str, status: int = 2):
        super().__init__(message)
        self.code, self.status = code, status


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def success(value) -> dict:
    return {
        "ok": True,
        "data": value,
        "warnings": value.get("warnings", []) if isinstance(value, dict) else [],
    }


def failure(exc: Exception) -> tuple[dict, int]:
    if isinstance(exc, LogError):
        code, status = exc.code, exc.status
    elif isinstance(exc, FileNotFoundError):
        code, status = "NOT_FOUND", 3
    elif isinstance(exc, (ValueError, TypeError, KeyError, yaml.YAMLError)):
        code, status = "INVALID_DATA", 2
    else:
        code, status = "PROCESSING_ERROR", 1
    return {"ok": False, "error": {"code": code, "message": str(exc)}}, status


def day(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or not 1900 <= parsed.year <= 2200:
        raise LogError("INVALID_DATE", "Use a canonical YYYY-MM-DD date from 1900 to 2200")
    return value


def clock(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise LogError("INVALID_TIME", "Use a 24-hour HH:MM time")
    return value


def bounded_text(value: str, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise LogError("INVALID_TEXT", f"{label} must be text of at most {maximum} characters")
    return value


def metrics(values: dict) -> dict:
    if not isinstance(values, dict) or not values or len(values) > len(METRICS):
        raise LogError("INVALID_METRICS", "Supply a nonempty metric object")
    values = dict(values)
    if "bp" in values:
        parts = str(values.pop("bp")).split("/")
        if len(parts) != 2 or {"systolic", "diastolic"} & set(values):
            raise LogError("INVALID_BP", "Use bp=SYSTOLIC/DIASTOLIC without separate BP fields")
        values.update(systolic=parts[0], diastolic=parts[1])
    result = {}
    for name, value in values.items():
        if name not in METRICS:
            raise LogError("UNKNOWN_METRIC", f"Unknown metric: {name}")
        unit, low, high = METRICS[name]
        if name in {"bedtime", "last_meal"}:
            result[name] = clock(value)
        elif name == "exercise_type":
            result[name] = bounded_text(value, name, 100)
        else:
            if isinstance(value, bool):
                raise LogError("INVALID_VALUE", f"{name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or not low <= number <= high:
                raise LogError("INVALID_VALUE", f"{name} must be between {low} and {high} {unit}")
            if name in INTEGER_METRICS and not number.is_integer():
                raise LogError("INVALID_VALUE", f"{name} requires a whole number")
            result[name] = int(number) if name in INTEGER_METRICS else round(number, 6)
    if {"systolic", "diastolic"} <= set(result) and result["systolic"] <= result["diastolic"]:
        raise LogError("INVALID_BP", "Systolic pressure must be greater than diastolic pressure")
    return result


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def today(config: dict) -> str:
    return datetime.now(ZoneInfo(config["timezone"])).date().isoformat()


def settings(data_dir=None, config_path=None) -> tuple[Path, dict]:
    root = Path(data_dir or os.environ.get("THL_DATA_DIR", "data")).expanduser().absolute()
    config = {
        "timezone": "Asia/Shanghai",
        "private_permissions": True,
        "preferred_apple_source": "",
        "allowed_hosts": ["127.0.0.1", "localhost", "[::1]"],
    }
    path = Path(config_path) if config_path else root / "config.yaml"
    if path.is_file():
        loaded = yaml.safe_load(path.read_text("utf-8")) or {}
        if not isinstance(loaded, dict) or set(loaded) - set(config):
            raise LogError("INVALID_CONFIG", "Unknown configuration keys")
        config.update(loaded)
    ZoneInfo(config["timezone"])
    if type(config["private_permissions"]) is not bool:
        raise LogError("INVALID_CONFIG", "private_permissions must be boolean")
    bounded_text(config["preferred_apple_source"], "preferred_apple_source", 200)
    if not isinstance(config["allowed_hosts"], list) or not all(
        isinstance(host, str) and host and "/" not in host and "*" not in host
        for host in config["allowed_hosts"]
    ):
        raise LogError("INVALID_CONFIG", "allowed_hosts must contain explicit host names")
    return root, config


def bp_hint(systolic: float, diastolic: float) -> dict:
    """AHA adult category labels are educational, never a diagnosis."""
    if systolic <= diastolic:
        return {
            "category": "invalid_pair",
            "message": "Check the recorded pressure pair and units.",
            "source": BP_SOURCE,
            "scope": "No category assigned to an inconsistent pair.",
        }
    if systolic > 180 or diastolic > 120:
        category = "severe_range"
        message = (
            "A very high recorded reading warrants prompt professional assessment. "
            "If accompanied by chest pain, breathlessness, weakness, vision change or "
            "difficulty speaking, contact local emergency services now."
        )
    elif systolic >= 140 or diastolic >= 90:
        category, message = (
            "stage_2_range",
            "Discuss repeated readings in this range with a clinician.",
        )
    elif systolic >= 130 or diastolic >= 80:
        category, message = (
            "stage_1_range",
            "Discuss repeated readings in this range with a clinician.",
        )
    elif systolic >= 120:
        category, message = (
            "elevated_range",
            "Keep a record and discuss your readings with a clinician.",
        )
    else:
        category, message = (
            "normal_range",
            "A category label alone does not establish health status.",
        )
    return {
        "category": category,
        "message": message,
        "source": BP_SOURCE,
        "scope": "General adult categories; not pregnancy, pediatric or individualized guidance.",
    }
