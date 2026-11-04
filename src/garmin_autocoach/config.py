"""User configuration: ~/.config/garmin-autocoach/config.toml."""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CONFIG_PATH = Path(os.environ.get("AUTOCOACH_CONFIG", "~/.config/garmin-autocoach/config.toml")).expanduser()
CACHE_DIR = Path(os.environ.get("AUTOCOACH_CACHE", "~/.cache/garmin-autocoach")).expanduser()

EXAMPLE_CONFIG = """# ~/.config/garmin-autocoach/config.toml
[athlete]
days_available = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
max_sessions = 5          # hard cap, the planner never schedules more
long_run_day = "sun"
quality_day = "wed"
max_weekly_km = 80        # hard cap on planned volume
min_weekly_km = 10        # floor when starting from nothing

[race]
# date = "2027-03-15"
# distance_km = 42.195

[garmin]
# tokens = "~/.garminconnect"   # or set GARMINTOKENS

[llm]
# model = "claude-sonnet-5-5"
"""


class Config(BaseModel):
    days_available: list[str] = Field(default_factory=lambda: list(DAYS))
    max_sessions: int = 5
    long_run_day: str = "sun"
    quality_day: str = "wed"
    max_weekly_km: float = 80.0
    min_weekly_km: float = 10.0
    race_date: Optional[dt.date] = None
    race_distance_km: Optional[float] = None
    tokens: str = "~/.garminconnect"
    llm_model: str = "claude-sonnet-5-5"

    @field_validator("days_available")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        out = [d.lower()[:3] for d in v]
        bad = [d for d in out if d not in DAYS]
        if bad:
            raise ValueError(f"unknown day(s) {bad}; use {DAYS}")
        return sorted(set(out), key=DAYS.index)

    @field_validator("long_run_day", "quality_day")
    @classmethod
    def _day(cls, v: str) -> str:
        v = v.lower()[:3]
        if v not in DAYS:
            raise ValueError(f"unknown day {v}")
        return v

    @field_validator("max_sessions")
    @classmethod
    def _sessions(cls, v: int) -> int:
        if not 1 <= v <= 7:
            raise ValueError("max_sessions must be 1..7")
        return v

    @property
    def tokens_path(self) -> str:
        return os.environ.get("GARMINTOKENS") or os.path.expanduser(self.tokens)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    athlete = raw.get("athlete", {})
    race = raw.get("race", {})
    garmin = raw.get("garmin", {})
    llm = raw.get("llm", {})
    data = {**athlete}
    if "date" in race:
        d = race["date"]
        data["race_date"] = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
    if "distance_km" in race:
        data["race_distance_km"] = float(race["distance_km"])
    if "tokens" in garmin:
        data["tokens"] = garmin["tokens"]
    if "model" in llm:
        data["llm_model"] = llm["model"]
    return Config(**data)
