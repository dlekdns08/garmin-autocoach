"""Weekly planners.

RulePlanner is the default: deterministic, explainable, no network.
LLMPlanner (optional, `pip install garmin-autocoach[llm]`) asks Claude for a plan
in the same schema and validates it with pydantic, then re-applies the hard caps
so an LLM can never schedule more than the rules allow.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Optional

from .config import DAYS, Config
from .models import AdjustmentMemo, AthleteSnapshot, WeekPlan, Workout

MAX_PROGRESSION = 0.10       # +10 %/week at most
RECOVERY_CUT = 0.25          # cut 25 % on red flags (20-30 % band)
DELOAD_CUT = 0.30            # every 4th week
READINESS_FLOOR = 40
ACWR_CEILING = 1.5
HRV_RED = {"LOW", "UNBALANCED", "POOR"}
LONG_RUN_SHARE = 0.32
QUALITY_SHARE = 0.22


def next_week_start(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    return today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=1)


def is_deload_week(week_start: dt.date) -> bool:
    """Deterministic deload: every ISO week divisible by 4."""
    return week_start.isocalendar().week % 4 == 0


def quality_kind(week_start: dt.date) -> str:
    return "intervals" if week_start.isocalendar().week % 2 == 0 else "tempo"


class RulePlanner:
    name = "rules"

    def __init__(self, config: Config):
        self.config = config

    # ---- volume --------------------------------------------------------- #
    def decide_volume(self, snap: AthleteSnapshot, memo: Optional[AdjustmentMemo], week_start: dt.date):
        cfg = self.config
        reasons: list[str] = []
        last = snap.last_week.km if snap.last_week else 0.0
        baseline = last if last > 0 else snap.avg_weekly_km
        if baseline <= 0:
            baseline = cfg.min_weekly_km
            reasons.append(f"No recent runs found: starting from the configured floor of {cfg.min_weekly_km:g} km.")
        else:
            reasons.append(f"Baseline = last week's volume ({baseline:.1f} km).")
            if snap.avg_weekly_km > baseline * 1.5:
                reasons.append(f"4-week average is {snap.avg_weekly_km:.1f} km, but progression anchors on last week to stay conservative.")

        direction = "progress"
        factor = 1.0 + MAX_PROGRESSION
        red_flags: list[str] = []
        if snap.hrv_status and snap.hrv_status.upper() in HRV_RED:
            red_flags.append(f"HRV status is {snap.hrv_status}")
        if snap.training_readiness is not None and snap.training_readiness < READINESS_FLOOR:
            red_flags.append(f"training readiness {snap.training_readiness} < {READINESS_FLOOR}")
        if snap.acwr is not None and snap.acwr > ACWR_CEILING:
            red_flags.append(f"acute:chronic load {snap.acwr:.2f} > {ACWR_CEILING}")

        if red_flags:
            direction, factor = "reduce", 1.0 - RECOVERY_CUT
            reasons.append("Recovery flags -> cut volume 25%: " + "; ".join(red_flags) + ".")
        elif memo is not None:
            direction, factor = memo.direction, memo.factor
            reasons.append(f"Review memo says {memo.direction} (x{memo.factor:.2f}): " + "; ".join(memo.reasons) + ".")
        else:
            reasons.append(f"No red flags -> progress at the 10% cap (x{factor:.2f}).")

        factor = min(factor, 1.0 + MAX_PROGRESSION)
        target = baseline * factor

        deload = is_deload_week(week_start)
        if deload:
            target = min(target, baseline * (1.0 - DELOAD_CUT))
            direction = "reduce" if direction != "reduce" else direction
            reasons.append(f"ISO week {week_start.isocalendar().week} is a deload week (every 4th): -30% from baseline.")

        if cfg.race_date and cfg.race_date - week_start <= dt.timedelta(days=14) and cfg.race_date >= week_start:
            target = min(target, baseline * 0.6)
            direction = "reduce"
            reasons.append(f"Race on {cfg.race_date} within two weeks -> taper to 60% of baseline.")

        if target > cfg.max_weekly_km:
            reasons.append(f"Capped at max_weekly_km={cfg.max_weekly_km:g}.")
            target = cfg.max_weekly_km
        target = max(target, min(cfg.min_weekly_km, cfg.max_weekly_km))
        return round(target, 1), round(baseline, 1), direction, deload, reasons

    # ---- structure ------------------------------------------------------ #
    def plan(self, snap: AthleteSnapshot, memo: Optional[AdjustmentMemo] = None,
             week_start: Optional[dt.date] = None) -> WeekPlan:
        cfg = self.config
        week_start = week_start or next_week_start(snap.as_of)
        target, baseline, direction, deload, reasons = self.decide_volume(snap, memo, week_start)
        pace = snap.easy_pace_min_per_km or 6.5
        reasons.append(f"Easy pace estimate {pace:.2f} min/km from recent Z1-Z2 runs; durations are time-based.")

        days = list(cfg.days_available)
        recent_sessions = snap.last_week.sessions if snap.last_week else 0
        n = min(cfg.max_sessions, len(days), max(3, min(5, recent_sessions + 1)))
        if direction == "reduce":
            n = min(n, max(2, recent_sessions))
        n = max(1, min(n, cfg.max_sessions, len(days)))
        reasons.append(f"{n} sessions (cap {cfg.max_sessions}, {len(days)} days available).")

        # Day assignment: long run, quality, then easy runs on the remaining days spaced out.
        def pick(pref: str, taken: set[str]) -> str:
            if pref in days and pref not in taken:
                return pref
            for d in sorted(days, key=lambda d: abs(DAYS.index(d) - DAYS.index(pref))):
                if d not in taken:
                    return d
            return days[0]

        taken: set[str] = set()
        long_day = pick(cfg.long_run_day, taken); taken.add(long_day)
        slots: list[tuple[str, str]] = [(long_day, "long")]
        include_quality = n >= 3 and direction != "reduce"
        if include_quality:
            q_day = pick(cfg.quality_day, taken); taken.add(q_day)
            slots.append((q_day, quality_kind(week_start)))
        elif n >= 3:
            reasons.append("Quality session dropped this week (recovery/deload): replaced by an easy run.")
        easy_needed = n - len(slots)
        remaining = [d for d in days if d not in taken]
        # choose easy days that are least adjacent to hard days
        def adjacency(d: str) -> int:
            return sum(1 for t in taken if abs(DAYS.index(d) - DAYS.index(t)) in (1, 6))
        for d in sorted(remaining, key=lambda d: (adjacency(d), DAYS.index(d)))[:easy_needed]:
            taken.add(d); slots.append((d, "easy"))

        # Volume split.
        long_km = round(min(target * LONG_RUN_SHARE, (snap.last_week.long_run_km if snap.last_week and snap.last_week.long_run_km else target * 0.3) * 1.1), 1)
        long_km = round(max(long_km, target * 0.2, target / n), 1)  # never shorter than an easy run
        quality_km = round(target * QUALITY_SHARE, 1) if include_quality else 0.0
        easy_total = max(target - long_km - quality_km, 0.0)
        easy_count = sum(1 for _, k in slots if k == "easy")
        easy_km = round(easy_total / easy_count, 1) if easy_count else 0.0

        workouts: list[Workout] = []
        for day, kind in slots:
            date = week_start + dt.timedelta(days=DAYS.index(day))
            workouts.append(self._build(kind, date, pace, long_km, quality_km, easy_km, snap, week_start))
        for d in DAYS:
            if d not in taken:
                workouts.append(Workout(date=week_start + dt.timedelta(days=DAYS.index(d)), kind="rest",
                                        name="Rest", why="Rest day: adaptation happens here."))
        workouts.sort(key=lambda w: w.date)
        return WeekPlan(week_start=week_start, target_km=target, baseline_km=baseline, direction=direction,
                        deload=deload, planner=self.name, reasons=reasons, workouts=workouts)

    def _build(self, kind: str, date: dt.date, pace: float, long_km: float, quality_km: float,
               easy_km: float, snap: AthleteSnapshot, week_start: dt.date) -> Workout:
        if kind == "long":
            mins = int(round(long_km * pace * 1.03 / 5) * 5)
            return Workout(date=date, kind="long", name=f"Long run {mins} min Z2", duration_min=mins,
                           distance_km=long_km, hr_zone=2,
                           why=f"Long run ~{long_km:.0f} km (<=32% of the week, <=+10% over last long run) builds aerobic durability.")
        if kind == "easy":
            mins = int(round(easy_km * pace / 5) * 5)
            mins = max(mins, 20)
            return Workout(date=date, kind="easy", name=f"Easy run {mins} min Z2", duration_min=mins,
                           distance_km=easy_km, hr_zone=2,
                           why="Easy Z2 run: most of the weekly volume should feel conversational.")
        if kind == "intervals":
            reps = 5 if quality_km < 7 else 6
            return Workout(date=date, kind="intervals", name=f"Intervals {reps}x3min Z4", duration_min=15 + reps * 5 + 10,
                           distance_km=quality_km, hr_zone=4, warmup_min=15, cooldown_min=10,
                           repeats=reps, rep_seconds=180, recovery_seconds=120,
                           why=f"Even ISO week -> VO2 intervals: {reps}x3 min at Z4 with 2 min jog, lifts top-end aerobic power.")
        # tempo
        tempo_min = max(15, min(30, int(quality_km * pace * 0.9 / 5) * 5))
        return Workout(date=date, kind="tempo", name=f"Tempo {tempo_min} min Z3", duration_min=15 + tempo_min + 10,
                       distance_km=quality_km, hr_zone=3, warmup_min=15, cooldown_min=10,
                       why=f"Odd ISO week -> tempo: {tempo_min} min continuous at Z3 (just under threshold) raises lactate threshold.")


def enforce_caps(plan: WeekPlan, config: Config, baseline_km: float) -> WeekPlan:
    """Hard safety net applied to any planner output (used for the LLM planner)."""
    sessions = plan.sessions
    allowed_days = {config.days_available[i] for i in range(len(config.days_available))}
    kept: list[Workout] = []
    for w in sessions:
        if DAYS[w.date.weekday()] not in allowed_days:
            continue
        kept.append(w)
    kept = kept[: config.max_sessions]
    max_km = min(config.max_weekly_km, baseline_km * (1 + MAX_PROGRESSION) if baseline_km > 0 else config.max_weekly_km)
    total = sum(w.distance_km for w in kept)
    if total > max_km and total > 0:
        scale = max_km / total
        for w in kept:
            w.distance_km = round(w.distance_km * scale, 1)
            w.duration_min = int(w.duration_min * scale)
        plan.reasons.append(f"Caps: scaled LLM volume {total:.1f} -> {max_km:.1f} km.")
    rest = [w for w in plan.workouts if w.kind == "rest"]
    plan.workouts = sorted(kept + rest, key=lambda w: w.date)
    plan.target_km = round(sum(w.distance_km for w in kept), 1)
    return plan


class LLMPlanner:
    """Asks Claude for a WeekPlan in the same schema; falls back to rules on any error."""

    name = "llm"

    SYSTEM = (
        "You are a conservative running coach. Produce exactly one week of running workouts "
        "as JSON matching the given schema. Rules you must respect: weekly volume may grow at most 10% "
        "over the baseline; cut 20-30% when HRV is LOW/UNBALANCED, readiness < 40, or acute:chronic > 1.5; "
        "one long run, at most one quality session (intervals or tempo), the rest easy Z2 runs and rest days; "
        "never exceed the session cap or schedule on unavailable days. Every workout needs a one-sentence 'why'. "
        "Dates must be ISO (YYYY-MM-DD) inside the requested week. Use kind 'rest' for rest days."
    )

    def __init__(self, config: Config, model: Optional[str] = None):
        self.config = config
        self.model = model or config.llm_model

    @staticmethod
    def available() -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def plan(self, snap: AthleteSnapshot, memo: Optional[AdjustmentMemo] = None,
             week_start: Optional[dt.date] = None) -> WeekPlan:
        import anthropic

        rules = RulePlanner(self.config)
        week_start = week_start or next_week_start(snap.as_of)
        target, baseline, direction, deload, reasons = rules.decide_volume(snap, memo, week_start)
        schema = WeekPlan.model_json_schema()
        schema.pop("$defs", None)
        payload = {
            "week_start": week_start.isoformat(),
            "suggested_target_km": target, "baseline_km": baseline, "direction": direction, "deload": deload,
            "volume_reasons": reasons,
            "config": self.config.model_dump(mode="json"),
            "snapshot": snap.model_dump(mode="json", exclude={"runs"}),
            "recent_runs": [r.model_dump(mode="json") for r in snap.runs[:12]],
            "memo": memo.model_dump(mode="json") if memo else None,
        }
        client = anthropic.Anthropic()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=self.SYSTEM,
                messages=[{"role": "user", "content": "Athlete context:\n" + json.dumps(payload, default=str)
                           + "\n\nReturn the WeekPlan JSON. Set planner to 'llm'."}],
                output_config={"format": {"type": "json_schema", "schema": _loose_schema(WeekPlan)}},
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("model refused")
            text = next(b.text for b in response.content if b.type == "text")
            plan = WeekPlan.model_validate_json(text)
        except Exception as exc:  # noqa: BLE001
            fallback = rules.plan(snap, memo, week_start)
            fallback.reasons.insert(0, f"LLM planner failed ({type(exc).__name__}: {exc}); used rules instead.")
            return fallback
        plan.planner = "llm"
        plan.week_start = week_start
        plan.baseline_km = baseline
        plan.deload = deload
        return enforce_caps(plan, self.config, baseline)


def _loose_schema(model) -> dict:
    """JSON schema for structured output: inline $defs, forbid extra keys."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                return resolve(defs[ref])
            out = {k: resolve(v) for k, v in node.items() if k not in ("default", "title")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def get_planner(name: str, config: Config):
    if name == "llm":
        if not LLMPlanner.available():
            raise RuntimeError("LLM planner needs ANTHROPIC_API_KEY and `pip install garmin-autocoach[llm]`.")
        return LLMPlanner(config)
    return RulePlanner(config)
