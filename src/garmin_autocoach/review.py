"""Compare last week's plan with what actually happened and emit an AdjustmentMemo."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

from .config import CACHE_DIR
from .models import AdjustmentMemo, AthleteSnapshot, RunSummary, WeekPlan

COMPLETION_LOW = 0.6
COMPLETION_FULL = 0.9
HR_DRIFT_WARN = 5.0  # percent


def plans_dir(root: Path = CACHE_DIR) -> Path:
    d = root / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_plan(plan: WeekPlan, root: Path = CACHE_DIR) -> Path:
    p = plans_dir(root) / f"{plan.week_key}.json"
    p.write_text(plan.model_dump_json(indent=2))
    return p


def load_plan(week_start: dt.date, root: Path = CACHE_DIR) -> Optional[WeekPlan]:
    p = plans_dir(root) / f"{week_start.isoformat()}.json"
    if not p.exists():
        return None
    return WeekPlan.model_validate_json(p.read_text())


def hr_efficiency(runs: list[RunSummary]) -> Optional[float]:
    """bpm per (km/h) over easy-ish runs; higher = more HR for the same speed."""
    vals = []
    for r in runs:
        if r.avg_hr and r.duration_min > 0 and r.distance_km > 0:
            speed = r.distance_km / (r.duration_min / 60)
            if speed > 0:
                vals.append(r.avg_hr / speed)
    return sum(vals) / len(vals) if vals else None


def hr_drift(week_runs: list[RunSummary], prior_runs: list[RunSummary]) -> Optional[float]:
    now, before = hr_efficiency(week_runs), hr_efficiency(prior_runs)
    if now is None or before is None or before == 0:
        return None
    return round((now / before - 1.0) * 100, 1)


def hrv_trend(snap: AthleteSnapshot) -> Optional[str]:
    if snap.hrv_last_night is None or not snap.hrv_weekly_avg:
        return None
    ratio = snap.hrv_last_night / snap.hrv_weekly_avg
    if ratio < 0.92:
        return "dropping"
    if ratio > 1.08:
        return "rising"
    return "stable"


def review_week(plan: WeekPlan, snap: AthleteSnapshot) -> AdjustmentMemo:
    week_end = plan.week_start + dt.timedelta(days=7)
    week_runs = [r for r in snap.runs if plan.week_start <= r.date < week_end]
    prior_runs = [r for r in snap.runs if r.date < plan.week_start]
    planned = plan.sessions
    planned_days = {w.date for w in planned}
    run_days = {r.date for r in week_runs}
    completed = len(planned_days & run_days)
    # Give credit for runs done on a different day than planned.
    completed = min(len(planned), max(completed, len(run_days)))
    completion = completed / len(planned) if planned else 0.0
    planned_km = round(sum(w.distance_km for w in planned), 1)
    actual_km = round(sum(r.distance_km for r in week_runs), 1)
    drift = hr_drift(week_runs, prior_runs)
    trend = hrv_trend(snap)
    status = (snap.hrv_status or "").upper() or None

    reasons: list[str] = [f"completed {completed}/{len(planned)} sessions ({completion:.0%}), {actual_km} of {planned_km} km"]
    direction, factor = "hold", 1.0
    if completion < COMPLETION_LOW:
        direction, factor = "reduce", 0.85
        reasons.append("completion under 60% -> repeat at a lower volume")
    if trend == "dropping" or status in {"LOW", "POOR"}:
        direction, factor = "reduce", min(factor, 0.75)
        reasons.append(f"HRV {trend or status} -> recovery week")
    if drift is not None and drift > HR_DRIFT_WARN:
        if direction != "reduce":
            direction, factor = "hold", 1.0
        reasons.append(f"HR drift +{drift}% at same speed -> do not progress")
    if direction == "hold" and completion >= COMPLETION_FULL and status == "BALANCED" and (drift is None or drift <= HR_DRIFT_WARN):
        direction, factor = "progress", 1.10
        reasons.append("all sessions done and HRV balanced -> progress 10%")
    elif direction == "hold" and factor == 1.0 and len(reasons) == 1:
        reasons.append("mixed signals -> hold volume")

    return AdjustmentMemo(
        week_start=plan.week_start, planned_sessions=len(planned), completed_sessions=completed,
        completion=round(completion, 2), planned_km=planned_km, actual_km=actual_km,
        hr_drift_pct=drift, hrv_status=status, hrv_trend=trend,
        direction=direction, factor=factor, reasons=reasons,
    )


def save_memo(memo: AdjustmentMemo, root: Path = CACHE_DIR) -> Path:
    d = root / "memos"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{memo.week_start.isoformat()}.json"
    p.write_text(memo.model_dump_json(indent=2))
    return p


def load_latest_memo(root: Path = CACHE_DIR, before: Optional[dt.date] = None) -> Optional[AdjustmentMemo]:
    d = root / "memos"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if before:
        files = [f for f in files if f.stem < before.isoformat()]
    if not files:
        return None
    try:
        return AdjustmentMemo.model_validate_json(files[-1].read_text())
    except (ValueError, json.JSONDecodeError):
        return None
