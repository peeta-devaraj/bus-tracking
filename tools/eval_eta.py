"""Evaluation: the live estimator vs a learned model, with a control.

What it does
------------
For each route, it simulates weeks of buses (tools/traffic.py), trains on the
first 14 days, and tests on the next 7, which training never saw. Once a
simulated minute per bus, every estimator predicts the arrival at every stop
ahead, and each is graded against when the bus actually got there.

Three estimators
----------------
  naive     what the API runs today: remaining distance / the bus's speed
  average   one learned average pace per direction, stops and halts included,
            nothing else
  learned   learned pace per 200 m of road, per direction, per time of day

Comparing all three separates two different effects. naive -> average is what
you gain just by accounting for time spent stopped. average -> learned is what
you gain by knowing *where* and *when* the road is slow.

The control
-----------
Each route also runs on `uniform` traffic: the same average speed and
randomness, but nothing that depends on place or time of day. On it, the
average -> learned gain should be about zero, because there is no where-or-when
to know. The first version of this evaluation compared only naive and learned,
and the control showed large gains on uniform traffic too -- which is how it
was discovered that most of the apparent gain was the live estimator ignoring
stop time, not the model learning structure. Hence the middle estimator.

Fairness rules
--------------
  * naive is the production code path: shared.eta.estimate with the same speed
    choice (reported, then rolling average, then default) and range.
  * average and learned fall back to that same naive speed wherever they have
    no data, so they cannot do worse merely by lacking coverage.
  * Ranges of the learned models are calibrated on the last two TRAINING days.
    Test days never tune anything.
  * Every range is rounded to whole minutes the way the API displays it, and
    graded with the same half-minute reading tolerance.
  * Estimators see GPS-noisy positions; ground truth uses noiseless ones.

The numbers are about simulated traffic. See the warning in tools/traffic.py.

Usage:
    python tools/eval_eta.py                  # full run, prints tables
    python tools/eval_eta.py --write-report   # also writes docs/eta-evaluation.md
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from shared.eta import (  # noqa: E402
    RANGE_HIGH,
    RANGE_LOW,
    average_speed_kmh_from_history,
    choose_speed_kmh,
    estimate,
)
from shared.learned_eta import (  # noqa: E402
    IST,
    TERMINUS_ZONE_M,
    SegmentModel,
    TrackPoint,
    time_band,
)
from traffic import STRUCTURED, UNIFORM, Fix, RouteProfile, load_profile, synthesize  # noqa: E402

REPORT_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "eta-evaluation.md")

FIRST_DAY = date(2026, 8, 3)  # a Monday; weekday structure is not modelled
QUERY_EVERY = 6               # every sixth fix, i.e. once a simulated minute
MIN_REMAINING_M = 150.0       # closer than this both just say "arriving"
READING_TOLERANCE_S = 30.0    # "6-11 min" is read as covering 11 min 30 s
CALIBRATION_DAYS = 2
PEAK = {"morning_peak", "evening_peak"}
HORIZONS = ((0, 5), (5, 10), (10, 20), (20, 999))


@dataclass
class Graded:
    horizon_min: float
    band: str
    naive_err_s: float
    average_err_s: float
    learned_err_s: float
    naive_hit: bool
    average_hit: bool
    learned_hit: bool
    naive_width_min: int
    average_width_min: int
    learned_width_min: int
    learned_source: str


def track_points(fixes: list[Fix]) -> list[TrackPoint]:
    """What the model learns from: observed positions and inferred direction."""
    return [TrackPoint(f.ts, f.along_m, f.direction) for f in fixes]


def true_arrival(fixes: list[Fix], start: int, target_m: float, direction: int) -> float | None:
    """When the bus really reached target_m, scanning forward from fix `start`.

    Uses noiseless positions and true direction. Linear interpolation between
    samples. None if the bus turned round or service ended first.
    """
    for k in range(start, len(fixes) - 1):
        p, q = fixes[k], fixes[k + 1]
        if q.true_direction != direction:
            return None
        a = (p.true_along_m - target_m) * direction
        b = (q.true_along_m - target_m) * direction
        if a <= 0 <= b:
            return p.ts if b == a else p.ts + (q.ts - p.ts) * (-a / (b - a))
    return None


def history_for(fixes: list[Fix], i: int) -> list[dict]:
    """The API's rolling-average input: up to 20 recent fixes, newest first."""
    return [
        {"ts": f.ts, "lat": f.lat, "lon": f.lon}
        for f in reversed(fixes[max(0, i - 19): i + 1])
    ]


