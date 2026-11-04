# garmin-autocoach

**An agent that writes to your watch, and listens back.**

`autocoach` reads your Garmin training data, writes next week's running plan,
uploads the workouts and schedules them on your Garmin calendar. The following
week it reads how your body responded (completed sessions, HR drift, HRV trend)
and adjusts the next plan. One command per week, dry-run by default, every
decision explained in plain English.

```
            ┌───────────────────────────────────────────────────────────┐
            │                                                           │
            ▼                                                           │
   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────┴──────┐
   │   snapshot   │───▶│     plan     │───▶│    apply     │───▶│    train     │
   │ 4 wk of runs │    │ rules or LLM │    │ upload +     │    │ (you, on the │
   │ HRV, sleep,  │    │ +10% cap,    │    │ schedule on  │    │  watch)      │
   │ readiness,   │    │ deload, cuts │    │ the calendar │    │              │
   │ ACWR, zones  │    │ every 'why'  │    │ dry-run first│    │              │
   └──────────────┘    └──────────────┘    └──────────────┘    └───────┬──────┘
            ▲                                                           │
            │                        ┌──────────────┐                   │
            └────────────────────────│    review    │◀──────────────────┘
                                     │ done? HR     │
                                     │ drift? HRV?  │
                                     │ -> memo      │
                                     └──────────────┘
```

## Why

Garmin already has adaptive coaching, but it is a black box: you cannot see the
rule that cut your long run, you cannot tune it, and you cannot swap in your own
planner. `garmin-autocoach` is the opposite: a small, auditable loop you own.

- **Safe by default.** `plan` never touches Garmin. `apply` is a dry run unless
  you pass `--apply`. Every write is recorded so `undo` can remove it.
- **Explainable.** Every workout carries a `why`; every weekly volume decision
  lists its reasons. The default planner is pure rules, no API key, no network
  beyond Garmin.
- **Closes the loop.** `review` turns last week's reality into an adjustment
  memo that the next `plan` consumes.
- **One command.** `autocoach plan` is all you run on Sunday evening.

## Install

