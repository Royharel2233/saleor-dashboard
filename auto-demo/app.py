"""Streamlit front end for the DoIP diagnostic engine.

Everything shown is read from the vehicle at click time -- stop the simulator
and the app reports a connection failure instead of a canned result.
"""

import json
import os
import time

import streamlit as st

from baseline import Baseline
from scan import full_scan
from triage import triage

BASELINE_PATH = "baseline_simulated_b48.json"


def load_baseline():
    """A verdict is expressed against what this vehicle reads when healthy.
    Without that reference the engine reports readings and declines a cause."""
    if os.path.exists(BASELINE_PATH):
        return Baseline.load(BASELINE_PATH)
    return None

st.set_page_config(page_title="AI Vehicle Diagnostic Triage", layout="wide")

st.title("Autonomous Vehicle Diagnostic Engine")
st.caption("DoIP / ISO 13400 transport · UDS / ISO 14229 services · live root-cause triage")

with st.sidebar:
    st.header("Transport")
    host = st.text_input("Gateway address", value="127.0.0.1", help="169.254.1.1 with a BMW ENET cable")
    port = st.number_input("TCP port", value=13400, step=1)
    st.caption("Simulator: `python doip_simulator.py`")
    st.divider()
    st.header("Baseline")
    reference = load_baseline()
    if reference:
        st.success(f"Loaded: {reference.name}")
        st.caption(reference.source)
    else:
        st.warning("No baseline recorded. The engine will report readings but "
                   "decline to name a cause — healthy idle values vary more "
                   "between vehicles than a fault moves them within one.")
        st.caption("Record one with `python discriminate.py`.")

if st.button("RUN FULL VEHICLE SCAN", type="primary"):
    with st.spinner(f"Routing activation and UDS scan over DoIP ({host}:{port})..."):
        started = time.time()
        try:
            scan = full_scan(host, int(port))
        except (ConnectionRefusedError, OSError, TimeoutError) as exc:
            st.error(f"No DoIP entity at {host}:{port} — {type(exc).__name__}: {exc}")
            st.info("Start the simulator with `python doip_simulator.py`, or plug in the ENET cable.")
            st.stop()
        verdict = triage(scan, reference)
        elapsed = time.time() - started

    total_dtcs = sum(len(ecu["dtcs"]) for ecu in scan["ecus"])
    st.success(f"Scan complete — VIN {scan['vin']} · {len(scan['ecus'])} ECUs · {total_dtcs} stored DTCs")

    metrics = st.columns(4)
    metrics[0].metric("VIN", scan["vin"][-8:])
    metrics[1].metric("ECUs scanned", len(scan["ecus"]))
    metrics[2].metric("Stored DTCs", total_dtcs)
    metrics[3].metric("Scan time", f"{elapsed:.2f}s")

    left, right = st.columns(2)

    with left:
        st.subheader("Raw DTC memory")
        for ecu in scan["ecus"]:
            with st.expander(f"{ecu['name']} — {ecu['address']} ({len(ecu['dtcs'])} DTC)", expanded=True):
                if ecu["dtcs"]:
                    st.table(
                        [
                            {
                                "Code": d["code"],
                                "Status": d["status_byte"],
                                "Description": d["description"],
                            }
                            for d in ecu["dtcs"]
                        ]
                    )
                else:
                    st.write("No stored faults.")
                st.code(f"0x19 0x02 response: {ecu['raw_0x19_response']}", language="text")
                if ecu["live_data"]:
                    st.json(ecu["live_data"])

    with right:
        st.subheader("Root-cause triage")
        st.caption(f"Engine: `{verdict['engine']}`")
        if verdict.get("engine_error"):
            st.warning(f"LLM call failed, fell back to rules — {verdict['engine_error']}")

        primary = verdict["primary"]
        if primary:
            st.error(
                f"PRIMARY FAULT · {primary['ecu']} {primary['code']} — {primary['cause']} "
                f"(confidence: {primary['confidence']})"
            )
        else:
            st.success("No faults to triage.")

        if verdict["consequential"]:
            st.info("Filtered as consequential:")
            for item in verdict["consequential"]:
                st.write(f"- **{item['ecu']} {item['code']}** — {item['mechanism']}")

        if verdict["independent"]:
            st.warning("Unlinked, inspect separately:")
            for item in verdict["independent"]:
                st.write(f"- **{item['ecu']} {item['code']}** — {item['note']}")

        st.markdown("### Supporting evidence")
        for line in verdict["evidence"]:
            st.write(f"- {line}")

        st.markdown("### Recommended action plan")
        for index, step in enumerate(verdict["plan"], start=1):
            st.write(f"{index}. {step}")

    with st.expander("Full scan payload (JSON)"):
        st.json(scan)
    st.download_button(
        "Download scan + verdict",
        data=json.dumps({"scan": scan, "verdict": verdict}, indent=2),
        file_name=f"scan_{scan['vin']}.json",
        mime="application/json",
    )
else:
    st.info("Start the simulator, then run a scan. Faults are read live over the DoIP socket.")
