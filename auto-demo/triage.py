"""Turns a raw DoIP/UDS scan into a root-cause verdict.

Two engines, and the UI always states which one produced the answer:

  llm   -- an Anthropic call with a strict output contract (needs ANTHROPIC_API_KEY)
  rules -- a deterministic fault-propagation model, used when no key is present

The rules engine exists so the demo never silently degrades into a hardcoded
script: it reasons from the scan data it was actually handed, so changing the
simulator's fault set changes the verdict.
"""

import json
import os

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You are a vehicle diagnostic triage engine for BMW F-series \
vehicles communicating over DoIP (ISO 13400) / UDS (ISO 14229).

You receive a JSON scan: per-ECU stored DTCs with status bytes, plus live data \
identifiers where available.

Rules you must follow:
1. Name exactly ONE primary root cause, citing the DTC code and the ECU.
2. List every other DTC as a consequential fault, and for each say which \
mechanism links it to the primary cause. A fault you cannot link, you must keep \
as an independent finding rather than dismiss it.
3. Give a 3-step physical inspection plan, cheapest and least invasive first.
4. Cite the live data values that support the verdict. If no live data supports \
it, say the verdict is DTC-pattern-only and lower your confidence.
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


def triage(scan: dict) -> dict:
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            result = _triage_llm(scan)
            result["engine"] = f"llm:{MODEL}"
            return result
        except Exception as exc:  # fall back rather than break a live demo
            result = _triage_rules(scan)
            result["engine"] = "rules (llm call failed)"
            result["engine_error"] = f"{type(exc).__name__}: {exc}"
            return result
    result = _triage_rules(scan)
    result["engine"] = "rules (no ANTHROPIC_API_KEY set)"
    return result


def _triage_llm(scan: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(scan, indent=2)}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def _triage_rules(scan: dict) -> dict:
    """Deterministic propagation model over whatever the scan actually returned."""
    faults = []
    live = {}
    for ecu in scan.get("ecus", []):
        live.update({f"{ecu['address']}:{k}": v for k, v in ecu.get("live_data", {}).items()})
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
    primary_source = dme_faults or faults
    primary = min(primary_source, key=lambda f: f["code"])

    consequential, independent = [], []
    for fault in faults:
        if fault is primary:
            continue
        if dme_faults and fault["ecu_address"] in DME_DEPENDENTS:
            consequential.append(
                {
                    "ecu": fault["ecu"],
                    "code": fault["code"],
                    "mechanism": (
                        f"{fault['ecu']} consumes engine data from the DME; a DME fault "
                        f"in state {fault['status_byte']} invalidates that message and "
                        f"sets this code without a local defect."
                    ),
                }
            )
        elif fault["ecu_address"] == primary["ecu_address"]:
            consequential.append(
                {
                    "ecu": fault["ecu"],
                    "code": fault["code"],
                    "mechanism": "Same ECU, same fault chain as the primary code.",
                }
            )
        else:
            independent.append(
                {"ecu": fault["ecu"], "code": fault["code"], "note": "No link to the primary cause; inspect separately."}
            )

    hypothesis = _mixture_hypothesis(live)

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
    }


# Idle thresholds. These are estimates, not values read from a specification,
# which is why no verdict built on them is reported above medium confidence.
IDLE_MAF_EXPECTED = (3.5, 4.5)   # g/s, B48 at warm idle
IDLE_MAP_SEALED_MAX = 450        # hPa; above this at idle, manifold vacuum is being lost
IDLE_RPM_NORMAL_MAX = 900        # rpm; above this at idle, something is admitting extra air
TRIM_LEAN_MIN = 10.0             # % additive correction that counts as a real lean error
TRIM_NEUTRAL_MAX = 5.0           # % below which the mixture is effectively correct
BOUNDARY_MARGIN = 0.05           # a reading within 5% of a threshold decides nothing


