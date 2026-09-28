"""Robustness harness: how the triage engine fails, and how close to a coin toss it is.

Three checks, none of which ask whether a verdict is right -- they ask whether it
is stable, whether it degrades honestly when data is missing, and whether its
first inspection step sends a technician at something the data already excluded.

    python robustness.py
"""

import copy
import json

import random

from baseline import DEVIATION_SIGMA, Baseline
from triage import MAF, MAP, MIN_EFFECT, MIN_EFFECT_FRACTION, RPM, TRIM, triage

# A healthy reference for a vehicle idling at the simulator's values. Built the
# way a real one is: repeated reads of a known-good vehicle, with sensor spread.
HEALTHY = {MAF: 3.98, TRIM: 1.20, RPM: 768.0, MAP: 346.0}
_rng = random.Random(20260928)
REFERENCE = Baseline.from_samples(
    name="synthetic healthy idle",
    source="120 simulated reads at 3% sensor noise",
    samples={channel: [value * (1 + _rng.gauss(0, 0.03)) for _ in range(120)]
             for channel, value in HEALTHY.items()},
)


def make_scan(maf=2.10, trim=23.8, rpm=762, map_hpa=344, dtcs=True, live=True):
    """Build a scan payload in the shape scan.full_scan produces."""
    dme_dtcs = [
        {"code": "101E01", "status_byte": "0x2F", "status": ["testFailed", "confirmedDTC"],
         "description": "Air mass sensor, plausibility: signal too low"},
        {"code": "10A204", "status_byte": "0x2F", "status": ["testFailed", "confirmedDTC"],
         "description": "Mixture adaptation bank 1, additive: limit exceeded"},
    ] if dtcs else []
    live_data = {
        "maf_g_per_s": maf,
        "long_term_fuel_trim_pct": trim,
        "engine_speed_rpm": rpm,
        "intake_manifold_pressure_hpa": map_hpa,
    } if live else {}
    return {
        "vin": "WBA8E9G51GNT12345",
        "connection": {"gateway": "0x0010", "transport": "synthetic"},
        "ecus": [
            {"address": "0x0012", "name": "DME (Engine)", "dtcs": dme_dtcs,
             "live_data": live_data, "raw_0x19_response": "-"},
            {"address": "0x0029", "name": "DSC (Brakes)",
             "dtcs": [{"code": "480AB2", "status_byte": "0x2F", "status": ["confirmedDTC"],
                       "description": "Interface to DME: implausible torque signal"}] if dtcs else [],
             "live_data": {}, "raw_0x19_response": "-"},
        ],
    }


def cause_of(scan, reference=REFERENCE):
    verdict = triage(scan, reference)
    primary = verdict["primary"]
    return (primary["cause"] if primary else "no faults"), primary["confidence"] if primary else "-", verdict


# ---------------------------------------------------------------- boundary sweep

# Verdicts that mean "this data does not decide" rather than naming a fault.
# A signal may legitimately pass through these between regimes.
INDECISIVE = ("plausibility: signal too low", "undetermined")


def _substantive(cause):
    return not any(marker in cause for marker in INDECISIVE)


def sweep(field, values, **fixed):
    """Walk one signal across a range, returning its transitions and the bands
    each substantive verdict occupies.

    A substantive verdict appearing in two disconnected bands means the signal
    is not monotonic in that verdict, which is the real instability. Simply
    counting transitions punishes an engine for having several honest regimes.
    """
    transitions = []
    bands = {}
    previous = None
    for value in values:
        cause, _, _ = cause_of(make_scan(**{**fixed, field: value}))
        if previous is not None and cause != previous[1]:
            transitions.append((previous[0], value, previous[1], cause))
            if _substantive(cause):
                bands.setdefault(cause, []).append(value)
        elif previous is None and _substantive(cause):
            bands.setdefault(cause, []).append(value)
        previous = (value, cause)
    return transitions, bands


def boundary_check():
    print("=== 1. boundary sweep: where does the verdict flip, and does it flip once? ===")
    print(f"    judged against {REFERENCE.describe()}")
    print(f"    cutoff: {DEVIATION_SIGMA}σ, plus a minimum effect of "
          f"{MIN_EFFECT[TRIM]} trim points / {MIN_EFFECT[RPM]:.0f} rpm / "
          f"{MIN_EFFECT_FRACTION[MAF]:.0%} of healthy MAF and MAP")
    findings = []

    sweeps = [
        ("long_term_fuel_trim_pct", "trim %", [round(-30 + i * 1.0, 1) for i in range(61)], {}),
        ("intake_manifold_pressure_hpa", "MAP hPa", list(range(250, 760, 10)), {}),
        ("engine_speed_rpm", "idle rpm", list(range(600, 1500, 20)), {}),
        ("maf_g_per_s", "MAF g/s", [round(0.5 + i * 0.1, 2) for i in range(60)], {}),
    ]
    for field, label, values, fixed in sweeps:
        key = {"long_term_fuel_trim_pct": "trim", "intake_manifold_pressure_hpa": "map_hpa",
               "engine_speed_rpm": "rpm", "maf_g_per_s": "maf"}[field]
        transitions, bands = sweep(key, values, **fixed)
        print(f"\n  {label}: {len(transitions)} transition(s), "
              f"{len(bands)} substantive verdict(s)")
        for low, high, before, after in transitions:
            print(f"    {low} -> {high}:  {before[:52]}")
            print(f"                     => {after[:52]}")
        for cause, starts in bands.items():
            if len(starts) > 1:
                findings.append(
                    f"{label}: '{cause[:46]}' is entered {len(starts)} separate times "
                    f"(at {starts}) -- the verdict is not monotonic in this signal"
                )
    return findings


