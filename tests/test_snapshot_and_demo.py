import datetime as dt

from garmin_autocoach.cli import main
from garmin_autocoach.demo import demo_snapshot
from garmin_autocoach.snapshot import acwr, parse_hr_zones, runs_from_activities, weekly_stats

from conftest import AS_OF


def test_runs_from_activities_filters_and_converts():
    acts = [
        {"activityType": {"typeKey": "running"}, "startTimeLocal": "2026-11-03 18:27:56", "distance": 6011.4,
         "duration": 2292.3, "averageHR": 163.0, "aerobicTrainingEffect": 3.2, "anaerobicTrainingEffect": 0.0, "activityId": 1},
        {"activityType": {"typeKey": "cycling"}, "startTimeLocal": "2026-11-02 10:00:00", "distance": 30000, "duration": 3600},
        {"activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-01 10:00:00", "distance": 5000, "duration": 1800},
    ]
    runs = runs_from_activities(acts, since=AS_OF - dt.timedelta(days=35))
    assert len(runs) == 1
    assert runs[0].distance_km == 6.01 and runs[0].duration_min == 38.2 and runs[0].avg_hr == 163.0


def test_weekly_stats_and_acwr():
    snap = demo_snapshot(as_of=AS_OF)
    weeks = weekly_stats(snap.runs, AS_OF)
    assert len(weeks) == 4 and weeks[-1].week_start == dt.date(2026, 10, 26)  # last complete week
    assert snap.current_week and snap.current_week.week_start == dt.date(2026, 11, 2)
    assert weekly_stats(snap.runs, dt.date(2026, 11, 8))[-1].week_start == dt.date(2026, 11, 2)  # Sunday counts
    assert weeks[0].week_start < weeks[-1].week_start
    acute, chronic, ratio = acwr(snap.runs, AS_OF)
    assert chronic >= acute > 0 and ratio is not None


def test_parse_hr_zones_prefers_running():
    raw = [{"sport": "DEFAULT", "zone1Floor": 100, "zone2Floor": 120, "zone3Floor": 140, "zone4Floor": 160, "zone5Floor": 180, "maxHeartRateUsed": 200},
           {"sport": "RUNNING", "zone1Floor": 103, "zone2Floor": 123, "zone3Floor": 144, "zone4Floor": 164, "zone5Floor": 185, "maxHeartRateUsed": 205}]
    z = parse_hr_zones(raw)
    assert z.z2 == 123 and z.range(2) == (123, 143) and z.range(5) == (185, 205)
    assert parse_hr_zones(None) is None


def test_demo_snapshot_is_complete():
    snap = demo_snapshot(as_of=AS_OF)
    assert snap.source == "demo" and snap.hr_zones and snap.easy_pace_min_per_km
    assert all(w.km > 0 for w in snap.weeks)


def test_demo_cli_runs(capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Week of" in out and "DRY RUN" in out and "Adjustment memo" in out
    assert main(["demo", "--scenario", "tired"]) == 0