def displayed(low_s: float, high_s: float) -> tuple[int, int]:
    """Round a range to whole minutes the way the API shows it."""
    low = max(0, int(low_s / 60.0))
    high = max(low + 1, int(round(high_s / 60.0)))
    return low, high


def hit(low_min: int, high_min: int, actual_s: float) -> bool:
    return low_min * 60 - READING_TOLERANCE_S <= actual_s <= high_min * 60 + READING_TOLERANCE_S


def queries(profile: RouteProfile, fixes: list[Fix]):
    """(fix index, stop, actual seconds) for every question worth asking.

    Skipped, and the same for both estimators:
      * unknown direction, or direction inference currently wrong -- neither
        estimator can be right, so it says nothing about which is better
      * the bus is inside a terminus zone: whether it is about to leave
        depends on a timetable neither estimator has
      * the stop is too close to be worth predicting, or never reached
    """
    L = profile.length_m
    for i in range(0, len(fixes), QUERY_EVERY):
        f = fixes[i]
        if f.direction == 0 or f.direction != f.true_direction:
            continue
        if f.true_along_m < TERMINUS_ZONE_M or f.true_along_m > L - TERMINUS_ZONE_M:
            continue
        for stop in profile.stops:
            remaining = (stop.along_m - f.along_m) * f.direction
            if remaining < MIN_REMAINING_M:
                continue
            arrived = true_arrival(fixes, i, stop.along_m, f.true_direction)
            if arrived is None:
                continue
            yield i, stop, arrived - f.ts


def naive_prediction(fixes: list[Fix], i: int, stop_along_m: float) -> tuple[float, int, int] | None:
    f = fixes[i]
    history = history_for(fixes, i)
    average = average_speed_kmh_from_history(history)
    eta = estimate(
        bus_id="eval", label="eval", bus_along_m=f.along_m, stop_along_m=stop_along_m,
        age_s=0.0, confidence="live", reported_speed_mps=f.speed_mps,
        average_speed_kmh=average, direction=f.direction,
    )
    if eta is None:
        return None
    speed_kmh, _ = choose_speed_kmh(f.speed_mps, average)
    point_s = eta.distance_m / (speed_kmh / 3.6)
    return point_s, eta.low_min, eta.high_min


def learned_prediction(model: SegmentModel, fixes: list[Fix], i: int, stop_along_m: float):
    f = fixes[i]
    speed_kmh, _ = choose_speed_kmh(f.speed_mps, average_speed_kmh_from_history(history_for(fixes, i)))
    fallback_pace = 3.6 / speed_kmh
    pred = model.predict(f.direction, f.along_m, stop_along_m, f.ts, fallback_pace)
    if pred is None:
        return None
    low, high = displayed(pred.low_seconds, pred.high_seconds)
    return pred.seconds, low, high, pred.source


def new_model(profile: RouteProfile, kind: str) -> SegmentModel:
    if kind == "average":
        # One bin covering the whole route and no time-of-day split: a single
        # average pace per direction, stops and halts included.
        return SegmentModel(profile.length_m, bin_m=profile.length_m + 1.0, use_bands=False)
    return SegmentModel(profile.length_m)


def train(profile: RouteProfile, days: dict[date, list[list[Fix]]], kind: str = "learned") -> SegmentModel:
    """Learn on training days, calibrating on the last CALIBRATION_DAYS of them."""
    ordered = sorted(days)
    fit_days, hold_days = ordered[:-CALIBRATION_DAYS], ordered[-CALIBRATION_DAYS:]

    provisional = new_model(profile, kind)
    for d in fit_days:
        for fixes in days[d]:
            provisional.learn_track(track_points(fixes))

    ratios = []
    for d in hold_days:
        for fixes in days[d]:
            for i, stop, actual_s in queries(profile, fixes):
                f = fixes[i]
                speed_kmh, _ = choose_speed_kmh(f.speed_mps, average_speed_kmh_from_history(history_for(fixes, i)))
                raw = provisional.raw_seconds(f.direction, f.along_m, stop.along_m, f.ts, 3.6 / speed_kmh)
                if raw is None:
                    continue
                seconds, share = raw
                if share >= 0.9 and seconds > 0:
                    ratios.append(actual_s / seconds)

    final = new_model(profile, kind)
    for d in ordered:
        for fixes in days[d]:
            final.learn_track(track_points(fixes))
    final.calibrate(ratios)
    return final


