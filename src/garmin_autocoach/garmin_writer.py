"""Turn a WeekPlan into Garmin Connect workout payloads, upload + schedule them, and undo.

Payload structure mirrors what Garmin Connect's own web app sends (ExecutableStepDTO /
RepeatGroupDTO with heart.rate.zone targets). dry_run=True never touches the network.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
from typing import Any, Optional

from .config import CACHE_DIR
from .models import AppliedWeek, AppliedWorkout, HRZones, WeekPlan, Workout

RUNNING = {"sportTypeId": 1, "sportTypeKey": "running"}
NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
HR_TARGET = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
STEP = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat"},
}
NAME_PREFIX = "[autocoach]"


def _hr_fields(zone: int, zones: Optional[HRZones]) -> tuple[dict, str]:
    """Named Garmin zone by default; exact bpm range when the athlete's zones are known."""
    if zones is not None:
        lo, hi = zones.range(zone)
        return {"targetValueOne": float(lo), "targetValueTwo": float(hi)}, f"Z{zone} {lo}-{hi}bpm"
    return {"zoneNumber": zone}, f"Z{zone}"


def _exec(order: int, step: str, desc: str, seconds: int, target: dict | None = None) -> dict:
    d = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": STEP[step],
        "description": desc,
        "endCondition": TIME,
        "endConditionValue": float(seconds),
        "targetType": HR_TARGET if target else NO_TARGET,
    }
    if target:
        d.update(target)
    return d


def _wrap(name: str, description: str, steps: list[dict]) -> dict:
    return {
        "workoutName": name,
        "description": description,
        "sportType": RUNNING,
        "workoutSegments": [{"segmentOrder": 1, "sportType": RUNNING, "workoutSteps": steps}],
    }


def build_steady(w: Workout, zones: Optional[HRZones]) -> dict:
    """Easy / long run: optional warmup, main block in zone, optional cooldown."""
    target, desc = _hr_fields(w.hr_zone, zones)
    main = max(w.duration_min - w.warmup_min - w.cooldown_min, 10)
    steps, order = [], 1
    if w.warmup_min:
        steps.append(_exec(order, "warmup", f"Warmup {w.warmup_min} min", w.warmup_min * 60)); order += 1
    steps.append(_exec(order, "interval", f"Run {main} min {desc}", main * 60, target)); order += 1
    if w.cooldown_min:
        steps.append(_exec(order, "cooldown", f"Cooldown {w.cooldown_min} min", w.cooldown_min * 60))
    return _wrap(f"{NAME_PREFIX} {w.name}", f"{w.why} ({main}m {desc})", steps)


def build_tempo(w: Workout, zones: Optional[HRZones]) -> dict:
    target, desc = _hr_fields(w.hr_zone, zones)
    main = max(w.duration_min - w.warmup_min - w.cooldown_min, 10)
    steps = [
        _exec(1, "warmup", f"Warmup {w.warmup_min} min", w.warmup_min * 60),
        _exec(2, "interval", f"Tempo {main} min {desc}", main * 60, target),
        _exec(3, "cooldown", f"Cooldown {w.cooldown_min} min", w.cooldown_min * 60),
    ]
    return _wrap(f"{NAME_PREFIX} {w.name}", f"{w.warmup_min}m warmup + {main}m tempo {desc} + {w.cooldown_min}m cooldown", steps)


def build_intervals(w: Workout, zones: Optional[HRZones]) -> dict:
    target, desc = _hr_fields(w.hr_zone, zones)
    children = [_exec(3, "interval", f"Rep {w.rep_seconds}s {desc}", w.rep_seconds, target)]
    if w.recovery_seconds > 0:
        children.append(_exec(4, "recovery", f"Jog {w.recovery_seconds}s", w.recovery_seconds))
    repeat = {
        "type": "RepeatGroupDTO",
        "stepOrder": 2,
        "stepType": STEP["repeat"],
        "numberOfIterations": w.repeats,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": float(w.repeats),
        "workoutSteps": children,
    }
    steps = [
        _exec(1, "warmup", f"Warmup {w.warmup_min} min", w.warmup_min * 60),
        repeat,
        _exec(3 + len(children), "cooldown", f"Cooldown {w.cooldown_min} min", w.cooldown_min * 60),
    ]
    rep_disp = f"{w.rep_seconds // 60}m" if w.rep_seconds % 60 == 0 else f"{w.rep_seconds}s"
    rec_disp = f"{w.recovery_seconds // 60}m" if w.recovery_seconds % 60 == 0 else f"{w.recovery_seconds}s"
    return _wrap(f"{NAME_PREFIX} {w.name}",
                 f"{w.warmup_min}m warmup + {w.repeats}x({rep_disp} {desc} / {rec_disp} jog) + {w.cooldown_min}m cooldown", steps)


