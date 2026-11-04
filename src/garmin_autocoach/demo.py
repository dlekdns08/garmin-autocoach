"""A synthetic athlete so the whole loop can be exercised without a Garmin account."""

from __future__ import annotations

import datetime as dt
import random

from .models import AthleteSnapshot, HRZones, RunSummary
from .snapshot import acwr, easy_pace, last_complete_monday, monday_of, week_bucket, weekly_stats


def demo_snapshot(as_of: dt.date | None = None, seed: int = 7, scenario: str = "steady") -> AthleteSnapshot:
    """scenario: steady | tired | fresh"""
    as_of = as_of or dt.date.today()
    rng = random.Random(seed)
    zones = HRZones(z1=103, z2=123, z3=144, z4=164, z5=185, max_hr=205, lthr=186)
    runs: list[RunSummary] = []
    weekly_km = [28, 31, 33, 30, 12]  # 4 complete weeks oldest -> newest, then the partial current week
    end = last_complete_monday(as_of)
    for wk_back, km in zip(range(3, -2, -1), weekly_km):
        monday = end - dt.timedelta(weeks=wk_back)
        if monday > as_of:
            continue
        splits = [0.18, 0.22, 0.25, 0.35]  # easy, easy, quality, long
        for offset, share in zip((1, 3, 4, 6), splits):
            day = monday + dt.timedelta(days=offset)
            if day > as_of:
                continue
            dist = round(km * share, 1)
            pace = 6.1 if share < 0.25 else (5.3 if share == 0.25 else 6.3)
            pace += rng.uniform(-0.15, 0.15)
            hr = 135 if share < 0.25 else (165 if share == 0.25 else 140)
            runs.append(RunSummary(
                activity_id=rng.randint(10**9, 10**10), date=day, distance_km=dist,
                duration_min=round(dist * pace, 1), avg_hr=hr + rng.randint(-3, 3),
                aerobic_te=2.5 + share * 3, anaerobic_te=0.0 if share != 0.25 else 1.8,
            ))
    runs.sort(key=lambda r: r.date, reverse=True)
    acute, chronic, ratio = acwr(runs, as_of)
    hrv = {"steady": ("BALANCED", 58, 57, 72, 80), "tired": ("LOW", 41, 52, 34, 61),
           "fresh": ("BALANCED", 63, 58, 88, 86)}[scenario]
    return AthleteSnapshot(
        as_of=as_of, weeks=weekly_stats(runs, as_of),
        current_week=week_bucket(runs, monday_of(as_of)) if as_of.weekday() != 6 else None, runs=runs,
        hrv_status=hrv[0], hrv_last_night=hrv[1], hrv_weekly_avg=hrv[2],
        sleep_avg_hours=7.3, sleep_score_avg=hrv[4], training_readiness=hrv[3],
        readiness_level="MODERATE" if hrv[3] >= 50 else "LOW",
        acwr=ratio, acute_km_7d=acute, chronic_km_28d=chronic,
        vo2max=48.0, training_status="PRODUCTIVE",
        race_prediction_s={"5K": 1320, "10K": 2760, "Half": 6240, "Marathon": 13500},
        hr_zones=zones, easy_pace_min_per_km=easy_pace(runs, zones), source="demo",
    )