def evaluate(
    route_id: str,
    mode: str,
    train_days: int = 14,
    test_days: int = 7,
    buses: int = 3,
    seed: int = 20260914,
) -> dict:
    profile = load_profile(route_id)
    started = time.time()

    all_days = synthesize(profile, FIRST_DAY, train_days + test_days, buses=buses, mode=mode, seed=seed)
    ordered = sorted(all_days)
    training = {d: all_days[d] for d in ordered[:train_days]}
    testing = {d: all_days[d] for d in ordered[train_days:]}

    model = train(profile, training, "learned")
    average = train(profile, training, "average")

    graded: list[Graded] = []
    for d in sorted(testing):
        for fixes in testing[d]:
            for i, stop, actual_s in queries(profile, fixes):
                naive = naive_prediction(fixes, i, stop.along_m)
                learned = learned_prediction(model, fixes, i, stop.along_m)
                avg = learned_prediction(average, fixes, i, stop.along_m)
                if naive is None or learned is None or avg is None:
                    continue
                n_point, n_low, n_high = naive
                l_point, l_low, l_high, source = learned
                a_point, a_low, a_high, _ = avg
                graded.append(Graded(
                    horizon_min=actual_s / 60.0,
                    band=time_band(fixes[i].ts),
                    naive_err_s=abs(n_point - actual_s),
                    average_err_s=abs(a_point - actual_s),
                    learned_err_s=abs(l_point - actual_s),
                    naive_hit=hit(n_low, n_high, actual_s),
                    average_hit=hit(a_low, a_high, actual_s),
                    learned_hit=hit(l_low, l_high, actual_s),
                    naive_width_min=n_high - n_low,
                    average_width_min=a_high - a_low,
                    learned_width_min=l_high - l_low,
                    learned_source=source,
                ))

    return {
        "route": route_id,
        "mode": mode,
        "lengthKm": profile.length_m / 1000.0,
        "graded": graded,
        "calibrated": model.calibrated,
        "range": (model.range_low, model.range_high),
        "bias": model.bias,
        "seconds": time.time() - started,
    }


# ------------------------------------------------------------------ summaries

def summarise(rows: list[Graded]) -> dict:
    if not rows:
        return {"n": 0}
    n = len(rows)
    naive_mae = statistics.fmean(r.naive_err_s for r in rows) / 60.0
    average_mae = statistics.fmean(r.average_err_s for r in rows) / 60.0
    learned_mae = statistics.fmean(r.learned_err_s for r in rows) / 60.0

    def gain(before: float, after: float) -> float:
        return (before - after) / before if before else 0.0

    return {
        "n": n,
        "naive_mae": naive_mae,
        "average_mae": average_mae,
        "learned_mae": learned_mae,
        "improvement": gain(naive_mae, learned_mae),
        # What simply accounting for stop-and-go time adds over the live estimator.
        "pace_gain": gain(naive_mae, average_mae),
        # What knowing *where and when* adds on top of that.
        "structure_gain": gain(average_mae, learned_mae),
        "naive_hit": sum(r.naive_hit for r in rows) / n,
        "average_hit": sum(r.average_hit for r in rows) / n,
        "learned_hit": sum(r.learned_hit for r in rows) / n,
        "naive_width": statistics.fmean(r.naive_width_min for r in rows),
        "average_width": statistics.fmean(r.average_width_min for r in rows),
        "learned_width": statistics.fmean(r.learned_width_min for r in rows),
        "learned_coverage": sum(r.learned_source == "learned" for r in rows) / n,
    }


def pct(x: float) -> str:
    """A gain as a signed error change: +40% gain shows as -40% error."""
    change = round(-x * 100)
    return "0%" if change == 0 else f"{change:+d}%"


