"""Turns a raw DoIP/UDS scan into a root-cause verdict, relative to a baseline.

Two engines, and the UI always states which one produced the answer:

  llm   -- an Anthropic call with a strict output contract (needs ANTHROPIC_API_KEY)
  rules -- a deterministic fault-propagation model over baseline deviations

Why deviations and not thresholds: measured against 1,542 real idle samples, a
fixed "MAF below 3.5 g/s" rule fired on 70% of normal running, and the whole
engine named a fault on a healthy vehicle 49.7% of the time. Healthy idle values
vary more between vehicles than a real leak moves them within one vehicle, so a
global number is either blind or a false-positive generator. Every judgement here
is therefore made against what this vehicle reads when it is healthy.

With no baseline the engine reports what it measured and declines to name a
cause. That is not a limitation to work around; an unreferenced reading genuinely
does not support a verdict.
"""

import json
import os

from baseline import DEVIATION_SIGMA, Baseline

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are a vehicle diagnostic triage engine for BMW F-series \
vehicles communicating over DoIP (ISO 13400) / UDS (ISO 14229).

You receive a JSON scan: per-ECU stored DTCs with status bytes, live data \
identifiers where available, and where available a baseline describing what this \
vehicle reads at idle when healthy, with each live value's deviation from it in \
robust sigma.

Rules you must follow:
1. Name exactly ONE primary root cause, citing the DTC code and the ECU.
2. List every other DTC as a consequential fault, and for each say which \
mechanism links it to the primary cause. A fault you cannot link, you must keep \
as an independent finding rather than dismiss it.
3. Judge live values by their deviation from the baseline, never by whether they \
look high or low in absolute terms. Healthy idle values differ widely between \
vehicles. If no baseline is supplied, say that the values cannot be interpreted \
and give a low confidence.
4. Give a 3-step physical inspection plan, cheapest and least invasive first.
5. Never invent a DTC, a value, or a part number that is not in the input.

