import datetime as dt
from pathlib import Path

from garmin_autocoach.garmin_writer import GarminWriter, load_applied, to_payload
from garmin_autocoach.models import HRZones, Workout
from garmin_autocoach.planner import RulePlanner

ZONES = HRZones(z1=103, z2=123, z3=144, z4=164, z5=185, max_hr=205)
DAY = dt.date(2026, 11, 11)


def _walk(step):
    yield step
    for s in step.get("workoutSteps", []):
        yield from _walk(s)


def test_easy_run_payload_shape():
    w = Workout(date=DAY, kind="easy", name="Easy 40", duration_min=40, hr_zone=2, why="x")
    p = to_payload(w, ZONES)
    assert p["sportType"] == {"sportTypeId": 1, "sportTypeKey": "running"}
    seg = p["workoutSegments"][0]
    assert seg["segmentOrder"] == 1 and seg["sportType"]["sportTypeKey"] == "running"
    steps = seg["workoutSteps"]
    assert len(steps) == 1
    s = steps[0]
    assert s["type"] == "ExecutableStepDTO" and s["stepOrder"] == 1
    assert s["stepType"] == {"stepTypeId": 3, "stepTypeKey": "interval"}
    assert s["endCondition"] == {"conditionTypeId": 2, "conditionTypeKey": "time"}
    assert s["endConditionValue"] == 2400.0
    assert s["targetType"] == {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}
    assert (s["targetValueOne"], s["targetValueTwo"]) == (123.0, 143.0)


def test_named_zone_when_no_zone_config():
    w = Workout(date=DAY, kind="long", name="Long", duration_min=90, hr_zone=2, why="x")
    s = to_payload(w, None)["workoutSegments"][0]["workoutSteps"][0]
    assert s["zoneNumber"] == 2 and "targetValueOne" not in s


def test_interval_payload_matches_reference_structure():
    w = Workout(date=DAY, kind="intervals", name="Int", duration_min=55, hr_zone=4, warmup_min=15,
                cooldown_min=10, repeats=6, rep_seconds=180, recovery_seconds=120, why="x")
    steps = to_payload(w, ZONES)["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup" and steps[0]["endConditionValue"] == 900.0
    rep = steps[1]
    assert rep["type"] == "RepeatGroupDTO" and rep["stepType"] == {"stepTypeId": 6, "stepTypeKey": "repeat"}
    assert rep["numberOfIterations"] == 6 and rep["smartRepeat"] is False
    assert rep["endCondition"] == {"conditionTypeId": 7, "conditionTypeKey": "iterations"}
    assert rep["endConditionValue"] == 6.0
    work, jog = rep["workoutSteps"]
    assert (work["stepOrder"], jog["stepOrder"]) == (3, 4)
    assert work["stepType"]["stepTypeKey"] == "interval" and work["endConditionValue"] == 180.0
    assert work["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert jog["stepType"]["stepTypeKey"] == "recovery" and jog["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert steps[2]["stepType"]["stepTypeKey"] == "cooldown"


def test_tempo_payload():
    w = Workout(date=DAY, kind="tempo", name="Tempo", duration_min=50, hr_zone=3, warmup_min=15, cooldown_min=10, why="x")
    steps = to_payload(w, ZONES)["workoutSegments"][0]["workoutSteps"]
    assert [s["stepType"]["stepTypeKey"] for s in steps] == ["warmup", "interval", "cooldown"]
    assert steps[1]["endConditionValue"] == 25 * 60.0
    assert (steps[1]["targetValueOne"], steps[1]["targetValueTwo"]) == (144.0, 163.0)


def test_rest_has_no_payload():
    assert to_payload(Workout(date=DAY, kind="rest", name="Rest", why="x")) is None


def test_every_step_has_required_keys(snap, config):
    plan = RulePlanner(config).plan(snap)
    for w in plan.sessions:
        for step in _walk({"workoutSteps": to_payload(w, snap.hr_zones)["workoutSegments"][0]["workoutSteps"]}):
            if "stepOrder" in step:
                assert {"type", "stepOrder", "stepType", "endCondition"} <= step.keys()


class FakeClient:
    def __init__(self):
        self.uploaded, self.scheduled, self.deleted, self.unscheduled = [], [], [], []

    def upload_workout(self, payload):
        self.uploaded.append(payload)
        return {"workoutId": 100 + len(self.uploaded), "workoutName": payload["workoutName"]}

    def schedule_workout(self, wid, date):
        self.scheduled.append((wid, date))
        return {"workoutScheduleId": 900 + wid}

    def unschedule_workout(self, sid):
        self.unscheduled.append(sid)

    def delete_workout(self, wid):
        self.deleted.append(wid)


def test_dry_run_never_calls_client(tmp_path: Path, snap, config):
    plan = RulePlanner(config).plan(snap)
    fake = FakeClient()
    rec = GarminWriter(client=fake, dry_run=True, cache_root=tmp_path).apply(plan, snap.hr_zones)
    assert fake.uploaded == [] and fake.scheduled == [] and rec.items == []
    assert load_applied(plan.week_start, tmp_path) is None


def test_apply_records_ids_and_undo_removes(tmp_path: Path, snap, config):
    plan = RulePlanner(config).plan(snap)
    fake = FakeClient()
    writer = GarminWriter(client=fake, dry_run=False, cache_root=tmp_path)
    rec = writer.apply(plan, snap.hr_zones, max_sessions=config.max_sessions)
    assert len(rec.items) == len(plan.sessions) == len(fake.uploaded) == len(fake.scheduled)
    assert all(p["workoutName"].startswith("[autocoach]") for p in fake.uploaded)
    assert load_applied(plan.week_start, tmp_path) is not None
    n = writer.undo(plan.week_start)
    assert n == len(rec.items)
    assert sorted(fake.deleted) == sorted(i.workout_id for i in rec.items)
    assert sorted(fake.unscheduled) == sorted(i.schedule_id for i in rec.items)
    assert load_applied(plan.week_start, tmp_path) is None


def test_refuses_more_sessions_than_config(tmp_path: Path, snap, config):
    plan = RulePlanner(config).plan(snap)
    import pytest
    with pytest.raises(RuntimeError):
        GarminWriter(client=FakeClient(), dry_run=False, cache_root=tmp_path).apply(plan, max_sessions=1)
