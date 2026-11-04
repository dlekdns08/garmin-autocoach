"""`autocoach` command line."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import CACHE_DIR, CONFIG_PATH, EXAMPLE_CONFIG, load_config
from .demo import demo_snapshot
from .garmin_writer import GarminWriter
from .models import AdjustmentMemo, AthleteSnapshot, WeekPlan
from .planner import RulePlanner, get_planner, next_week_start
from .review import load_latest_memo, load_plan, review_week, save_memo, save_plan
from .snapshot import build_snapshot, connect, last_complete_monday, save_snapshot

console = Console()


def _fmt_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def render_snapshot(snap: AthleteSnapshot) -> None:
    t = Table(title=f"Athlete snapshot ({snap.source}, as of {snap.as_of})", show_lines=False)
    t.add_column("Week"); t.add_column("km", justify="right"); t.add_column("Sessions", justify="right")
    t.add_column("Long run", justify="right"); t.add_column("Avg HR", justify="right")
    for w in snap.weeks:
        t.add_row(w.week_start.isoformat(), f"{w.km:.1f}", str(w.sessions), f"{w.long_run_km:.1f}",
                  f"{w.avg_hr:.0f}" if w.avg_hr else "-")
    if snap.current_week:
        c = snap.current_week
        t.add_row(f"{c.week_start} (so far)", f"{c.km:.1f}", str(c.sessions), f"{c.long_run_km:.1f}",
                  f"{c.avg_hr:.0f}" if c.avg_hr else "-", style="dim")
    console.print(t)
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold"); body.add_column()
    body.add_row("HRV", f"{snap.hrv_status or '-'} (last night {snap.hrv_last_night or '-'} / weekly {snap.hrv_weekly_avg or '-'} ms)")
    body.add_row("Sleep", f"{snap.sleep_avg_hours or '-'} h avg, score {snap.sleep_score_avg or '-'}")
    body.add_row("Readiness", f"{snap.training_readiness if snap.training_readiness is not None else '-'} ({snap.readiness_level or '-'})")
    body.add_row("ACWR", f"{snap.acwr if snap.acwr is not None else '-'} (7d {snap.acute_km_7d} km / 28d {snap.chronic_km_28d} km)")
    body.add_row("VO2max", f"{snap.vo2max or '-'}  status: {snap.training_status or '-'}")
    body.add_row("Race predictions", ", ".join(f"{k} {_fmt_time(v)}" for k, v in snap.race_prediction_s.items()) or "-")
    if snap.hr_zones:
        z = snap.hr_zones
        body.add_row("HR zones", f"Z1 {z.z1}+  Z2 {z.z2}+  Z3 {z.z3}+  Z4 {z.z4}+  Z5 {z.z5}+  (max {z.max_hr})")
    body.add_row("Easy pace", f"{snap.easy_pace_min_per_km or '-'} min/km")
    console.print(Panel(body, title="Readiness"))


def render_plan(plan: WeekPlan) -> None:
    flag = " (deload)" if plan.deload else ""
    t = Table(title=f"Week of {plan.week_start}: {plan.target_km:.1f} km, {plan.direction}{flag}, planner={plan.planner}")
    t.add_column("Date"); t.add_column("Day"); t.add_column("Workout"); t.add_column("Min", justify="right")
    t.add_column("~km", justify="right"); t.add_column("Why", max_width=70)
    for w in plan.workouts:
        style = "dim" if w.kind == "rest" else ""
        t.add_row(w.date.isoformat(), w.date.strftime("%a"), w.name,
                  str(w.duration_min) if w.duration_min else "-",
                  f"{w.distance_km:.1f}" if w.distance_km else "-", w.why, style=style)
    console.print(t)
    console.print(Panel("\n".join(f"- {r}" for r in plan.reasons), title="Why this volume"))


def render_memo(memo: AdjustmentMemo) -> None:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="bold"); body.add_column()
    body.add_row("Week", memo.week_start.isoformat())
    body.add_row("Completion", f"{memo.completed_sessions}/{memo.planned_sessions} ({memo.completion:.0%}), {memo.actual_km} / {memo.planned_km} km")
    body.add_row("HR drift", f"{memo.hr_drift_pct:+.1f}%" if memo.hr_drift_pct is not None else "-")
    body.add_row("HRV", f"{memo.hrv_status or '-'} / {memo.hrv_trend or '-'}")
    body.add_row("Next week", f"[bold]{memo.direction}[/bold] x{memo.factor:.2f}")
    body.add_row("Because", "\n".join(memo.reasons))
    console.print(Panel(body, title="Adjustment memo"))


def _snapshot(args, config) -> AthleteSnapshot:
    if getattr(args, "demo", False):
        return demo_snapshot(scenario=getattr(args, "scenario", "steady"))
    with console.status("Reading Garmin..."):
        client = connect(config)
        snap = build_snapshot(client, refresh=getattr(args, "refresh", False))
    save_snapshot(snap)
    return snap


def cmd_snapshot(args) -> int:
    config = load_config()
    snap = _snapshot(args, config)
    if args.json:
        print(snap.model_dump_json(indent=2))
    else:
        render_snapshot(snap)
    return 0


def cmd_plan(args) -> int:
    config = load_config()
    snap = _snapshot(args, config)
    week_start = next_week_start(snap.as_of) if args.week == "next" else dt.date.fromisoformat(args.week)
    memo = None if args.no_memo else load_latest_memo(before=week_start)
    planner = get_planner(args.planner, config)
    with console.status(f"Planning with {planner.name}..."):
        plan = planner.plan(snap, memo=memo, week_start=week_start)
    render_plan(plan)
    out = Path(args.out)
    out.write_text(plan.model_dump_json(indent=2))
    save_plan(plan)
    console.print(f"Plan written to [bold]{out}[/bold]. Nothing was sent to Garmin. "
                  f"Next: [bold]autocoach apply --plan {out}[/bold] (dry run) then add [bold]--apply[/bold].")
    return 0


def cmd_apply(args) -> int:
    config = load_config()
    plan = WeekPlan.model_validate_json(Path(args.plan).read_text())
    dry = not args.apply
    zones = None
    client = None
    if args.demo:
        zones = demo_snapshot().hr_zones
        if not dry:
            console.print("[red]--demo cannot be combined with --apply.[/red]")
            return 2
    else:
        from .snapshot import load_snapshot
        cached = load_snapshot(dt.date.today())
        zones = cached.hr_zones if cached else None
        if not dry:
            client = connect(config)
    writer = GarminWriter(client=client, dry_run=dry, console=console)
    render_plan(plan)
    if dry:
        console.print(Panel("[yellow]DRY RUN[/yellow] - showing what would be uploaded. Re-run with [bold]--apply[/bold] to write to Garmin.", title="Safety"))
    try:
        record = writer.apply(plan, zones=zones, max_sessions=config.max_sessions)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    if not dry:
        console.print(f"Applied {len(record.items)} workouts. Undo with: autocoach undo --week {plan.week_key}")
    return 0


def cmd_undo(args) -> int:
    config = load_config()
    week = dt.date.fromisoformat(args.week)
    dry = not args.apply
    client = None if dry else connect(config)
    writer = GarminWriter(client=client, dry_run=dry, console=console)
    n = writer.undo(week)
    if dry:
        console.print("[yellow]DRY RUN[/yellow] - add --apply to actually delete/unschedule.")
    else:
        console.print(f"Removed {n} workouts.")
    return 0


def cmd_review(args) -> int:
    config = load_config()
    snap = _snapshot(args, config)
    week_start = last_complete_monday(snap.as_of) if args.week == "last" else dt.date.fromisoformat(args.week)
    plan = load_plan(week_start)
    if plan is None and args.demo:
        plan = RulePlanner(config).plan(demo_snapshot(as_of=week_start - dt.timedelta(days=1)), week_start=week_start)
    if plan is None:
        console.print(f"[red]No saved plan for week {week_start}. Run `autocoach plan --week {week_start}` first.[/red]")
        return 1
    memo = review_week(plan, snap)
    render_memo(memo)
    if not args.demo:
        save_memo(memo)
        console.print("Memo saved; the next `autocoach plan` will use it.")
    return 0


def cmd_demo(args) -> int:
    config = load_config()
    console.rule("[bold]1. snapshot (synthetic athlete)")
    snap = demo_snapshot(scenario=args.scenario)
    render_snapshot(snap)
    console.rule("[bold]2. plan")
    plan = RulePlanner(config).plan(snap)
    render_plan(plan)
    console.rule("[bold]3. apply (dry run)")
    GarminWriter(dry_run=True, console=console).apply(plan, zones=snap.hr_zones, max_sessions=config.max_sessions)
    console.rule("[bold]4. review of a simulated previous week")
    prev_week = last_complete_monday(snap.as_of)
    prev_plan = RulePlanner(config).plan(demo_snapshot(as_of=prev_week - dt.timedelta(days=1), scenario=args.scenario), week_start=prev_week)
    memo = review_week(prev_plan, snap)
    render_memo(memo)
    console.rule("[bold]5. next plan with the memo applied")
    render_plan(RulePlanner(config).plan(snap, memo=memo))
    return 0


def cmd_config(args) -> int:
    if args.init:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            console.print(f"{CONFIG_PATH} already exists.")
        else:
            CONFIG_PATH.write_text(EXAMPLE_CONFIG)
            console.print(f"Wrote {CONFIG_PATH}")
        return 0
    console.print(f"config: {CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'missing, using defaults'})")
    console.print(f"cache:  {CACHE_DIR}")
    print(json.dumps(load_config().model_dump(mode="json"), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocoach", description="An agent that writes to your watch, and listens back.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--demo", action="store_true", help="use a synthetic athlete (no Garmin)")
        sp.add_argument("--scenario", choices=["steady", "tired", "fresh"], default="steady")
        sp.add_argument("--refresh", action="store_true", help="ignore today's raw cache")

    s = sub.add_parser("snapshot", help="read the last 4 weeks from Garmin"); common(s)
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("plan", help="build next week's plan (never writes to Garmin)"); common(s)
    s.add_argument("--week", default="next", help="'next' or a Monday YYYY-MM-DD")
    s.add_argument("--planner", choices=["rules", "llm"], default="rules")
    s.add_argument("--out", default="plan.json")
    s.add_argument("--no-memo", action="store_true", help="ignore the last review memo")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("apply", help="upload + schedule a plan (dry run unless --apply)")
    s.add_argument("--plan", required=True)
    s.add_argument("--apply", action="store_true", help="actually write to Garmin")
    s.add_argument("--demo", action="store_true")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("undo", help="delete/unschedule an applied week")
    s.add_argument("--week", required=True, help="Monday YYYY-MM-DD")
    s.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("review", help="compare last week's plan with reality -> memo"); common(s)
    s.add_argument("--week", default="last", help="'last' or a Monday YYYY-MM-DD")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("demo", help="run the whole loop on a synthetic athlete")
    s.add_argument("--scenario", choices=["steady", "tired", "fresh"], default="steady")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("config", help="show or create the config file")
    s.add_argument("--init", action="store_true")
    s.set_defaults(fn=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