Reply with JSON only, matching this shape:
{"primary": {"ecu": str, "code": str, "cause": str, "confidence": "high|medium|low"},
 "consequential": [{"ecu": str, "code": str, "mechanism": str}],
 "independent": [{"ecu": str, "code": str, "note": str}],
 "evidence": [str],
 "plan": [str]}"""

# Which ECUs consume engine-torque/engine-data messages, and so produce
# follow-on faults when the DME itself is unhealthy.
DME_DEPENDENTS = {"0x0029", "0x0040"}

# Live channel names as scan.py emits them.
MAF = "maf_g_per_s"
TRIM = "long_term_fuel_trim_pct"
RPM = "engine_speed_rpm"
MAP = "intake_manifold_pressure_hpa"

# A deviation within this fraction of the cutoff decides nothing either way.
CUTOFF_MARGIN = 0.2

# A single channel must clear the full bar on its own. Two channels moving
# together in the pattern the fault predicts may each clear a lower one: an
# unmetered leak pushes air mass down and fuel trim up at the same time, and
# noise producing both at once, in those directions, is far less likely than
# noise producing either. This is what recovers small leaks that a
# single-channel bar misses, without lowering the bar generally.
CORROBORATED_SIGMA = 2.0

# Smallest change in each channel that a technician would act on, regardless of
# how many sigma it represents. Without these, a quiet baseline turns sensor
# noise into a diagnosis. Absolute units, except where noted as a fraction of the
# healthy median.
MIN_EFFECT = {
    TRIM: 5.0,    # percentage points of fuel correction
    RPM: 120.0,   # rpm
}
MIN_EFFECT_FRACTION = {
    MAF: 0.15,    # 15% of healthy air mass
    MAP: 0.15,    # 15% of healthy manifold pressure
}


def _sigma_text(score: float) -> str:
    """Report an implausibly large deviation as a bound rather than a number.

    A very tight baseline makes ordinary differences enormous in sigma. Printing
    "+762σ" invites a reader to treat arithmetic as certainty, when past roughly
    thirty sigma the only honest statement is that the reading is nowhere near
    this vehicle's normal.
    """
    if abs(score) > 30:
        return f"{'+' if score > 0 else '-'}>30σ"
    return f"{score:+.1f}σ"


def _min_effect(channel_name, reference):
    if channel_name in MIN_EFFECT:
        return MIN_EFFECT[channel_name]
    fraction = MIN_EFFECT_FRACTION.get(channel_name, 0.0)
    return abs(reference.median) * fraction


def triage(scan: dict, baseline: Baseline = None) -> dict:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            result = _triage_llm(scan, baseline)
            result["engine"] = f"llm:{MODEL}"
            return result
        except Exception as exc:  # fall back rather than break a live demo
            result = _triage_rules(scan, baseline)
            result["engine"] = "rules (llm call failed)"
            result["engine_error"] = f"{type(exc).__name__}: {exc}"
            return result
    result = _triage_rules(scan, baseline)
    result["engine"] = "rules (no ANTHROPIC_API_KEY set)"
    return result


def annotate_with_baseline(scan: dict, baseline: Baseline) -> dict:
    """Attach each live value's deviation, so the LLM judges against the baseline
    rather than against its own sense of what a normal reading looks like."""
    annotated = json.loads(json.dumps(scan))
    if baseline is None:
        annotated["baseline"] = None
        return annotated
    annotated["baseline"] = {"name": baseline.name, "source": baseline.source}
    for ecu in annotated.get("ecus", []):
        deviations = {}
        for channel, value in (ecu.get("live_data") or {}).items():
            reference = baseline.channel(channel)
            if reference is not None and value is not None:
                deviations[channel] = {
                    "value": value,
                    "healthy_median": reference.median,
                    "sigma": round(reference.z(value), 2),
                }
        if deviations:
            ecu["deviation_from_healthy"] = deviations
    return annotated


def _triage_llm(scan: dict, baseline: Baseline) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user",
                   "content": json.dumps(annotate_with_baseline(scan, baseline), indent=2)}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def _triage_rules(scan: dict, baseline: Baseline) -> dict:
    """Deterministic propagation model over whatever the scan actually returned."""
    faults = []
    live = {}
    for ecu in scan.get("ecus", []):
        if ecu["address"] == "0x0012":
            live.update(ecu.get("live_data") or {})
        for dtc in ecu.get("dtcs", []):
            faults.append({**dtc, "ecu": ecu["name"], "ecu_address": ecu["address"]})

    if not faults:
        return {
            "primary": None,
            "consequential": [],
            "independent": [],
            "evidence": ["No stored DTCs in any scanned ECU."],
            "plan": ["No action required."],
        }

    # The DME is the upstream node: a confirmed DME fault explains bus-consumer
    # faults in the dependent ECUs. Otherwise pick the first confirmed fault.
    dme_faults = [f for f in faults if f["ecu_address"] == "0x0012"]
    primary = min(dme_faults or faults, key=lambda f: f["code"])

    consequential, independent = [], []
    for fault in faults:
        if fault is primary:
            continue
        if dme_faults and fault["ecu_address"] in DME_DEPENDENTS:
            consequential.append({
                "ecu": fault["ecu"], "code": fault["code"],
                "mechanism": (
                    f"{fault['ecu']} consumes engine data from the DME; a DME fault "
                    f"in state {fault['status_byte']} invalidates that message and "
                    f"sets this code without a local defect."
                ),
            })
        elif fault["ecu_address"] == primary["ecu_address"]:
            consequential.append({
                "ecu": fault["ecu"], "code": fault["code"],
                "mechanism": "Same ECU, same fault chain as the primary code.",
            })
        else:
            independent.append({
                "ecu": fault["ecu"], "code": fault["code"],
                "note": "No link to the primary cause; inspect separately.",
            })

    hypothesis = _mixture_hypothesis(live, baseline)
    return {
        "primary": {
            "ecu": primary["ecu"],
            "code": primary["code"],
            "cause": hypothesis["cause"] or primary["description"],
            "confidence": hypothesis["confidence"],
        },
        "consequential": consequential,
        "independent": independent,
        "evidence": hypothesis["evidence"],
        "plan": hypothesis["plan"],
        "baseline": baseline.name if baseline else None,
    }


def _indeterminate(evidence, plan=None):
    return {
        "cause": None,
        "confidence": "low",
        "evidence": evidence,
        "plan": plan or [
            "Record a healthy baseline for this vehicle, or supply one for its engine "
            "family: without it these readings cannot be called normal or abnormal.",
            "Repeat the measurement at operating temperature and at 2000 rpm.",
            "Do not replace parts on this data alone.",
        ],
    }


def _near_cutoff(value, reference, direction, min_effect, score=None,
                 sigma: float = None) -> bool:
    """Is this reading sitting on the cutoff that decides the question being asked?

    Two things can make a deviation decisive -- a sigma excursion and a minimum
    absolute effect -- so a reading is borderline when it sits close to either
    one. The check is direction-aware: a manifold pressure far BELOW normal is
    nowhere near the cutoff for "is it above normal", and treating it as
    borderline would blank out a verdict that the data supports.
    """
    if value is None or reference is None:
        return False
    sigma = DEVIATION_SIGMA if sigma is None else sigma
    score = reference.z(value) if score is None else score
    if direction == "high" and score <= 0:
        return False
    if direction == "low" and score >= 0:
        return False
    if abs(abs(score) - sigma) <= sigma * CUTOFF_MARGIN:
        return True
    effect = abs(value - reference.median)
    return min_effect > 0 and abs(effect - min_effect) <= min_effect * CUTOFF_MARGIN


def _mixture_hypothesis(live: dict, baseline: Baseline) -> dict:
    """Decide what the live values support, judged against this vehicle's normal.

    Fuel trim answers whether the mixture is wrong at all, and in which
    direction. Manifold pressure and idle speed then answer whether air is
    entering before or after the throttle plate.
    """
    maf, trim = live.get(MAF), live.get(TRIM)
    rpm, map_hpa = live.get(RPM), live.get(MAP)

    evidence = []
    if baseline is None:
        for label, channel, value in (("MAF", MAF, maf), ("fuel trim", TRIM, trim),
                                      ("idle speed", RPM, rpm), ("manifold pressure", MAP, map_hpa)):
            if value is not None:
                evidence.append(f"{label} {value} measured, with nothing to compare it against.")
        evidence.append(
            "No baseline for this vehicle, so the verdict would rest on the DTC pattern "
            "alone. Healthy idle values vary more between vehicles than a fault moves "
            "them within one, so these readings support no cause by themselves."
        )
        return _indeterminate(evidence)

    evidence.append(f"Compared against {baseline.describe()}")

    if trim is None or not baseline.covers(TRIM):
        evidence.append(
            "No referenced fuel trim value, so the verdict would rest on the DTC pattern "
            "alone. Trim decides whether a mixture error exists at all, and without it "
            "the remaining signals cannot be read."
        )
        return _indeterminate(evidence)

    trim_channel = baseline.channel(TRIM)
    trim_effect = _min_effect(TRIM, trim_channel)
    trim_lean, trim_score = trim_channel.deviates(trim, "high", min_effect=trim_effect)
    trim_rich, _ = trim_channel.deviates(trim, "low", min_effect=trim_effect)
    evidence.append(
        f"Fuel trim {trim:+.1f}% against a healthy {trim_channel.median:+.1f}%: "
        f"{_sigma_text(trim_score)}, a move of {abs(trim - trim_channel.median):.1f} points "
        f"(acted on above {trim_effect:.0f})."
    )

    maf_low, maf_score = (False, None)
    maf_channel = None
    if maf is not None and baseline.covers(MAF):
        maf_channel = baseline.channel(MAF)
        maf_low, maf_score = maf_channel.deviates(
            maf, "low", min_effect=_min_effect(MAF, maf_channel))
        evidence.append(
            f"MAF {maf} g/s against a healthy {maf_channel.median} g/s: {_sigma_text(maf_score)}."
        )

    # Corroboration: neither channel alone clears its bar, but both have moved
    # past the lower bar in the directions an unmetered leak produces.
    corroborated = False
    # Evaluated whether or not the single-channel bar was cleared. Gating this on
    # a missed bar made the logic non-monotonic: a reading that just cleared the
    # bar landed in the borderline zone and was blanked, while a slightly smaller
    # one fell through to corroboration and was decided.
    if maf_channel is not None and maf_score is not None:
        trim_partial, _ = trim_channel.deviates(
            trim, "high", sigma=CORROBORATED_SIGMA,
            min_effect=trim_effect * CORROBORATED_SIGMA / DEVIATION_SIGMA)
        maf_partial, _ = maf_channel.deviates(
            maf, "low", sigma=CORROBORATED_SIGMA,
            min_effect=_min_effect(MAF, maf_channel) * CORROBORATED_SIGMA / DEVIATION_SIGMA)
        if trim_partial and maf_partial:
            # Corroboration lowers the bar, so it needs the same protection the
            # full bar has: a reading sitting on the lower cutoff decides no more
            # than one sitting on the higher one.
            scaled = CORROBORATED_SIGMA / DEVIATION_SIGMA
            on_the_line = _near_cutoff(
                trim, trim_channel, "high", trim_effect * scaled, trim_score,
                sigma=CORROBORATED_SIGMA,
            ) or _near_cutoff(
                maf, maf_channel, "low", _min_effect(MAF, maf_channel) * scaled, maf_score,
                sigma=CORROBORATED_SIGMA,
            )
            if on_the_line:
                evidence.append(
                    f"Air mass and fuel trim both moved in the directions a leak "
                    f"produces, but one sits on the {CORROBORATED_SIGMA}σ corroboration "
                    f"cutoff, so the pair decides nothing."
                )
                return _indeterminate(evidence)
            corroborated = True
            evidence.append(
                f"Neither channel alone clears {DEVIATION_SIGMA}σ, but air mass is "
                f"{_sigma_text(maf_score)} and fuel trim {_sigma_text(trim_score)}: both past "
                f"{CORROBORATED_SIGMA}σ, and in the two directions an unmetered leak "
                f"produces together. Taken as corroborating evidence."
            )

    trim_lean = trim_lean or corroborated
    # Corroboration is independent evidence and resolves a borderline reading.
    # Without it, a reading sitting on either cutoff decides nothing, whichever
    # side of the line it happens to fall.
    if not corroborated and (
        _near_cutoff(trim, trim_channel, "high", trim_effect, trim_score)
        or _near_cutoff(trim, trim_channel, "low", trim_effect, trim_score)
    ):
        evidence.append("That deviation sits on the decision cutoff and separates nothing.")
        return _indeterminate(evidence)

    # Case 1: trim has not moved, so the mixture is correct and no unmetered air
    # is entering. A MAF reading that has moved is then the reading's problem.
    if not trim_lean and not trim_rich:
        evidence.append("Trim is within this vehicle's normal range, so the mixture is correct.")
        if maf_low:
            return {
                "cause": "Air mass sensor signal implausible with no mixture error: sensor or its wiring, not a leak",
                "confidence": "medium",
                "evidence": evidence,
                "plan": [
                    "Back-probe the MAF connector for supply, ground and signal; check for "
                    "chafing and water ingress before condemning the sensor.",
                    "Compare MAF against calculated load at 2000 rpm: a proportional offset "
                    "across the range indicates the sensor rather than the intake tract.",
                    "Do not smoke-test first -- trim argues against a leak.",
                ],
            }
        evidence.append("No live channel deviates from this vehicle's normal.")
        return _indeterminate(evidence, plan=[
            "Treat the stored code as historic until a deviation appears: read freeze-frame "
            "data to see what the conditions were when it set.",
            "Check the DTC's occurrence counter and aging counter via 0x19 subfunction 0x06.",
            "Re-read after a drive cycle before replacing anything.",
        ])

    # Case 2: rich. An air leak cannot cause this, so do not report one.
    if trim_rich:
        evidence.append("Trim has moved negative: the ECU is removing fuel, so the mixture is rich.")
        return {
            "cause": "Over-fuelling: injector leak, fuel pressure regulation or an air mass sensor reading high",
            "confidence": "medium",
            "evidence": evidence,
            "plan": [
                "Check fuel pressure against specification, hot and cold.",
                "Inspect injectors for leak-down; compare cylinder contributions.",
                "Check for oil contamination on the MAF element causing a high reading.",
            ],
        }

    # Lean from here. Where is the air entering?
    if map_hpa is None or not baseline.covers(MAP):
        evidence.append(
            "No referenced manifold pressure, so whether the air enters before or "
            "after the throttle plate is undetermined."
        )
        return {
            "cause": "Unmetered air entering, position relative to the throttle plate undetermined",
            "confidence": "low",
            "evidence": evidence,
            "plan": [
                "Read manifold pressure and idle speed against this vehicle's healthy "
                "values: they decide whether the leak is upstream or downstream of the "
                "throttle plate.",
                "Smoke-test only after that reading narrows the search.",
                "Compare MAF against calculated load at 2000 rpm.",
            ],
        }

    map_channel = baseline.channel(MAP)
    map_high, map_score = map_channel.deviates(
        map_hpa, "high", min_effect=_min_effect(MAP, map_channel))
    evidence.append(
        f"Manifold pressure {map_hpa} hPa against a healthy {map_channel.median} hPa: {_sigma_text(map_score)}."
    )
    rpm_high, rpm_score = (False, None)
    if rpm is not None and baseline.covers(RPM):
        rpm_channel = baseline.channel(RPM)
        rpm_high, rpm_score = rpm_channel.deviates(
            rpm, "high", min_effect=_min_effect(RPM, rpm_channel))
        evidence.append(
            f"Idle {rpm} rpm against a healthy {baseline.channel(RPM).median} rpm: {_sigma_text(rpm_score)}."
        )
    if _near_cutoff(map_hpa, map_channel, "high", _min_effect(MAP, map_channel), map_score) or (
        rpm is not None and baseline.covers(RPM)
        and _near_cutoff(rpm, baseline.channel(RPM), "high", _min_effect(RPM, baseline.channel(RPM)), rpm_score)
    ):
        evidence.append("A deviation sits on the decision cutoff; the position cannot be called.")
        return _indeterminate(evidence)

    if map_high or rpm_high:
        reasons = []
        if map_high:
            reasons.append(f"manifold pressure is {_sigma_text(map_score)} above this vehicle's healthy value")
        if rpm_high:
            reasons.append(f"idle is {_sigma_text(rpm_score)} above it")
        evidence.append(
            "Lean mixture with " + " and ".join(reasons) + ": the throttle plate has lost "
            "control of manifold pressure, so air is entering downstream of it."
        )
        return {
            "cause": "Unmetered air entering downstream of the throttle plate (manifold gasket, PCV or a vacuum line)",
            "confidence": "medium",
            "evidence": evidence,
            "plan": [
                "Smoke-test the intake manifold, its gasket, the PCV diaphragm and every "
                "vacuum line and brake-booster connection.",
                "Watch whether idle returns to its healthy value once the leak is sealed.",
                "Clear adaptations and re-read 0x19 to confirm the codes do not return.",
            ],
        }

    evidence.append(
        "Lean mixture while manifold pressure and idle stay within this vehicle's "
        "normal range: the throttle still controls the manifold, so the leak is "
        "upstream of it and downstream of the sensor. This excludes the manifold "
        "gasket and PCV."
    )
    return {
        "cause": "Unmetered air entering between the air mass sensor and the throttle plate",
        "confidence": "medium",
        "evidence": evidence,
        "plan": [
            "Smoke-test from the MAF outlet to the throttle plate: turbo inlet duct, "
            "charge pipe and its boot, and the intercooler connections.",
            "Compare MAF against calculated load at 2000 rpm; a load-dependent error "
            "indicates a leak, a proportional one the sensor.",
            "Clear adaptations and re-read 0x19 to confirm the codes do not return.",
        ],
    }


if __name__ == "__main__":
    import sys

    from scan import full_scan

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    reference = Baseline.load(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(triage(full_scan(host), reference), indent=2))
