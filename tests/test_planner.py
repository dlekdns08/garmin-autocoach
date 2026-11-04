import datetime as dt

from garmin_autocoach.config import Config
from garmin_autocoach.demo import demo_snapshot
from garmin_autocoach.models import AdjustmentMemo
from garmin_autocoach.planner import RulePlanner, enforce_caps, is_deload_week, next_week_start, quality_kind

from conftest import AS_OF

WEEK = dt.date(2026, 11, 9)   # ISO week 46: even -> intervals, not deload
DELOAD_WEEK = dt.date(2026, 11, 23)  # ISO week 48 -> deload


def test_next_week_start_is_monday():
    assert next_week_start(AS_OF) == WEEK and WEEK.weekday() == 0


def test_progression_capped_at_10_percent(config, snap):
    plan = RulePlanner(config).plan(snap, week_start=WEEK)
    assert plan.direction == "progress"
    assert plan.target_km <= round(plan.baseline_km * 1.10, 1) + 0.05
    assert plan.target_km > plan.baseline_km


def test_memo_cannot_exceed_cap(config, snap):
    memo = AdjustmentMemo(week_start=WEEK - dt.timedelta(weeks=1), planned_sessions=4, completed_sessions=4,
                          completion=1.0, planned_km=30, actual_km=31, direction="progress", factor=1.5, reasons=["x"])
    plan = RulePlanner(config).plan(snap, memo=memo, week_start=WEEK)
    assert plan.target_km <= round(plan.baseline_km * 1.10, 1) + 0.05


def test_hrv_low_cuts_volume_and_drops_quality(config):
    tired = demo_snapshot(as_of=AS_OF, scenario="tired")
    plan = RulePlanner(config).plan(tired, week_start=WEEK)
    assert plan.direction == "reduce"
    assert abs(plan.target_km - plan.baseline_km * 0.75) < 0.1
    assert not any(w.kind in ("intervals", "tempo") for w in plan.workouts)
    assert any("HRV" in r for r in plan.reasons)


def test_readiness_and_acwr_flags(config, snap):
    low = snap.model_copy(update={"hrv_status": "BALANCED", "training_readiness": 30})
    assert RulePlanner(config).plan(low, week_start=WEEK).direction == "reduce"
    hot = snap.model_copy(update={"hrv_status": "BALANCED", "training_readiness": 80, "acwr": 1.7})
    assert RulePlanner(config).plan(hot, week_start=WEEK).direction == "reduce"


def test_deload_every_fourth_week(config, snap):
    assert is_deload_week(DELOAD_WEEK) and not is_deload_week(WEEK)
    plan = RulePlanner(config).plan(snap, week_start=DELOAD_WEEK)
    assert plan.deload
    assert plan.target_km <= plan.baseline_km * 0.70 + 0.1


def test_structure_one_long_one_quality_rest_easy(config, snap):
    plan = RulePlanner(config).plan(snap, week_start=WEEK)
    kinds = [w.kind for w in plan.workouts]
    assert kinds.count("long") == 1
    assert kinds.count("intervals") + kinds.count("tempo") == 1
    assert quality_kind(WEEK) == "intervals" and "intervals" in kinds
    assert 2 <= kinds.count("easy") <= 3
    assert len(plan.workouts) == 7
    assert all(w.why for w in plan.workouts)
    assert all(plan.week_start <= w.date < plan.week_start + dt.timedelta(days=7) for w in plan.workouts)
    assert abs(sum(w.distance_km for w in plan.sessions) - plan.target_km) < 0.5


def test_respects_days_available_and_max_sessions(snap):
    cfg = Config(days_available=["tue", "thu", "sat"], max_sessions=3, long_run_day="sat", quality_day="tue")
    plan = RulePlanner(cfg).plan(snap, week_start=WEEK)
    assert len(plan.sessions) == 3
    assert {w.date.strftime("%a").lower() for w in plan.sessions} == {"tue", "thu", "sat"}
    assert next(w for w in plan.sessions if w.kind == "long").date.strftime("%a").lower() == "sat"


def test_max_weekly_km_cap(snap):
    cfg = Config(max_weekly_km=20)
    plan = RulePlanner(cfg).plan(snap, week_start=WEEK)
    assert plan.target_km == 20


def test_enforce_caps_scales_llm_output(config, snap):
    plan = RulePlanner(config).plan(snap, week_start=WEEK)
    for w in plan.sessions:
        w.distance_km *= 3
    capped = enforce_caps(plan, config, baseline_km=plan.baseline_km)
    assert capped.target_km <= plan.baseline_km * 1.10 + 0.1
    assert len(capped.sessions) <= config.max_sessions


def test_long_run_never_shorter_than_easy(config, snap):
    tiny = snap.model_copy(deep=True)
    tiny.weeks[-1].long_run_km = 2.0
    tiny.weeks[-1].km = 12.0
    plan = RulePlanner(config).plan(tiny, week_start=WEEK)
    long = next(w for w in plan.sessions if w.kind == "long")
    assert all(long.distance_km >= w.distance_km for w in plan.sessions if w.kind == "easy")
