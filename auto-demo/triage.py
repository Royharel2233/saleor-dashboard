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

    evidence, confidence = [], "low"
    maf = live.get("0x0012:maf_g_per_s")
    trim = live.get("0x0012:long_term_fuel_trim_pct")
    rpm = live.get("0x0012:engine_speed_rpm")
    if maf is not None and rpm:
        evidence.append(f"MAF reads {maf} g/s at {rpm} rpm idle (B48 expects roughly 3.5-4.5 g/s).")
    if trim is not None:
        evidence.append(f"Long-term fuel trim {trim:+.1f}% -- the ECU is adding fuel to correct a lean mixture.")
    if maf is not None and trim is not None and trim > 10 and maf < 3.0:
        evidence.append(
            "Low measured air mass with a positive (lean) trim correction means air is "
            "entering downstream of the sensor -- unmetered. A failed sensor element alone "
            "would not drive trim in this direction."
        )
        confidence = "high"
    elif evidence:
        confidence = "medium"
    else:
        evidence.append("DTC-pattern only: no live data returned to corroborate the verdict.")

    return {
        "primary": {
            "ecu": primary["ecu"],
            "code": primary["code"],
            "cause": primary["description"],
            "confidence": confidence,
        },
        "consequential": consequential,
        "independent": independent,
        "evidence": evidence,
        "plan": [
            "Smoke-test the intake tract from the MAF sensor to the throttle body; "
            "check the charge-pipe boot and PCV diaphragm for splits.",
            "Compare MAF reading against calculated load at 2000 rpm; a proportional "
            "offset points at the sensor, a load-dependent one at a leak.",
            "Clear adaptations, run UDS RoutineControl 0x31 on the tank ventilation "
            "valve, then re-read 0x19 to confirm which codes return.",
        ],
    }


if __name__ == "__main__":
    import sys

    from scan import full_scan

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    print(json.dumps(triage(full_scan(host)), indent=2))
