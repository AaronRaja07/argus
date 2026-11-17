"""
Score the detector against ground truth.

Anomaly detection projects usually stop at "here is a dashboard with a red
line on it". The question that actually matters — does this thing catch real
incidents without burying the on-call in noise — needs labels. We have them,
because every injected fault is recorded with its exact window.

Definitions used here:
  true positive   an injected fault overlapped by >=1 alert
  false negative  an injected fault with no overlapping alert
  false positive  an alert overlapping no injected fault
  MTTD            mean time from fault start to the first overlapping alert

Run:  python evaluate.py --data /data --window-grace 45
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median


@dataclass
class Interval:
    start: float
    end: float
    meta: dict


def detects(alert: Interval, fault: Interval, grace: float, lead: float = 15.0) -> bool:
    """Does `alert` count as detecting `fault`?

    The alert must OPEN inside the fault window, allowing `grace` seconds of
    detection lag past the end and a small `lead` for clock skew.

    An earlier version tested plain interval overlap with a symmetric grace
    window. That was wrong in a way that flattered the results: an alert which
    had already CLOSED shortly before a fault began still "overlapped" it
    within grace, so it was credited with the detection. It produced a
    time-to-detect of minus 372 seconds -- an alert detecting a fault six
    minutes before that fault existed. Any negative TTD is proof the matcher
    is broken, which is why TTD is reported rather than just precision/recall.
    """
    return fault.start - lead <= alert.start <= fault.end + grace


def _union_seconds(spans: list[tuple[float, float]]) -> float:
    if not spans:
        return 0.0
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return sum(b - a for a, b in merged)


def _outside_seconds(spans, windows) -> float:
    """Seconds of `spans` falling outside every window in `windows`."""
    total = 0.0
    for a, b in spans:
        covered = _union_seconds(
            [(max(a, w0), min(b, w1)) for w0, w1 in windows
             if min(b, w1) > max(a, w0)]
        )
        total += (b - a) - covered
    return total


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/data")
    ap.add_argument("--window-grace", type=float, default=45.0,
                    help="seconds of slack allowed for detection lag")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    root = Path(args.data)
    faults = [
        Interval(f["started_at"], f["ends_at"], f)
        for f in load_jsonl(root / "ground_truth.jsonl")
    ]
    alerts = [
        Interval(a["opened_at"], a.get("closed_at", a["opened_at"] + 1), a)
        for a in load_jsonl(root / "alerts.jsonl")
    ]

    if not faults:
        print("no ground truth found — run an experiment first")
        return

    detected: dict[int, list[Interval]] = {}
    for i, f in enumerate(faults):
        hits = [a for a in alerts if detects(a, f, args.window_grace)]
        if hits:
            detected[i] = hits

    matched_alerts = {id(a) for hits in detected.values() for a in hits}
    false_positives = [a for a in alerts if id(a) not in matched_alerts]

    tp, fn, fp = len(detected), len(faults) - len(detected), len(false_positives)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    ttds = [min(a.start for a in hits) - faults[i].start
            for i, hits in detected.items()]
    assert all(t >= -15.0 for t in ttds), "negative TTD means the matcher is broken"

    # Noise measured as TIME, not alert count: one alert stuck open for an hour
    # is far worse for an on-call than three short correct ones.
    fault_secs = _union_seconds([(f.start, f.end) for f in faults])
    alert_secs = _union_seconds([(a.start, a.end) for a in alerts])
    outside = _outside_seconds(
        [(a.start, a.end) for a in alerts],
        [(f.start - args.window_grace, f.end + args.window_grace) for f in faults],
    )
    span = (max(a.end for a in alerts) - min(f.start for f in faults)) if alerts else 0.0
    overhangs = [
        max(a.end for a in hits) - faults[i].end for i, hits in detected.items()
    ]

    by_kind: dict[str, list[int]] = {}
    for i, f in enumerate(faults):
        by_kind.setdefault(f.meta["kind"], []).append(i)

    if args.markdown:
        print("| Metric | Value |")
        print("|---|---|")
        print(f"| Faults injected | {len(faults)} |")
        print(f"| Detected | {tp} |")
        print(f"| Missed | {fn} |")
        print(f"| False alarms | {fp} |")
        print(f"| Precision | {precision:.1%} |")
        print(f"| Recall | {recall:.1%} |")
        print(f"| F1 | {f1:.3f} |")
        if ttds:
            print(f"| Median time-to-detect | {median(ttds):.0f}s |")
            print(f"| Mean time-to-detect | {mean(ttds):.0f}s |")
        print(f"| False-alarm time | {outside:.0f}s"
              + (f" ({outside/span:.1%} of window)" if span else "") + " |")
        if overhangs:
            print(f"| Median alert over-hang | {median(overhangs):.0f}s |")
        print()
        print("| Fault type | Injected | Detected | Recall | Median TTD |")
        print("|---|---|---|---|---|")
        for kind, idxs in sorted(by_kind.items()):
            hit = [i for i in idxs if i in detected]
            kttd = [
                min(a.start for a in detected[i]) - faults[i].start for i in hit
            ]
            ttd_s = f"{median(kttd):.0f}s" if kttd else "—"
            print(
                f"| `{kind}` | {len(idxs)} | {len(hit)} | "
                f"{len(hit)/len(idxs):.0%} | {ttd_s} |"
            )
        return

    print("=" * 58)
    print("  ARGUS DETECTOR EVALUATION")
    print("=" * 58)
    print(f"  faults injected     {len(faults)}")
    print(f"  detected            {tp}")
    print(f"  missed              {fn}")
    print(f"  false alarms        {fp}")
    print("-" * 58)
    print(f"  precision           {precision:.1%}")
    print(f"  recall              {recall:.1%}")
    print(f"  f1                  {f1:.3f}")
    if ttds:
        print(f"  median TTD          {median(ttds):.0f}s")
        print(f"  mean TTD            {mean(ttds):.0f}s")
    print("-" * 58)
    print(f"  fault time          {fault_secs:.0f}s")
    print(f"  alert time          {alert_secs:.0f}s")
    print(f"  false-alarm time    {outside:.0f}s"
          f"  ({outside/span:.1%} of window)" if span else "")
    if overhangs:
        print(f"  median over-hang    {median(overhangs):.0f}s past fault end")
    print("-" * 58)
    print("  by fault type")
    for kind, idxs in sorted(by_kind.items()):
        hit = [i for i in idxs if i in detected]
        print(f"    {kind:<26} {len(hit)}/{len(idxs)}  ({len(hit)/len(idxs):.0%})")
    if false_positives:
        print("-" * 58)
        print("  false alarms")
        for a in false_positives[:10]:
            print(
                f"    peak={a.meta.get('peak_score', 0):.1f} "
                f"primary={a.meta.get('primary_signal', '?')} "
                f"dur={a.meta.get('duration_s', 0):.0f}s"
            )
    print("=" * 58)


if __name__ == "__main__":
    main()