def to_payload(w: Workout, zones: Optional[HRZones] = None) -> Optional[dict]:
    if w.kind == "rest":
        return None
    if w.kind == "intervals":
        return build_intervals(w, zones)
    if w.kind == "tempo":
        return build_tempo(w, zones)
    return build_steady(w, zones)


# --------------------------------------------------------------------------- #
# Apply / undo
# --------------------------------------------------------------------------- #
def applied_dir(root: Path = CACHE_DIR) -> Path:
    d = root / "applied"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_applied(week_start: dt.date, root: Path = CACHE_DIR) -> Optional[AppliedWeek]:
    p = applied_dir(root) / f"{week_start.isoformat()}.json"
    return AppliedWeek.model_validate_json(p.read_text()) if p.exists() else None


def _call(fn, *args) -> Any:
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class GarminWriter:
    def __init__(self, client=None, dry_run: bool = True, cache_root: Path = CACHE_DIR, console=None):
        self.client = client
        self.dry_run = dry_run
        self.cache_root = cache_root
        self.console = console

    def _log(self, msg: str) -> None:
        if self.console is not None:
            self.console.print(msg, markup=False, highlight=False)
        else:
            print(msg)

    def apply(self, plan: WeekPlan, zones: Optional[HRZones] = None, max_sessions: Optional[int] = None) -> AppliedWeek:
        if load_applied(plan.week_start, self.cache_root) and not self.dry_run:
            raise RuntimeError(f"Week {plan.week_key} already applied; run `autocoach undo --week {plan.week_key}` first.")
        sessions = plan.sessions
        if max_sessions is not None and len(sessions) > max_sessions:
            raise RuntimeError(f"Plan has {len(sessions)} sessions but config allows {max_sessions}; refusing.")
        record = AppliedWeek(week_start=plan.week_start, applied_at=dt.datetime.now())
        mode = "[DRY RUN] " if self.dry_run else ""
        for w in sessions:
            payload = to_payload(w, zones)
            if payload is None:
                continue
            self._log(f"{mode}{w.date} upload '{payload['workoutName']}' -> schedule on {w.date}")
            if self.dry_run:
                self._log("  " + json.dumps(payload)[:160] + " ...")
                continue
            if self.client is None:
                raise RuntimeError("apply(dry_run=False) needs a Garmin client")
            uploaded = _call(self.client.upload_workout, payload) or {}
            workout_id = uploaded.get("workoutId")
            schedule_id = None
            if workout_id:
                scheduled = _call(self.client.schedule_workout, workout_id, w.date.isoformat()) or {}
                schedule_id = scheduled.get("workoutScheduleId") or scheduled.get("id")
            record.items.append(AppliedWorkout(date=w.date, name=payload["workoutName"],
                                               workout_id=workout_id, schedule_id=schedule_id))
            self._log(f"  uploaded workoutId={workout_id} scheduleId={schedule_id}")
        if not self.dry_run:
            (applied_dir(self.cache_root) / f"{plan.week_key}.json").write_text(record.model_dump_json(indent=2))
        return record

    def _calendar_entries(self, workout_id: int, day: dt.date) -> list[int]:
        """Calendar-entry ids for a workout id, from the month view (fallback when apply got no id)."""
        try:
            cal = _call(self.client.get_scheduled_workouts, day.year, day.month) or {}
        except Exception:  # noqa: BLE001
            return []
        return [int(it["id"]) for it in cal.get("calendarItems", [])
                if it.get("workoutId") == workout_id and it.get("id")]

    def undo(self, week_start: dt.date) -> int:
        record = load_applied(week_start, self.cache_root)
        if record is None:
            self._log(f"Nothing recorded for week {week_start.isoformat()}.")
            return 0
        mode = "[DRY RUN] " if self.dry_run else ""
        done = 0
        for item in record.items:
            self._log(f"{mode}{item.date} unschedule {item.schedule_id or '(lookup)'} + delete workout {item.workout_id} ({item.name})")
            if self.dry_run:
                continue
            if not item.schedule_id and item.workout_id:
                for sid in self._calendar_entries(item.workout_id, item.date):
                    try:
                        _call(self.client.unschedule_workout, sid)
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"  unschedule {sid} failed: {exc}")
            if item.schedule_id:
                try:
                    _call(self.client.unschedule_workout, item.schedule_id)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"  unschedule failed: {exc}")
            if item.workout_id:
                try:
                    _call(self.client.delete_workout, item.workout_id)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    self._log(f"  delete failed: {exc}")
        if not self.dry_run:
            p = applied_dir(self.cache_root) / f"{week_start.isoformat()}.json"
            p.rename(p.with_suffix(".undone.json"))
        return done
