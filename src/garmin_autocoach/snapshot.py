"""Read the athlete's recent Garmin data into an AthleteSnapshot.

Every Garmin call is wrapped: a failure yields None for that field rather than
aborting the snapshot. Raw responses are cached under ~/.cache/garmin-autocoach/raw/.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

from .config import CACHE_DIR, Config
from .models import AthleteSnapshot, HRZones, RunSummary, WeekStats

RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running",
             "street_running", "virtual_run", "ultra_run"}

_quiet_loggers = ("garminconnect", "garth", "urllib3", "httpx", "requests")


def quiet_library_logs() -> None:
    for name in _quiet_loggers:
        logging.getLogger(name).setLevel(logging.CRITICAL)
        logging.getLogger(name).propagate = False


def connect(config: Config):
    """Log in with stored tokens. Library stderr noise is swallowed."""
    quiet_library_logs()
    from garminconnect import Garmin  # imported lazily so `demo` works without network

    client = Garmin()
    with contextlib.redirect_stderr(io.StringIO()):
        client.login(config.tokens_path)
    return client


class RawCache:
    def __init__(self, root: Path = CACHE_DIR, day: Optional[dt.date] = None):
        self.dir = root / "raw" / (day or dt.date.today()).isoformat()
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, data: Any) -> None:
        try:
            (self.dir / f"{name}.json").write_text(json.dumps(data, default=str, indent=1))
        except OSError:
            pass

    def get(self, name: str) -> Any:
        p = self.dir / f"{name}.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None


def _safe(cache: RawCache, name: str, fn: Callable[[], Any], refresh: bool = False) -> Any:
    if not refresh:
        hit = cache.get(name)
        if hit is not None:
            return hit
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            data = fn()
    except Exception:  # noqa: BLE001 - any API failure degrades to None
        return None
    if data is not None:
        cache.put(name, data)
    return data


# --------------------------------------------------------------------------- #
# Pure transforms (unit-testable without Garmin)
# --------------------------------------------------------------------------- #
def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def runs_from_activities(activities: list[dict], since: dt.date) -> list[RunSummary]:
    runs: list[RunSummary] = []
    for a in activities or []:
        key = ((a.get("activityType") or {}).get("typeKey") or "").lower()
        if key not in RUN_TYPES:
            continue
        start = a.get("startTimeLocal") or a.get("startTimeGMT")
        if not start:
            continue
        day = dt.date.fromisoformat(str(start)[:10])
        if day < since:
            continue
        dist_m = float(a.get("distance") or 0.0)
        dur_s = float(a.get("duration") or 0.0)
        if dist_m <= 0 or dur_s <= 0:
            continue
        runs.append(RunSummary(
            activity_id=a.get("activityId"),
            date=day,
            distance_km=round(dist_m / 1000, 2),
            duration_min=round(dur_s / 60, 1),
            avg_hr=a.get("averageHR"),
            aerobic_te=a.get("aerobicTrainingEffect"),
            anaerobic_te=a.get("anaerobicTrainingEffect"),
        ))
    runs.sort(key=lambda r: r.date, reverse=True)
    return runs


def last_complete_monday(as_of: dt.date) -> dt.date:
    """Monday of the most recent *complete* week (the current week only counts on a Sunday)."""
    this_monday = monday_of(as_of)
    return this_monday if as_of.weekday() == 6 else this_monday - dt.timedelta(weeks=1)


def week_bucket(runs: list[RunSummary], monday: dt.date) -> WeekStats:
    rs = [r for r in runs if monday_of(r.date) == monday]
    hrs = [r.avg_hr for r in rs if r.avg_hr]
    return WeekStats(
        week_start=monday,
        km=round(sum(r.distance_km for r in rs), 1),
        sessions=len(rs),
        long_run_km=round(max((r.distance_km for r in rs), default=0.0), 1),
        avg_hr=round(sum(hrs) / len(hrs), 1) if hrs else None,
    )


def weekly_stats(runs: list[RunSummary], as_of: dt.date, weeks: int = 4) -> list[WeekStats]:
    """Return `weeks` complete-week buckets ending with the last complete week, oldest first."""
    end = last_complete_monday(as_of)
    buckets: dict[dt.date, list[RunSummary]] = defaultdict(list)
    for r in runs:
        buckets[monday_of(r.date)].append(r)
    out = []
    for i in range(weeks - 1, -1, -1):
        wk = end - dt.timedelta(weeks=i)
        rs = buckets.get(wk, [])
        hrs = [r.avg_hr for r in rs if r.avg_hr]
        out.append(WeekStats(
            week_start=wk,
            km=round(sum(r.distance_km for r in rs), 1),
            sessions=len(rs),
            long_run_km=round(max((r.distance_km for r in rs), default=0.0), 1),
            avg_hr=round(sum(hrs) / len(hrs), 1) if hrs else None,
        ))
    return out


def acwr(runs: list[RunSummary], as_of: dt.date) -> tuple[float, float, Optional[float]]:
    d7 = as_of - dt.timedelta(days=7)
    d28 = as_of - dt.timedelta(days=28)
    acute = sum(r.distance_km for r in runs if d7 < r.date <= as_of)
    chronic = sum(r.distance_km for r in runs if d28 < r.date <= as_of)
    ratio = round(acute / (chronic / 4), 2) if chronic > 0 else None
    return round(acute, 1), round(chronic, 1), ratio


def easy_pace(runs: list[RunSummary], zones: Optional[HRZones]) -> Optional[float]:
    """Median pace of runs whose avg HR sits in Z1-Z2 (or all runs if zones unknown)."""
    candidates = []
    for r in runs:
        p = r.pace_min_per_km
        if p is None or p < 2.5 or p > 15:
            continue
        if zones and r.avg_hr and r.avg_hr >= zones.z3:
            continue
        candidates.append(p)
    if not candidates:
        candidates = [r.pace_min_per_km for r in runs if r.pace_min_per_km]
    if not candidates:
        return None
    candidates.sort()
    return round(candidates[len(candidates) // 2], 2)


def parse_hr_zones(raw: Any) -> Optional[HRZones]:
    if not isinstance(raw, list):
        return None
    chosen = None
    for z in raw:
        if z.get("sport") == "RUNNING":
            chosen = z
            break
        if z.get("sport") == "DEFAULT" and chosen is None:
            chosen = z
    if not chosen:
        return None
    try:
        return HRZones(
            z1=int(chosen["zone1Floor"]), z2=int(chosen["zone2Floor"]), z3=int(chosen["zone3Floor"]),
            z4=int(chosen["zone4Floor"]), z5=int(chosen["zone5Floor"]),
            max_hr=chosen.get("maxHeartRateUsed"), lthr=chosen.get("lactateThresholdHeartRateUsed"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _vo2max_from_status(ts: Any) -> Optional[float]:
    try:
        g = ts["mostRecentVO2Max"]["generic"]
        return g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")
    except (KeyError, TypeError, AttributeError):
        return None


def _training_status_label(ts: Any) -> Optional[str]:
    try:
        data = ts["mostRecentTrainingStatus"]["latestTrainingStatusData"]
        first = next(iter(data.values()))
        return first.get("trainingStatusFeedbackPhrase") or str(first.get("trainingStatus"))
    except (KeyError, TypeError, AttributeError, StopIteration):
        return None


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_snapshot(client, as_of: Optional[dt.date] = None, refresh: bool = False,
                   cache_root: Path = CACHE_DIR) -> AthleteSnapshot:
    as_of = as_of or dt.date.today()
    cache = RawCache(cache_root, as_of)
    yesterday = (as_of - dt.timedelta(days=1)).isoformat()

    activities = _safe(cache, "activities", lambda: client.get_activities(0, 120), refresh) or []
    runs = runs_from_activities(activities, since=as_of - dt.timedelta(days=42))
    weeks = weekly_stats(runs, as_of)
    current = week_bucket(runs, monday_of(as_of)) if as_of.weekday() != 6 else None
    acute, chronic, ratio = acwr(runs, as_of)

    hrv = _safe(cache, "hrv", lambda: client.get_hrv_data(yesterday), refresh) or {}
    hrv_sum = hrv.get("hrvSummary") or {}

    sleep_hours, sleep_scores = [], []
    for i in range(1, 8):
        day = (as_of - dt.timedelta(days=i)).isoformat()
        s = _safe(cache, f"sleep_{day}", lambda d=day: client.get_sleep_data(d), refresh) or {}
        dto = s.get("dailySleepDTO") or {}
        secs = dto.get("sleepTimeSeconds")
        if secs:
            sleep_hours.append(secs / 3600)
        score = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")
        if score:
            sleep_scores.append(score)

    readiness = _safe(cache, "readiness", lambda: client.get_training_readiness(as_of.isoformat()), refresh)
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    readiness = readiness or {}

    status = _safe(cache, "training_status", lambda: client.get_training_status(as_of.isoformat()), refresh)
    race = _safe(cache, "race_predictions", lambda: client.get_race_predictions(), refresh) or {}
    zones = parse_hr_zones(_safe(cache, "hr_zones", lambda: client.connectapi("/biometric-service/heartRateZones"), refresh))

    preds = {}
    for key, label in (("time5K", "5K"), ("time10K", "10K"), ("timeHalfMarathon", "Half"), ("timeMarathon", "Marathon")):
        if isinstance(race, dict) and race.get(key):
            preds[label] = int(race[key])

    return AthleteSnapshot(
        as_of=as_of,
        weeks=weeks,
        current_week=current,
        runs=runs,
        hrv_status=hrv_sum.get("status"),
        hrv_last_night=hrv_sum.get("lastNightAvg"),
        hrv_weekly_avg=hrv_sum.get("weeklyAvg"),
        sleep_avg_hours=round(sum(sleep_hours) / len(sleep_hours), 1) if sleep_hours else None,
        sleep_score_avg=round(sum(sleep_scores) / len(sleep_scores), 0) if sleep_scores else None,
        training_readiness=readiness.get("score"),
        readiness_level=readiness.get("level"),
        acwr=ratio,
        acute_km_7d=acute,
        chronic_km_28d=chronic,
        vo2max=_vo2max_from_status(status),
        training_status=_training_status_label(status),
        race_prediction_s=preds,
        hr_zones=zones,
        easy_pace_min_per_km=easy_pace(runs, zones),
        source="garmin",
    )


def save_snapshot(snap: AthleteSnapshot, cache_root: Path = CACHE_DIR) -> Path:
    d = cache_root / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{snap.as_of.isoformat()}.json"
    p.write_text(snap.model_dump_json(indent=2))
    return p


def load_snapshot(day: dt.date, cache_root: Path = CACHE_DIR) -> Optional[AthleteSnapshot]:
    p = cache_root / "snapshots" / f"{day.isoformat()}.json"
    if not p.exists():
        return None
    return AthleteSnapshot.model_validate_json(p.read_text())