def _borderline(value, threshold, label, margin=BOUNDARY_MARGIN):
    """True when a reading sits close enough to a threshold that either side is
    within measurement noise. Treating such a reading as decisive produces a
    verdict that flips on a rounding error."""
    if value is None or threshold == 0:
        return None
    if abs(value - threshold) <= abs(threshold) * margin:
        return f"{label} {value} is within {int(margin * 100)}% of the {threshold} decision threshold"
    return None


def _mixture_hypothesis(live: dict) -> dict:
    """Decide what the live values actually support, and how confidently.

    The three cases below share DTCs, so the codes alone cannot separate them.
    What separates them is fuel trim (is the mixture wrong at all?) and then
    manifold pressure with idle speed (is air entering before or after the
    throttle plate?).
    """
    maf = live.get("0x0012:maf_g_per_s")
    trim = live.get("0x0012:long_term_fuel_trim_pct")
    rpm = live.get("0x0012:engine_speed_rpm")
    map_hpa = live.get("0x0012:intake_manifold_pressure_hpa")

    evidence = []
    if maf is not None and rpm:
        evidence.append(
            f"MAF {maf} g/s at {rpm} rpm (idle expectation roughly "
            f"{IDLE_MAF_EXPECTED[0]}-{IDLE_MAF_EXPECTED[1]} g/s; estimate, not a specification)."
        )
    if trim is not None:
        evidence.append(f"Long-term fuel trim {trim:+.1f}%.")
    if map_hpa is not None:
        evidence.append(f"Intake manifold pressure {map_hpa} hPa.")

    if maf is None or trim is None:
        evidence.append("No live data returned: verdict rests on the DTC pattern alone.")
        return {
            "cause": None,
            "confidence": "low",
            "evidence": evidence,
            "plan": [
                "Re-read live data with the engine running; without it these codes "
                "cannot be separated from each other.",
                "Compare MAF against calculated load at 2000 rpm.",
                "Re-read 0x19 to confirm which codes are current rather than historic.",
            ],
        }

    maf_low = maf < IDLE_MAF_EXPECTED[0]
    # Absence of a reading is not evidence. A missing MAP value leaves the
    # question of where the air enters open; it does not answer it.
    manifold_sealed = None if map_hpa is None else map_hpa <= IDLE_MAP_SEALED_MAX
    idle_normal = None if rpm is None else rpm <= IDLE_RPM_NORMAL_MAX

    # Fix 2: refuse to pick a side when a decisive reading sits on a threshold.
    proximity = [
        _borderline(trim, TRIM_LEAN_MIN, "fuel trim"),
        _borderline(trim, TRIM_NEUTRAL_MAX, "fuel trim"),
        _borderline(map_hpa, IDLE_MAP_SEALED_MAX, "manifold pressure"),
        _borderline(rpm, IDLE_RPM_NORMAL_MAX, "idle speed"),
        _borderline(maf, IDLE_MAF_EXPECTED[0], "MAF"),
    ]
    borderline = [note for note in proximity if note]
    if borderline:
        evidence.extend(borderline)
        evidence.append(
            "A reading this close to a decision threshold does not separate the "
            "candidate faults; repeat the measurement before acting on it."
        )
        return {
            "cause": None,
            "confidence": "low",
            "evidence": evidence,
            "plan": [
                "Repeat the measurement at operating temperature and confirm the reading is stable.",
                "Record the same signals at 2000 rpm, where the candidates separate more widely.",
                "Do not replace parts on this data alone.",
            ],
        }

    # Case 1: the mixture is correct, so no unmetered air is entering. A low
    # air mass reading with no lean correction means the READING is wrong.
    if abs(trim) <= TRIM_NEUTRAL_MAX:
        evidence.append(
            "Trim is near zero, so the mixture is correct and no unmetered air is "
            "entering. A leak would force a positive correction. The air mass "
            "signal itself is implausible."
        )
        return {
            "cause": "Air mass sensor signal implausible with no mixture error: sensor or its wiring, not a leak",
            "confidence": "medium",
            "evidence": evidence,
            "plan": [
                "Back-probe the MAF connector for supply, ground and signal; check for "
                "chafing and water ingress before condemning the sensor.",
                "Compare MAF against calculated load at 2000 rpm: a proportional offset "
                "across the range indicates the sensor, not the intake tract.",
                "Do not smoke-test first -- the trim value already argues against a leak.",
            ],
        }

    # Case 2: lean correction with lost manifold vacuum or raised idle. Air is
    # entering AFTER the throttle plate, which is why the throttle has lost
    # control of manifold pressure.
    if trim >= TRIM_LEAN_MIN and (manifold_sealed is False or idle_normal is False):
        reasons = []
        if manifold_sealed is False:
            reasons.append(
                f"manifold pressure {map_hpa} hPa is above the {IDLE_MAP_SEALED_MAX} hPa "
                f"a sealed manifold holds at idle"
            )
        if idle_normal is False:
            reasons.append(f"idle is elevated at {rpm} rpm")
        evidence.append(
            "Lean correction with " + " and ".join(reasons) + ": the throttle plate has "
            "lost control of manifold pressure, so air is entering downstream of it."
        )
        return {
            "cause": "Unmetered air entering downstream of the throttle plate (manifold gasket, PCV or a vacuum line)",
            "confidence": "medium",
            "evidence": evidence,
            "plan": [
                "Smoke-test the intake manifold, its gasket, the PCV diaphragm and every "
                "vacuum line and brake-booster connection.",
                "Watch whether idle falls back to normal when the leak is sealed.",
                "Clear adaptations and re-read 0x19 to confirm the codes do not return.",
            ],
        }

    # Case 3: lean correction while the manifold still holds vacuum and idle is
    # normal. The leak is between the sensor and the throttle plate.
    if trim >= TRIM_LEAN_MIN and maf_low and manifold_sealed is None:
        evidence.append(
            "No manifold pressure reading, so whether the air enters before or after "
            "the throttle plate is undetermined."
        )
        return {
            "cause": "Unmetered air entering, position relative to the throttle plate undetermined",
            "confidence": "low",
            "evidence": evidence,
            "plan": [
                "Read intake manifold pressure and idle speed: they decide whether the leak "
                "is upstream or downstream of the throttle plate.",
                "Smoke-test only after that reading narrows the search.",
                "Compare MAF against calculated load at 2000 rpm.",
            ],
        }

    if trim >= TRIM_LEAN_MIN and maf_low and manifold_sealed:
        evidence.append(
            f"Lean correction while manifold pressure stays at {map_hpa} hPa and idle "
            f"at {rpm} rpm: the throttle still controls the manifold, so the leak is "
            f"upstream of it and downstream of the sensor. This excludes the manifold "
            f"gasket and PCV."
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

    # Case 4: negative trim. The engine is being over-fuelled; a lean-air
    # hypothesis does not fit and must not be reported.
    if trim <= -TRIM_LEAN_MIN:
        evidence.append(
            "Trim is negative, so the ECU is removing fuel: the mixture is rich. "
            "An air leak cannot cause this."
        )
        return {
            "cause": "Over-fuelling: injector leak, fuel pressure regulation or a contaminated air mass sensor reading high",
            "confidence": "medium",
            "evidence": evidence,
            "plan": [
                "Check fuel pressure against specification, hot and cold.",
                "Inspect injectors for leak-down; compare cylinder contributions.",
                "Check for oil contamination on the MAF element causing a high reading.",
            ],
        }

    evidence.append("Live values do not fit a single mixture hypothesis.")
    return {
        "cause": None,
        "confidence": "low",
        "evidence": evidence,
        "plan": [
            "Record MAF, trim, manifold pressure and idle speed across the load range.",
            "Compare against a known-good vehicle of the same model.",
            "Re-read 0x19 after a drive cycle to see which codes are current.",
        ],
    }


if __name__ == "__main__":
    import sys

    from scan import full_scan

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    print(json.dumps(triage(full_scan(host)), indent=2))