def error_table(results: list[dict]) -> list[str]:
    lines = [
        "| Route | Traffic | Predictions | Naive (live) | Average pace | Learned | Stop time accounted | Where & when learned |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for res in results:
        s = summarise(res["graded"])
        lines.append(
            f"| {res['route']} | {res['mode']} | {s['n']:,} | {s['naive_mae']:.2f} min | "
            f"{s['average_mae']:.2f} min | **{s['learned_mae']:.2f} min** | "
            f"{pct(s['pace_gain'])} | {pct(s['structure_gain'])} |"
        )
    return lines


def range_table(results: list[dict]) -> list[str]:
    lines = [
        "| Route | Traffic | In range: naive | average | learned | Width: naive | average | learned |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for res in results:
        s = summarise(res["graded"])
        lines.append(
            f"| {res['route']} | {res['mode']} | {s['naive_hit'] * 100:.0f}% | {s['average_hit'] * 100:.0f}% | "
            f"{s['learned_hit'] * 100:.0f}% | {s['naive_width']:.1f} min | {s['average_width']:.1f} min | "
            f"{s['learned_width']:.1f} min |"
        )
    return lines


def breakdown_table(res: dict, key, labels) -> list[str]:
    groups: dict[str, list[Graded]] = defaultdict(list)
    for r in res["graded"]:
        groups[key(r)].append(r)
    lines = [
        "| | Predictions | Naive (live) | Average pace | Learned | Where & when learned |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label in labels:
        s = summarise(groups.get(label, []))
        if not s["n"]:
            continue
        lines.append(
            f"| {label} | {s['n']:,} | {s['naive_mae']:.2f} min | {s['average_mae']:.2f} min | "
            f"{s['learned_mae']:.2f} min | {pct(s['structure_gain'])} |"
        )
    return lines


def horizon_label(r: Graded) -> str:
    for lo, hi in HORIZONS:
        if lo <= r.horizon_min < hi:
            return f"{lo}-{hi} min away" if hi < 999 else f"{lo}+ min away"
    return "?"


HORIZON_LABELS = [f"{lo}-{hi} min away" if hi < 999 else f"{lo}+ min away" for lo, hi in HORIZONS]


def peak_label(r: Graded) -> str:
    return "rush hour" if r.band in PEAK else "outside rush hour"


def render_report(results: list[dict]) -> str:
    generated = datetime.now(IST).strftime("%Y-%m-%d")
    structured = [r for r in results if r["mode"] == STRUCTURED]
    by = {(r["route"], r["mode"]): summarise(r["graded"]) for r in results}

    out: list[str] = [
        "# Arrival estimates: what learning from history adds",
        "",
        f"_Generated by `tools/eval_eta.py` on {generated}. The simulation is seeded, so re-running reproduces these numbers._",
        "",
        "> **Every trip in this evaluation is simulated.** No real buses were recorded in",
        "> Nagercoil. The traffic's *shape* (rush hours, slow town streets, waiting at",
        "> stops) is a plausible assumption and its *magnitudes* are invented. These",
        "> results show what each method can exploit when that kind of structure exists.",
        "> They say nothing about how much structure Nagercoil's real roads have, or how",
        "> accurate any estimator would be on them.",
        "",
        "## Summary",
        "",
    ]

    for res in structured:
        s = by[(res["route"], STRUCTURED)]
        c = by.get((res["route"], UNIFORM))
        line = (
            f"- **{res['route']}** ({res['lengthKm']:.1f} km): learning where and when the road is slow cut average "
            f"error by **{s['structure_gain'] * 100:.0f}%** beyond an average pace"
        )
        if c:
            line += f"; on the control, the same comparison changed it by {pct(c['structure_gain'])}"
        out.append(line + ".")

    uniform_pace = [by[(r["route"], UNIFORM)]["pace_gain"] for r in results if r["mode"] == UNIFORM]
    if uniform_pace:
        out += [
            f"- **The live estimator ignores time spent stopped.** Even on traffic with no rush hours at all, simply "
            f"learning an average pace that includes stops cut its error by "
            f"{min(uniform_pace) * 100:.0f}-{max(uniform_pace) * 100:.0f}%. That flaw is independent of learning "
            "anything about Nagercoil.",
        ]
    out += [
        "",
        "## Method",
        "",
        "- **Routes:** the OSM-routed Nagercoil routes in `tools/data/nagercoil_routes.json`.",
        "- **Simulated service:** 3 buses per route, 05:30-22:30, out and back, for 21 days.",
        "- **Train / test split:** training uses days 1-14; grading uses days 15-21, which training never saw. "
        f"Learned ranges are calibrated on the last {CALIBRATION_DAYS} *training* days; test days tune nothing.",
        "- **Questions:** once a simulated minute per bus, every estimator predicts the arrival at every stop "
        "ahead, and is graded against when the bus really got there.",
        "- **Estimators:**",
        "  - *Naive (live)* is what the API runs today: remaining distance divided by the bus's reported speed "
        f"(or its recent average, or 18 km/h), with a {RANGE_LOW:g}x-{RANGE_HIGH:g}x range.",
        "  - *Average pace* learns one travel pace per direction from history, time spent stopped included, "
        "and nothing about place or time of day.",
        "  - *Learned* is `api/shared/learned_eta.py`: pace per 200 m of road, per direction, per time of day.",
        "  - Both learned estimators fall back to the naive speed where they have no data, so lack of coverage "
        "cannot make them worse.",
        "- **Noise:** estimators see GPS-noisy positions; ground truth uses noiseless ones.",
        "- **Ranges** are rounded to whole minutes as the API displays them and read with a half-minute tolerance.",
        "- **Excluded for all estimators alike:** buses waiting inside a terminus zone (whether they leave now "
        "depends on a timetable none of them has), moments where a bus's direction is still being inferred, and "
        "stops under 150 m away.",
        "",
        "## Average error",
        "",
        "Mean absolute difference between predicted and actual arrival. The last two columns show how the error "
        "changed at each step: live -> average pace, and average pace -> learned.",
        "",
    ]
    out += error_table(results)
    out += [
        "",
        "### The control, and why there are three estimators",
        "",
        "`uniform` traffic has the same average speed and randomness as `structured`, but nothing that depends on "
        "place or time of day. So on `uniform`, the *where & when* column should be close to zero: there is no "
        "where-or-when to learn.",
        "",
        "The first version of this evaluation compared only the live estimator with the learned one, and on "
        "`uniform` traffic the learned model still appeared to cut error by 35-61%. The control had caught a "
        "confound: most of that gain was the live estimator ignoring stop time, which any history-based pace fixes. "
        "The *average pace* estimator was added to separate the two effects.",
        "",
        "## Ranges shown to riders",
        "",
        "\"In range\" is how often the actual arrival fell inside the range shown. Width is the average size of that "
        "range. A good range is right often *and* narrow.",
        "",
    ]
    out += range_table(results)
    for res in structured:
        out += [
            "",
            f"## {res['route']} in detail (structured traffic)",
            "",
            "By how far away the bus was:",
            "",
        ]
        out += breakdown_table(res, horizon_label, HORIZON_LABELS)
        out += ["", "By time of day:", ""]
        out += breakdown_table(res, peak_label, ["rush hour", "outside rush hour"])
        low, high = res["range"]
        out += [
            "",
            f"Learned range after calibration: {low:.2f}x-{high:.2f}x of the model's travel time, with a "
            f"{res['bias']:.2f}x bias correction"
            + ("" if res["calibrated"] else " (not calibrated: too little held-out data)") + ".",
        ]
    out += [
        "",
        "## Reading these honestly",
        "",
        "- The learned model's extra accuracy comes from knowing that rush hour is slow and that buses wait longer at "
        "some places. On real roads, the size of that advantage depends on how strong and how *regular* those patterns "
        "are, which only recorded trips can show.",
        "- Weekday/weekend differences, festivals, rain and school timings are not simulated. Real traffic has all of "
        "them; the model as built separates only by time of day.",
        "- A model trained on simulated traffic must never be presented to riders as knowledge of real traffic. The "
        "API reports what its model was trained on for that reason.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", nargs="+", default=["NGL-VAD-KKD", "NGL-KK"])
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    results = []
    for route_id in args.routes:
        for mode in (STRUCTURED, UNIFORM):
            print(f"Evaluating {route_id} / {mode} ...", flush=True)
            res = evaluate(route_id, mode)
            s = summarise(res["graded"])
            print(
                f"  {s['n']:,} predictions in {res['seconds']:.0f}s | avg error: naive {s['naive_mae']:.2f}, "
                f"average pace {s['average_mae']:.2f}, learned {s['learned_mae']:.2f} min | "
                f"where & when {pct(s['structure_gain'])}",
                flush=True,
            )
            results.append(res)

    print()
    print("\n".join(error_table(results)))
    print()
    print("\n".join(range_table(results)))

    if args.write_report:
        with open(REPORT_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_report(results))
        print(f"\nWrote {os.path.relpath(REPORT_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
