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

from robustness import make_scan
from triage import (
    IDLE_MAF_EXPECTED,
    IDLE_RPM_NORMAL_MAX,
    TRIM_LEAN_MIN,
    TRIM_NEUTRAL_MAX,
    triage,
)

IDLE_RPM_RANGE = (550, 1000)
SUBSTANTIVE_MARKERS = ("Unmetered air", "Over-fuelling", "signal implausible")


def column(header, pattern):
    for index, name in enumerate(header):
        if re.search(pattern, name, re.I):
            return index
    return None


def load_idle_samples(data_dir):
    """Every idle sample from every log carrying MAF, long-term trim and RPM."""
    samples = []
    files_used = 0
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
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

    print("\n=== thresholds under test ===")
    print(f"  MAF low        < {IDLE_MAF_EXPECTED[0]} g/s")
    print(f"  trim neutral  <= {TRIM_NEUTRAL_MAX} %")
    print(f"  trim lean     >= {TRIM_LEAN_MIN} %")
    print(f"  idle normal   <= {IDLE_RPM_NORMAL_MAX} rpm")

    maf_values = [s["maf"] for s in samples]
    trim_values = [s["trim"] for s in samples]
    rpm_values = [s["rpm"] for s in samples]
    print("\n=== how the thresholds land on normal running ===")
    print(f"  samples my 'MAF low' rule would flag  : {sum(m < IDLE_MAF_EXPECTED[0] for m in maf_values)/len(maf_values):6.1%}")
    print(f"  samples my 'idle elevated' rule flags : {sum(r > IDLE_RPM_NORMAL_MAX for r in rpm_values)/len(rpm_values):6.1%}")
    print(f"  samples my 'rich' rule would flag     : {sum(t <= -TRIM_LEAN_MIN for t in trim_values)/len(trim_values):6.1%}")
    print(f"  samples my 'neutral trim' rule covers : {sum(abs(t) <= TRIM_NEUTRAL_MAX for t in trim_values)/len(trim_values):6.1%}")

    # The measurement that matters. With no stored DTCs the engine short-circuits
    # to "no faults" and never reaches the mixture logic, so that configuration
    # tests nothing. The realistic false positive is a code stored for a transient
    # or unrelated reason while the engine is in fact running normally: does the
    # verdict then read normal live values as confirming a leak?
    print("\n=== false positives: a stored code, but normal running ===")
    verdicts = {}
    for sample in samples:
        scan = make_scan(maf=sample["maf"], trim=sample["trim"], rpm=sample["rpm"])
        # These logs carry no manifold pressure channel, so remove it rather than
        # substituting a value -- the engine must handle its absence honestly.
        del scan["ecus"][0]["live_data"]["intake_manifold_pressure_hpa"]
        verdict = triage(scan)
        primary = verdict["primary"]
        cause = primary["cause"] if primary else "no faults"
        verdicts[cause] = verdicts.get(cause, 0) + 1

    total = sum(verdicts.values())
    substantive = 0
    for cause, count in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        marker = ""
        if any(m in cause for m in SUBSTANTIVE_MARKERS):
            substantive += count
            marker = "  <-- names a fault"
        print(f"  {count:6} ({count/total:5.1%})  {cause[:64]}{marker}")
    print(f"\n  false-positive rate (a fault named on normal running): {substantive/total:.1%}")

    print("\n=== suggested thresholds from this data ===")
    print(f"  MAF idle p5..p95      : {percentile(maf_values, .05):.2f} .. {percentile(maf_values, .95):.2f} g/s"
          f"   (currently {IDLE_MAF_EXPECTED[0]}..{IDLE_MAF_EXPECTED[1]})")
    print(f"  trim idle p5..p95     : {percentile(trim_values, .05):+.1f} .. {percentile(trim_values, .95):+.1f} %")
    print(f"  idle rpm p95          : {percentile(rpm_values, .95):.0f} rpm   (currently {IDLE_RPM_NORMAL_MAX})")
    print("\n  Caveat: one vehicle, one owner, 2018, petrol, unknown model and")
    print("  displacement, health not independently confirmed. These numbers show")
    print("  the current thresholds are wrong for THIS vehicle; they are not yet a")
    print("  specification for a B48.")


if __name__ == "__main__":
    main()
