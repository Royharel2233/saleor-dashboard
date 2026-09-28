"""Calibrate thresholds against real petrol-vehicle logs, and measure how often
the engine cries fault on a vehicle that is running normally.

The scenario values in doip_simulator.py were chosen by the same author who wrote
the thresholds in triage.py, so they cannot test each other. These logs were
recorded by a stranger, for unrelated reasons, before this project existed. That
is the property that makes them worth something: nobody chose them to make this
engine look right.

Source (clone next to this repo, or pass --data):
    git clone --depth 1 https://github.com/raleighlittles/bluedriver-visualization

    python calibrate.py --data ../bluedriver-visualization/data
"""

import argparse
import csv
import glob
import os
import re
import statistics as st

from baseline import Baseline
from robustness import make_scan
from triage import MAF, RPM, TRIM, triage

IDLE_RPM_RANGE = (550, 1000)
SUBSTANTIVE_MARKERS = ("Unmetered air", "Over-fuelling", "signal implausible")


def column(header, pattern):
    for index, name in enumerate(header):
        if re.search(pattern, name, re.I):
            return index
    return None


def load_idle_samples(data_dir, files=None):
    """Every idle sample from every log carrying MAF, long-term trim and RPM."""
    samples = []
    files_used = 0
    for path in files if files is not None else sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        with open(path, encoding="utf-8", errors="replace") as handle:
            rows = list(csv.reader(handle))
        if len(rows) < 5:
            continue
        header = rows[2]  # BlueDriver puts a title and a timestamp above the header
        i_maf = column(header, r"Mass Air Flow")
        i_trim = column(header, r"Long Term Fuel Trim Bank 1")
        i_rpm = column(header, r"Engine RPM")
        i_speed = column(header, r"Vehicle Speed")
        if None in (i_maf, i_trim, i_rpm):
            continue
        files_used += 1
        for row in rows[3:]:
            try:
                rpm = float(row[i_rpm])
                maf = float(row[i_maf])
                trim = float(row[i_trim])
            except (ValueError, IndexError):
                continue
            speed = None
            if i_speed is not None:
                try:
                    speed = float(row[i_speed])
                except (ValueError, IndexError):
                    speed = None
            if IDLE_RPM_RANGE[0] <= rpm <= IDLE_RPM_RANGE[1] and (speed is None or speed == 0):
                samples.append({"maf": maf, "trim": trim, "rpm": rpm})
    return samples, files_used


def usable_logs(data_dir):
    """Logs carrying all three channels this vehicle actually records."""
    keep = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        with open(path, encoding="utf-8", errors="replace") as handle:
            rows = [handle.readline() for _ in range(3)]
        header = rows[2].split(",")
        if all(column(header, p) is not None for p in
               (r"Mass Air Flow", r"Long Term Fuel Trim Bank 1", r"Engine RPM")):
            keep.append(path)
    return keep


def false_positive_rate(samples, reference, label):
    """How often a fault is named on a vehicle that is running normally.

    A stored code is included deliberately: with none, the engine short-circuits
    to "no faults" before the mixture logic runs, so that configuration would
    report a flattering rate while testing nothing. The realistic case is a code
    set by a transient and latched, with the engine now running correctly.
    """
    verdicts = {}
    for sample in samples:
        scan = make_scan(maf=sample["maf"], trim=sample["trim"], rpm=sample["rpm"])
        del scan["ecus"][0]["live_data"]["intake_manifold_pressure_hpa"]
        verdict = triage(scan, reference)
        primary = verdict["primary"]
        cause = primary["cause"] if primary else "no faults"
        verdicts[cause] = verdicts.get(cause, 0) + 1

    total = sum(verdicts.values())
    named = 0
    print(f"\n  --- {label} ---")
    for cause, count in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        flag = ""
        if any(m in cause for m in SUBSTANTIVE_MARKERS):
            named += count
            flag = "  <-- names a fault"
        print(f"  {count:6} ({count/total:5.1%})  {cause[:62]}{flag}")
    print(f"  false-positive rate: {named/total:.1%}")
    return named / total


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def describe(name, values):
    print(
        f"  {name:14} n={len(values):6}  min {min(values):7.2f}  p5 {percentile(values, .05):7.2f}  "
        f"med {st.median(values):7.2f}  p95 {percentile(values, .95):7.2f}  max {max(values):7.2f}"
    )


def main():
    parser = argparse.ArgumentParser(description="Calibrate triage thresholds on real logs")
    parser.add_argument("--data", default="../bluedriver-visualization/data")
    args = parser.parse_args()

    samples, files_used = load_idle_samples(args.data)
    if not samples:
        print(f"No usable logs in {args.data}. See the docstring for the clone command.")
        return

    print(f"=== real idle distributions ({files_used} logs, {len(samples)} idle samples) ===")
    describe("MAF g/s", [s["maf"] for s in samples])
    describe("LTFT %", [s["trim"] for s in samples])
    describe("RPM", [s["rpm"] for s in samples])

    logs = usable_logs(args.data)
    half = len(logs) // 2
    build_logs, eval_logs = logs[:half], logs[half:]
    build_samples, _ = load_idle_samples(args.data, build_logs)
    eval_samples, _ = load_idle_samples(args.data, eval_logs)

    print(f"\n=== train / test split ===")
    print(f"  baseline built from {len(build_logs)} logs ({len(build_samples)} idle samples)")
    print(f"  evaluated on a held-out {len(eval_logs)} logs ({len(eval_samples)} idle samples)")

    reference = Baseline.from_samples(
        name="BlueDriver petrol vehicle (2018)",
        source=f"{len(build_logs)} held-in logs, {len(build_samples)} idle samples",
        samples={
            MAF: [s["maf"] for s in build_samples],
            TRIM: [s["trim"] for s in build_samples],
            RPM: [s["rpm"] for s in build_samples],
        },
    )
    reference.save("baseline_bluedriver.json")
    print("  " + reference.describe())

    print("\n=== false positives on held-out normal running ===")
    with_baseline = false_positive_rate(eval_samples, reference, "with the vehicle's own baseline")
    without = false_positive_rate(eval_samples, None, "with no baseline")

    print(f"\n=== result ===")
    print(f"  no baseline        : {without:.1%} of normal running gets a named fault")
    print(f"  own baseline       : {with_baseline:.1%}")
    print("\n  Caveat: one vehicle, one owner, 2018, petrol, model and displacement")
    print("  unknown, health not independently confirmed. The baseline is built and")
    print("  evaluated on disjoint logs, so the rate is out-of-sample for this")
    print("  vehicle; it is not evidence about any other vehicle.")


if __name__ == "__main__":
    main()
