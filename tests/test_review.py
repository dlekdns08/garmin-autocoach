import datetime as dt

from garmin_autocoach.demo import demo_snapshot
from garmin_autocoach.models import RunSummary
from garmin_autocoach.planner import RulePlanner
from garmin_autocoach.review import review_week

from conftest import AS_OF

PREV = dt.date(2026, 10, 26)  # Monday of the week before AS_OF's week


def _plan(config):
    return RulePlanner(config).plan(demo_snapshot(as_of=PREV - dt.timedelta(days=1)), week_start=PREV)


def test_all_done_and_balanced_progresses(config):
    plan = _plan(config)
    snap = demo_snapshot(as_of=AS_OF, scenario="steady")
    runs = [RunSummary(date=w.date, distance_km=w.distance_km, duration_min=w.duration_min, avg_hr=140) for w in plan.sessions]
    snap = snap.model_copy(update={"runs": runs + [r for r in snap.runs if r.date < PREV], "hrv_status": "BALANCED"})
    memo = review_week(plan, snap)
    assert memo.completion == 1.0
    assert memo.direction == "progress" and memo.factor == 1.10


def test_low_completion_reduces(config):
    plan = _plan(config)
    snap = demo_snapshot(as_of=AS_OF)
    one = plan.sessions[0]
    snap = snap.model_copy(update={"runs": [RunSummary(date=one.date, distance_km=5, duration_min=30, avg_hr=140)]})
    memo = review_week(plan, snap)
    assert memo.completion < 0.6 and memo.direction == "reduce" and memo.factor < 1


def test_hrv_dropping_reduces_even_if_complete(config):
    plan = _plan(config)
    snap = demo_snapshot(as_of=AS_OF)
    runs = [RunSummary(date=w.date, distance_km=w.distance_km, duration_min=w.duration_min, avg_hr=140) for w in plan.sessions]
    snap = snap.model_copy(update={"runs": runs, "hrv_status": "BALANCED", "hrv_last_night": 45, "hrv_weekly_avg": 58})
    memo = review_week(plan, snap)
    assert memo.hrv_trend == "dropping" and memo.direction == "reduce"


def test_hr_drift_holds(config):
    plan = _plan(config)
    snap = demo_snapshot(as_of=AS_OF)
    prior = [RunSummary(date=PREV - dt.timedelta(days=i), distance_km=8, duration_min=48, avg_hr=135) for i in (2, 4, 6)]
    runs = [RunSummary(date=w.date, distance_km=8, duration_min=48, avg_hr=150) for w in plan.sessions]
    snap = snap.model_copy(update={"runs": runs + prior, "hrv_status": "BALANCED"})
    memo = review_week(plan, snap)
    assert memo.hr_drift_pct and memo.hr_drift_pct > 5
    assert memo.direction == "hold"
