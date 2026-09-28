"""Discrimination test: does the triage engine tell the scenarios apart?

Starts the simulator once per fault scenario on its own port, scans it, triages
it, and prints what came back. A row where two different vehicle states produce
the same cause is a failure of the engine, not of the test.
"""

import struct
import subprocess
import sys
import time

from doipclient import DoIPClient

from baseline import Baseline
from doip_simulator import SCENARIOS
from scan import LIVE_DIDS, full_scan, raw_uds
from triage import triage

BASE_PORT = 13500
BASELINE_SAMPLES = 60


def sample_live(port: int, samples: int = BASELINE_SAMPLES):
    """Read the DME's live channels repeatedly to capture their spread.

    This is what recording a healthy baseline means in practice: the vehicle is
    known good, and you log it until you know what its normal looks like.
    """
    collected = {name: [] for _, (name, _) in LIVE_DIDS.items()}
    client = DoIPClient("127.0.0.1", 0x0012, tcp_port=port)
    try:
        raw_uds(client, bytes([0x10, 0x03]))
        for _ in range(samples):
            for did, (name, decode) in LIVE_DIDS.items():
                response = raw_uds(client, bytes([0x22]) + struct.pack(">H", did), timeout=1.0)
                if response and response[0] == 0x62:
                    collected[name].append(decode(response[3:]))
    finally:
        client.close()
    return collected


def start(name: str, port: int):
    process = subprocess.Popen(
        [sys.executable, "doip_simulator.py", "--host", "127.0.0.1",
         "--tcp-port", str(port), "--scenario", name, "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    return process


def build_baseline(port_offset: int = 90):
    """Record the vehicle's healthy idle, exactly as a workshop would."""
    port = BASE_PORT + port_offset
    process = start("healthy", port)
    try:
        samples = sample_live(port)
    finally:
        process.terminate()
        process.wait(timeout=5)
    return Baseline.from_samples(
        name="simulated F30 330i (B48)",
        source=f"{BASELINE_SAMPLES} idle reads of the healthy scenario",
        samples=samples,
    )


def run_scenario(name: str, port: int, reference=None):
    process = start(name, port)
    try:
        scan = full_scan("127.0.0.1", port)
        return scan, triage(scan, reference)
    finally:
        process.terminate()
        process.wait(timeout=5)


def main():
    reference = build_baseline()
    print(reference.describe())
    reference.save("baseline_simulated_b48.json")

    causes = {}
    for index, name in enumerate(sorted(SCENARIOS)):
        scan, verdict = run_scenario(name, BASE_PORT + index, reference)
        total = sum(len(e["dtcs"]) for e in scan["ecus"])
        codes = ",".join(d["code"] for e in scan["ecus"] for d in e["dtcs"]) or "-"
        primary = verdict["primary"]
        cause = primary["cause"] if primary else "no faults"
        print(f"\n### {name}  ({SCENARIOS[name]['description']})")
        print(f"    stored DTCs : {total}  [{codes}]")
        print(f"    verdict     : {cause}")
        print(f"    confidence  : {primary['confidence'] if primary else '-'}")
        print(f"    consequential: {len(verdict['consequential'])}  independent: {len(verdict['independent'])}")
        print(f"    plan step 1 : {verdict['plan'][0][:96]}")
        causes.setdefault(cause, []).append(name)

    print("\n=== discrimination ===")
    collisions = {c: n for c, n in causes.items() if len(n) > 1}
    for cause, names in causes.items():
        print(f"  {len(names)}x  {cause[:78]}")
    print(f"\n{len(causes)} distinct verdicts across {len(SCENARIOS)} scenarios")
    if collisions:
        print("COLLISIONS (different vehicle states, same verdict):")
        for cause, names in collisions.items():
            print(f"  {names} -> {cause}")
    else:
        print("No collisions: every scenario produced its own verdict.")


if __name__ == "__main__":
    main()
