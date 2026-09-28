"""Discrimination test: does the triage engine tell the scenarios apart?

Starts the simulator once per fault scenario on its own port, scans it, triages
it, and prints what came back. A row where two different vehicle states produce
the same cause is a failure of the engine, not of the test.
"""

import subprocess
import sys
import time

from doip_simulator import SCENARIOS
from scan import full_scan
from triage import triage

BASE_PORT = 13500


def run_scenario(name: str, port: int):
    process = subprocess.Popen(
        [sys.executable, "doip_simulator.py", "--host", "127.0.0.1",
         "--tcp-port", str(port), "--scenario", name, "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        scan = full_scan("127.0.0.1", port)
        return scan, triage(scan)
    finally:
        process.terminate()
        process.wait(timeout=5)


def main():
    causes = {}
    for index, name in enumerate(sorted(SCENARIOS)):
        scan, verdict = run_scenario(name, BASE_PORT + index)
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