def hysteresis_check():
    """A verdict that flips on a rounding error is not a diagnosis."""
    print("\n=== 2. sensitivity: verdict change for a 1% move in one signal ===")
    findings = []
    def cutoff(channel, direction):
        """The value at which this channel first counts as deviating."""
        reference = REFERENCE.channel(channel)
        floor = MIN_EFFECT.get(channel, abs(reference.median) * MIN_EFFECT_FRACTION.get(channel, 0))
        step = max(DEVIATION_SIGMA * reference.sigma, floor)
        return reference.median + (step if direction == "high" else -step)

    cases = [
        ("trim", 23.8), ("map_hpa", 344), ("rpm", 762), ("maf", 2.10),
        ("trim", round(cutoff(TRIM, "high"), 2)), ("trim", round(cutoff(TRIM, "low"), 2)),
        ("map_hpa", round(cutoff(MAP, "high"), 1)), ("rpm", round(cutoff(RPM, "high"), 1)),
        ("maf", round(cutoff(MAF, "low"), 2)),
    ]
    for field, value in cases:
        low = round(value * 0.99, 3)
        high = round(value * 1.01, 3)
        cause_low, _, _ = cause_of(make_scan(**{field: low}))
        cause_high, _, _ = cause_of(make_scan(**{field: high}))
        flipped = cause_low != cause_high
        marker = "FLIPS" if flipped else "stable"
        print(f"  {field:8} {low:>8} vs {high:>8}  {marker}")
        if flipped:
            print(f"      {cause_low[:70]}")
            print(f"   -> {cause_high[:70]}")
            findings.append(
                f"{field} at {value} flips the verdict on a 1% change -- a reading this close "
                f"to the threshold should be reported as indeterminate, not as a verdict"
            )
    return findings


# ------------------------------------------------------------ degradation checks

def degradation_check():
    print("\n=== 3. degradation: does it stay honest when data is missing? ===")
    findings = []

    cause, confidence, verdict = cause_of(make_scan(live=False))
    print(f"  no live data at all      -> confidence={confidence}  cause={cause}")
    if confidence != "low":
        findings.append("with no live data the engine still reports above low confidence")
    if "DTC pattern alone" not in " ".join(verdict["evidence"]):
        findings.append("with no live data the engine does not say the verdict rests on DTCs alone")

    scan = make_scan()
    del scan["ecus"][0]["live_data"]["intake_manifold_pressure_hpa"]
    cause, confidence, _ = cause_of(scan)
    print(f"  MAP missing              -> confidence={confidence}  cause={cause[:58]}")
    if "downstream of the throttle" in cause:
        findings.append("claims a leak is downstream of the throttle with no MAP reading to support it")

    scan = make_scan()
    del scan["ecus"][0]["live_data"]["long_term_fuel_trim_pct"]
    cause, confidence, _ = cause_of(scan)
    print(f"  trim missing             -> confidence={confidence}  cause={cause[:58]}")
    if confidence != "low":
        findings.append("reports above low confidence with no fuel trim, its primary discriminator")

    cause, confidence, verdict = cause_of(make_scan(dtcs=False))
    print(f"  no DTCs, live data only  -> {cause}")

    scan = make_scan()
    scan["ecus"][0]["dtcs"][0]["description"] = "unknown (no local text entry)"
    cause, _, _ = cause_of(scan)
    print(f"  unknown DTC text         -> {cause[:58]}")
    return findings


# --------------------------------------------------------------- wasted-step check

# What the data actively EXCLUDES in each state. A first step naming one of these
# is a wasted job, which is the metric with commercial meaning.
EXCLUDED = {
    "intake_leak": {
        "scan": dict(maf=2.10, trim=23.8, rpm=762, map_hpa=344),
        "forbidden": ["pcv", "manifold gasket", "brake-booster", "fuel pressure"],
        "why": "manifold holds vacuum and idle is normal, so the leak is not downstream of the throttle",
    },
    "manifold_leak": {
        "scan": dict(maf=2.65, trim=20.1, rpm=1140, map_hpa=641),
        "forbidden": ["charge pipe", "turbo inlet", "intercooler"],
        "why": "manifold vacuum is lost and idle is raised, so the leak is downstream of the throttle",
    },
    "maf_signal_fault": {
        "scan": dict(maf=1.95, trim=0.9, rpm=758, map_hpa=338),
        "forbidden": ["smoke-test"],
        "why": "trim is neutral, so there is no unmetered air to find",
    },
    "over_fuelling": {
        "scan": dict(maf=4.02, trim=-18.8, rpm=771, map_hpa=349),
        "forbidden": ["unmetered", "smoke-test", "leak"],
        "why": "trim is negative, so the mixture is rich and an air leak cannot cause it",
    },
}


def wasted_step_check():
    print("\n=== 4. wasted inspection steps: does step 1 name something excluded? ===")
    findings = []
    wasted = 0
    for name, case in EXCLUDED.items():
        _, _, verdict = cause_of(make_scan(**case["scan"]))
        step = verdict["plan"][0].lower()
        hits = [term for term in case["forbidden"] if term in step]
        status = f"WASTED ({', '.join(hits)})" if hits else "clean"
        print(f"  {name:18} {status}")
        if hits:
            wasted += 1
            findings.append(f"{name}: step 1 names {hits} although {case['why']}")
    print(f"\n  wasted-step rate: {wasted}/{len(EXCLUDED)}")
    return findings


def main():
    findings = []
    findings += boundary_check()
    findings += hysteresis_check()
    findings += degradation_check()
    findings += wasted_step_check()

    print("\n" + "=" * 72)
    if findings:
        print(f"{len(findings)} finding(s):")
        for finding in findings:
            print(f"  - {finding}")
    else:
        print("No findings.")


if __name__ == "__main__":
    main()
