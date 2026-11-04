"""Pydantic schemas shared by every stage of the loop.

snapshot -> plan -> apply -> (train) -> review -> plan ...
"""

from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

WorkoutKind = Literal["easy", "long", "intervals", "tempo", "rest"]
Direction = Literal["progress", "hold", "reduce"]


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
class RunSummary(BaseModel):
    activity_id: Optional[int] = None
    date: dt.date
    distance_km: float
    duration_min: float
    avg_hr: Optional[float] = None
    aerobic_te: Optional[float] = None
    anaerobic_te: Optional[float] = None

    @property
    def pace_min_per_km(self) -> Optional[float]:
        if self.distance_km <= 0:
            return None
        return self.duration_min / self.distance_km


class WeekStats(BaseModel):
    week_start: dt.date  # Monday
    km: float = 0.0
    sessions: int = 0
    long_run_km: float = 0.0
    avg_hr: Optional[float] = None


class HRZones(BaseModel):
    """Zone floors in bpm (Garmin's zoneNFloor fields)."""

    z1: int
    z2: int
    z3: int
    z4: int
    z5: int
    max_hr: Optional[int] = None
    lthr: Optional[int] = None

    def range(self, zone: int) -> tuple[int, int]:
        floors = [self.z1, self.z2, self.z3, self.z4, self.z5]
        lo = floors[zone - 1]
        hi = floors[zone] - 1 if zone < 5 else (self.max_hr or self.z5 + 20)
        return lo, hi


class AthleteSnapshot(BaseModel):
    as_of: dt.date
    weeks: list[WeekStats] = Field(default_factory=list, description="Last 4 complete weeks, oldest first")
    current_week: Optional[WeekStats] = Field(default=None, description="Partial, in-progress week (None on Sundays)")
    runs: list[RunSummary] = Field(default_factory=list, description="Runs in the window, newest first")
    hrv_status: Optional[str] = None  # BALANCED / UNBALANCED / LOW / POOR
    hrv_last_night: Optional[int] = None
    hrv_weekly_avg: Optional[int] = None
    sleep_avg_hours: Optional[float] = None
    sleep_score_avg: Optional[float] = None
    training_readiness: Optional[int] = None
    readiness_level: Optional[str] = None
    acwr: Optional[float] = None  # (7d km) / (28d km / 4)
    acute_km_7d: float = 0.0
    chronic_km_28d: float = 0.0
    vo2max: Optional[float] = None
    training_status: Optional[str] = None
    race_prediction_s: dict[str, int] = Field(default_factory=dict)  # "5K": 1294 ...
    hr_zones: Optional[HRZones] = None
    easy_pace_min_per_km: Optional[float] = None
    source: Literal["garmin", "demo", "cache"] = "garmin"

    @property
    def last_week(self) -> Optional[WeekStats]:
        return self.weeks[-1] if self.weeks else None

    @property
    def avg_weekly_km(self) -> float:
        nonzero = [w.km for w in self.weeks if w.km > 0]
        return sum(nonzero) / len(nonzero) if nonzero else 0.0


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
class Workout(BaseModel):
    date: dt.date
    kind: WorkoutKind
    name: str
    duration_min: int = 0
    distance_km: float = 0.0
    hr_zone: int = 2
    warmup_min: int = 0
    cooldown_min: int = 0
    repeats: int = 0
    rep_seconds: int = 0
    recovery_seconds: int = 0
    why: str

    @field_validator("hr_zone")
    @classmethod
    def _zone_range(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("hr_zone must be 1..5")
        return v


class WeekPlan(BaseModel):
    week_start: dt.date
    target_km: float
    baseline_km: float
    direction: Direction
    deload: bool = False
    planner: str = "rules"
    reasons: list[str] = Field(default_factory=list)
    workouts: list[Workout] = Field(default_factory=list)

    @property
    def week_key(self) -> str:
        return self.week_start.isoformat()

    @property
    def sessions(self) -> list[Workout]:
        return [w for w in self.workouts if w.kind != "rest"]


# --------------------------------------------------------------------------- #
# Review -> memo
# --------------------------------------------------------------------------- #
class AdjustmentMemo(BaseModel):
    week_start: dt.date
    planned_sessions: int
    completed_sessions: int
    completion: float  # 0..1
    planned_km: float
    actual_km: float
    hr_drift_pct: Optional[float] = None  # + means HR higher at same pace vs baseline
    hrv_status: Optional[str] = None
    hrv_trend: Optional[str] = None  # rising / stable / dropping
    direction: Direction
    factor: float  # multiplier suggested for next week's baseline
    reasons: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Applied record (for undo)
# --------------------------------------------------------------------------- #
class AppliedWorkout(BaseModel):
    date: dt.date
    name: str
    workout_id: Optional[int] = None
    schedule_id: Optional[int] = None


class AppliedWeek(BaseModel):
    week_start: dt.date
    applied_at: dt.datetime
    items: list[AppliedWorkout] = Field(default_factory=list)
