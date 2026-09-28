"""The full diagnostic cycle against one vehicle, printed as a transcript.

    routing activation
      -> extended session
        -> read fault memory            (0x19 0x02)
          -> read each fault's freeze frame (0x19 0x04)
            -> compare against the vehicle's healthy baseline
              -> verdict

The freeze frame matters more than live data, and for a practical reason: the
fault usually will not reproduce on demand while the car sits at idle in a
workshop. The ECU already recorded the conditions at the moment the code set,
and that is the measurement worth reasoning about.

It carries a constraint that is easy to miss. A baseline recorded at idle can
only referee a snapshot taken at idle. A freeze frame captured at 2,500 rpm
under load compared against an idle baseline would make normal running look
catastrophic, so this refuses the comparison instead.

    python cycle.py --scenario intake_leak
"""

import argparse
import json
import subprocess
import sys
import time

from doipclient import DoIPClient

import scan
from baseline import Baseline
from triage import MAF, MAP, RPM, TRIM, triage

BASELINE_PATH = "baseline_simulated_b48.json"


def conditions_match(snapshot_values: dict, reference: Baseline):
    """Is the freeze frame close enough to the baseline's conditions to compare?"""
    rpm = snapshot_values.get(RPM)
    channel = reference.channel(RPM) if reference else None
    if rpm is None or channel is None:
        return True, "no engine speed recorded in the frame; conditions unverified"
    # Allow a wide margin: the question is idle vs not idle, not a tight match.
    low, high = channel.p5 * 0.8, channel.p95 * 1.5
    if low <= rpm <= high:
        return True, f"frame captured at {rpm} rpm, within the baseline's idle conditions"
    return False, (
        f"frame captured at {rpm} rpm, outside the baseline's idle range "
        f"({channel.p5:.0f}..{channel.p95:.0f}). An idle baseline cannot referee it."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="intake_leak")
    parser.add_argument("--port", type=int, default=13650)
    args = parser.parse_args()

    process = subprocess.Popen(
        [sys.executable, "doip_simulator.py", "--host", "127.0.0.1",
         "--tcp-port", str(args.port), "--scenario", args.scenario, "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    try:
        reference = Baseline.load(BASELINE_PATH)
    except OSError:
        reference = None

    try:
        print(f"[1] DoIP routing activation -> 127.0.0.1:{args.port}")
        gateway = DoIPClient("127.0.0.1", 0x0010, tcp_port=args.port)
        vin = scan.read_vin(gateway)
        gateway.close()
        print(f"    gateway 0x0010 answered, VIN {vin}")

        print("\n[2] fault memory, every ECU (0x10 0x03, then 0x19 0x02)")
        scan_result = scan.full_scan("127.0.0.1", args.port)
        for ecu in scan_result["ecus"]:
            codes = ", ".join(d["code"] for d in ecu["dtcs"]) or "none"
            print(f"    {ecu['address']}  {ecu['name']:<14} {codes}")

        print("\n[3] freeze frames for the DME's codes (0x19 0x04)")
        dme = next(e for e in scan_result["ecus"] if e["address"] == "0x0012")
        client = DoIPClient("127.0.0.1", 0x0012, tcp_port=args.port)
        frames = {}
        try:
            scan.raw_uds(client, bytes([0x10, 0x03]))
            for dtc in dme["dtcs"]:
                snapshot = scan.read_snapshot(client, int(dtc["code"], 16))
                if not snapshot["supported"]:
                    print(f"    {dtc['code']}  no snapshot stored")
                    continue
                for number, record in snapshot["records"].items():
                    print(f"    {dtc['code']}  record {number}: {record['values']}")
                    if record["unparsed_tail"]:
                        print(f"        unparsed tail (identifier length unknown): "
                              f"{record['unparsed_tail']}")
                    frames.setdefault(dtc["code"], record["values"])
        finally:
            client.close()

        if not frames:
            print("\n[4] no freeze frame to reason about; falling back to live data")
            conditions = dme["live_data"]
        else:
            first_code = sorted(frames)[0]
            conditions = frames[first_code]
            print(f"\n[4] reasoning from the freeze frame of {first_code}, "
                  f"not from workshop idle")

        usable, note = conditions_match(conditions, reference)
        print(f"    {note}")
        if not usable:
            print("    comparison refused.")
            conditions = {}

        print("\n[5] verdict against the vehicle's healthy baseline")
        if reference is None:
            print("    no baseline recorded for this vehicle")
        else:
            print(f"    {reference.name}: {reference.source}")
        scan_result["ecus"][0]["live_data"] = conditions
        verdict = triage(scan_result, reference)
        primary = verdict["primary"]
        print(f"\n    PRIMARY   : {primary['cause'] if primary else 'no faults'}")
        print(f"    confidence: {primary['confidence'] if primary else '-'}")
        for item in verdict["consequential"]:
            print(f"    filtered  : {item['ecu']} {item['code']}")
        print("\n    evidence:")
        for line in verdict["evidence"]:
            print(f"      - {line}")
        print("\n    plan:")
        for index, step in enumerate(verdict["plan"], 1):
            print(f"      {index}. {step}")
    finally:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    main()
