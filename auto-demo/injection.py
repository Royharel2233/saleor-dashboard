"""Inject physically-derived faults into real healthy logs.

The gap a real vehicle closes is one link: does a leak actually move fuel trim
and air mass the way this engine assumes? That cannot be settled here. What can
be settled is the question underneath it -- how large must the effect be before
the engine notices -- and that turns a blocked yes/no into a measured curve.

The substrate is real: 1,542 idle samples from a petrol vehicle logged by a
stranger, with that vehicle's own noise and spread. Only the perturbation is
modelled, and it is modelled from conservation of mass rather than chosen to be
detectable:

    At idle the ECU governs engine speed, so total air entering the cylinders is
    held roughly constant. Air admitted through a leak downstream of the sensor
    displaces air through the sensor: the throttle closes to compensate.

        measured MAF  ->  m * (1 - f)
        true charge   ->  unchanged

    The mixture is therefore lean by the leak fraction, and the closed-loop
    correction adds fuel to match:

        long-term trim -> t + 100 * f / (1 - f)

    where f is the leak as a fraction of total idle air flow.

This is a first-order model and it is the assumption under test, not evidence
for it. What it produces is honest in a narrow way: the detection threshold is
expressed in units of a physical quantity, so a vehicle measurement can later
confirm or refute it directly.

    python injection.py --data ../bluedriver-visualization/data
"""

import argparse
import statistics as st

from baseline import Baseline
from calibrate import load_idle_samples, usable_logs
from robustness import make_scan
from triage import MAF, RPM, TRIM, triage

SUBSTANTIVE = ("Unmetered air", "Over-fuelling", "signal implausible")
LEAK_FRACTIONS = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40]

# A single UDS read is one noisy sample. A tester can sit at idle and log for
# half a minute instead, and the median of K samples has roughly 1/sqrt(K) of
# the spread. Since the decision bar is a multiple of that spread, averaging
# lowers the smallest detectable fault by the same factor -- the one lever that
# costs nothing but time.
AGGREGATE = 16


def inject_leak(sample: dict, fraction: float) -> dict:
    """Apply an unmetered-air leak of the given fraction to one real sample."""
    if fraction <= 0:
        return dict(sample)
    return {
        "maf": sample["maf"] * (1 - fraction),
        "trim": sample["trim"] + 100.0 * fraction / (1 - fraction),
        "rpm": sample["rpm"],
    }


def aggregate(samples, size):
    """Collapse consecutive samples into their medians, as a logging session would."""
    if size <= 1:
        return list(samples)
    out = []
    for start in range(0, len(samples) - size + 1, size):
        window = samples[start:start + size]
        out.append({key: st.median([s[key] for s in window]) for key in ("maf", "trim", "rpm")})
    return out


def verdict_for(sample, reference):
    scan = make_scan(maf=sample["maf"], trim=sample["trim"], rpm=sample["rpm"])
    # These logs carry no manifold pressure channel.
    del scan["ecus"][0]["live_data"]["intake_manifold_pressure_hpa"]
    result = triage(scan, reference)
    primary = result["primary"]
    return primary["cause"] if primary else "no faults"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../bluedriver-visualization/data")
    args = parser.parse_args()

    logs = usable_logs(args.data)
    half = len(logs) // 2
    configurations = [
        (False, 1, "any idle, single reading"),
        (True, 1, "settled warm idle, single reading"),
        (True, AGGREGATE, f"settled warm idle, median of {AGGREGATE} readings"),
    ]
    for strict, window, label in configurations:
        build_samples, _ = load_idle_samples(args.data, logs[:half], strict=strict)
        eval_samples, _ = load_idle_samples(args.data, logs[half:], strict=strict)
        if not build_samples or not eval_samples:
            print(f"No usable logs in {args.data}")
            return
        build_samples = aggregate(build_samples, window)
        eval_samples = aggregate(eval_samples, window)
        print(f"\n{'=' * 78}\n== {label}\n{'=' * 78}")
        run(build_samples, eval_samples, label, half)


def run(build_samples, eval_samples, label, half):
    reference = Baseline.from_samples(
        name="BlueDriver petrol vehicle (2018)",
        source=f"{label}, {half} held-in logs, {len(build_samples)} samples",
        samples={
            MAF: [s["maf"] for s in build_samples],
            TRIM: [s["trim"] for s in build_samples],
            RPM: [s["rpm"] for s in build_samples],
        },
    )
    print(reference.describe())
    print(f"\ninjecting into {len(eval_samples)} held-out real idle samples\n")
    print(f"{'leak':>6}  {'Δtrim':>7}  {'ΔMAF':>7}  {'detected':>9}  most common verdict")
    print("-" * 78)

    first_detected = None
    for fraction in LEAK_FRACTIONS:
        verdicts = {}
        for sample in eval_samples:
            cause = verdict_for(inject_leak(sample, fraction), reference)
            verdicts[cause] = verdicts.get(cause, 0) + 1
        total = sum(verdicts.values())
        detected = sum(count for cause, count in verdicts.items()
                       if any(marker in cause for marker in SUBSTANTIVE))
        leading = max(verdicts.items(), key=lambda kv: kv[1])
        delta_trim = 100.0 * fraction / (1 - fraction) if fraction else 0.0
        rate = detected / total
        if first_detected is None and rate >= 0.5 and fraction > 0:
            first_detected = fraction
        print(f"{fraction:5.0%}  {delta_trim:+6.1f}%  {-fraction:6.0%}  {rate:8.1%}  "
              f"{leading[0][:40]} ({leading[1]/total:.0%})")

    print()
    if first_detected:
        print(f"Detection passes 50% at a leak of about {first_detected:.0%} of idle air flow, "
              f"a trim shift of {100.0 * first_detected / (1 - first_detected):+.0f} points.")
    print("\nAt 0% leak the rate is the false-positive rate: the same real samples,")
    print("unmodified. Any detection there is the engine inventing a fault.")
    print("\nWhat this does not establish: that a real leak of that size produces this")
    print("trim and air-mass response. That link needs one measurement on a vehicle,")
    print("and this curve states exactly which measurement would settle it.")


if __name__ == "__main__":
    main()