Requires Python 3.12 and a Garmin Connect account with stored tokens
(`~/.garminconnect`, created by [garminconnect](https://github.com/cyberjunky/python-garminconnect)
or [garth](https://github.com/matin/garth); `GARMINTOKENS` overrides the path).

```bash
uv tool install garmin-autocoach          # or: pip install garmin-autocoach
autocoach demo                            # whole loop on a synthetic athlete, no Garmin
autocoach config --init                   # writes ~/.config/garmin-autocoach/config.toml
```

From source:

```bash
git clone https://github.com/dlekdns08/garmin-autocoach
cd garmin-autocoach && uv sync && uv run autocoach demo
```

## The weekly loop

```bash
autocoach snapshot                 # what Garmin knows about the last 4 weeks
autocoach review                   # last week's plan vs reality -> memo (after the first cycle)
autocoach plan                     # next week's plan -> plan.json (nothing sent)
autocoach apply --plan plan.json   # DRY RUN: prints every payload it would send
autocoach apply --plan plan.json --apply   # really uploads + schedules
autocoach undo --week 2026-11-09 --apply   # removes everything that apply wrote
```

### Example `autocoach plan` output

```
                        Week of 2026-11-09: 33.0 km, progress, planner=rules
┏━━━━━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Date       ┃ Day ┃ Workout             ┃ Min ┃  ~km ┃ Why                                        ┃
┡━━━━━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026-11-09 │ Mon │ Easy run 30 min Z2  │  30 │  5.0 │ Easy Z2 run: most of the weekly volume     │
│            │     │                     │     │      │ should feel conversational.                │
│ 2026-11-10 │ Tue │ Easy run 30 min Z2  │  30 │  5.0 │ Easy Z2 run: most of the weekly volume     │
│            │     │                     │     │      │ should feel conversational.                │
│ 2026-11-11 │ Wed │ Intervals 6x3min Z4 │  55 │  7.3 │ Even ISO week -> VO2 intervals: 6x3 min at │
│            │     │                     │     │      │ Z4 with 2 min jog, lifts top-end aerobic   │
│            │     │                     │     │      │ power.                                     │
│ 2026-11-12 │ Thu │ Rest                │   - │    - │ Rest day: adaptation happens here.         │
│ 2026-11-13 │ Fri │ Easy run 30 min Z2  │  30 │  5.0 │ Easy Z2 run: most of the weekly volume     │
│            │     │                     │     │      │ should feel conversational.                │
│ 2026-11-14 │ Sat │ Rest                │   - │    - │ Rest day: adaptation happens here.         │
│ 2026-11-15 │ Sun │ Long run 65 min Z2  │  65 │ 10.6 │ Long run ~11 km (<=32% of the week, <=+10% │
│            │     │                     │     │      │ over last long run) builds aerobic         │
│            │     │                     │     │      │ durability.                                │
└────────────┴─────┴─────────────────────┴─────┴──────┴────────────────────────────────────────────┘
╭──────────────────────────────────────── Why this volume ─────────────────────────────────────────╮
│ - Baseline = last week's volume (30.0 km).                                                       │
│ - No red flags -> progress at the 10% cap (x1.10).                                               │
│ - Easy pace estimate 6.14 min/km from recent Z1-Z2 runs; durations are time-based.               │
│ - 5 sessions (cap 5, 7 days available).                                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
Plan written to plan.json. Nothing was sent to Garmin.
```

When the body says no, the plan says so too (`autocoach demo --scenario tired`):

```
╭──────────────────────────────────────── Why this volume ─────────────────────────────────────────╮
│ - Baseline = last week's volume (30.0 km).                                                       │
│ - Recovery flags -> cut volume 25%: HRV status is LOW; training readiness 34 < 40.               │
│ - Easy pace estimate 6.14 min/km from recent Z1-Z2 runs; durations are time-based.               │
│ - 4 sessions (cap 5, 7 days available).                                                          │
│ - Quality session dropped this week (recovery/deload): replaced by an easy run.                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## Planning rules (the default planner)

| Rule | Behaviour |
|---|---|
| Progression | next week <= last week x 1.10, always |
| Recovery cut | HRV `LOW`/`UNBALANCED`/`POOR`, readiness < 40, or ACWR (7d / 28d-weekly) > 1.5 -> -25 % and no quality session |
| Deload | every 4th ISO week -> -30 % from baseline |
| Taper | race date within 14 days -> 60 % of baseline |
| Structure | 1 long run (<=32 % of the week, <=+10 % over last long run), 1 quality session (intervals on even ISO weeks, tempo on odd), 2-3 easy Z2 runs, rest days |
| Memo | `review` can say `progress` (x1.10), `hold` (x1.00) or `reduce` (x0.75-0.85); red flags in the snapshot override the memo |
| Caps | `max_sessions`, `days_available`, `max_weekly_km` from config are hard limits for every planner |

Durations are time-based in heart-rate zones (your Garmin zones, as bpm ranges)
so the watch coaches you live; the `~km` column is an estimate from your recent
easy pace.

### Review logic

`autocoach review` compares the saved plan for last week with the runs Garmin
recorded and writes an adjustment memo:

- completion < 60 % -> `reduce` (x0.85)
- HRV status `LOW`/`POOR`, or last night < 92 % of the weekly average -> `reduce` (x0.75)
- HR drift > +5 % at the same speed vs the previous weeks -> `hold`
- everything done and HRV `BALANCED` -> `progress` (x1.10)

## Safety

- `plan` and `snapshot` and `review` are read-only. Nothing is written to Garmin.
- `apply` is a **dry run by default**; it prints every payload it would upload.
  Only `--apply` writes.
- Every applied week is recorded in `~/.cache/garmin-autocoach/applied/<week>.json`
  (workout ids and calendar entry ids). `autocoach undo --week <monday> --apply`
  unschedules and deletes exactly those. `apply` refuses to run twice for the same
  week until you `undo`.
- The writer refuses a plan with more sessions than `max_sessions`, whoever
  produced it. LLM output is re-validated with pydantic and passed through the
  same caps (volume <= baseline x 1.10, `days_available`, `max_sessions`).
- Workouts are named `[autocoach] ...` so they are easy to spot and delete in
  Garmin Connect.
- Raw Garmin responses are cached under `~/.cache/garmin-autocoach/raw/<date>/`
  for the day; `--refresh` bypasses the cache. Nothing leaves your machine except
  the Garmin calls (and, only with `--planner llm`, the anonymised snapshot sent
  to Anthropic).

## Config

`~/.config/garmin-autocoach/config.toml` (`autocoach config --init` writes this):

```toml
[athlete]
days_available = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
max_sessions = 5          # hard cap, the planner never schedules more
long_run_day = "sun"
quality_day = "wed"
max_weekly_km = 80        # hard cap on planned volume
min_weekly_km = 10        # floor when starting from nothing

[race]
# date = "2027-03-15"
# distance_km = 42.195

[garmin]
# tokens = "~/.garminconnect"   # or set GARMINTOKENS

[llm]
# model = "claude-sonnet-5-5"
```

`AUTOCOACH_CONFIG` and `AUTOCOACH_CACHE` override the file and cache locations.

## Optional LLM planner

```bash
uv tool install "garmin-autocoach[llm]"
export ANTHROPIC_API_KEY=...
autocoach plan --planner llm
```

The LLM planner receives the same snapshot and memo, must return the same
`WeekPlan` schema (enforced with structured output and pydantic), and its result
is clipped by the same caps as the rule planner. If the call fails for any
reason the rule planner's plan is used and the reason is listed first. Without
`--planner llm` the `anthropic` package is never imported.

## What it reads from Garmin

`get_activities`, `get_hrv_data`, `get_sleep_data` (7 nights),
`get_training_readiness`, `get_training_status` (VO2max), `get_race_predictions`
and the heart-rate-zone profile. Each call is wrapped: a failure leaves that
field empty rather than aborting the snapshot.

## Development

```bash
uv sync --extra llm
uv run pytest
uv run autocoach demo --scenario tired
```

## Disclaimer

This is a hobby project and not medical advice. The rules are a reasonable
default for healthy recreational runners; they do not know about injuries,
illness, heat, altitude or anything the watch cannot see. Read every plan before
you `--apply` it, and talk to a professional if in doubt.

## License

MIT, copyright 2026 dlekdns08.
