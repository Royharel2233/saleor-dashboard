"""DoIP/UDS scan client: talks to a real gateway or to doip_simulator.py.

Nothing here is simulated -- it opens a TCP socket, performs ISO 13400 routing
activation, and issues real UDS services. Point --host at 169.254.1.1 with an
ENET cable and the same code runs against a car.
"""

import argparse
import json
import struct
import time

from doipclient import DoIPClient
from doipclient.connectors import DoIPClientUDSConnector
from udsoncan.client import Client as UdsClient
from udsoncan.configs import default_client_config
from udsoncan.exceptions import NegativeResponseException, TimeoutException

# Logical addresses of the ECUs worth interrogating on an F-series bus.
ECU_MAP = [
    (0x0012, "DME (Engine)"),
    (0x0029, "DSC (Brakes)"),
    (0x0040, "BDC (Body)"),
]

# DTC text lookup. On a real car this comes from the manufacturer's fault
# tables; here it is a local dictionary so the scan output stays readable.
DTC_TEXT = {
    0x101E01: "Air mass sensor, plausibility: signal too low",
    0x10A204: "Mixture adaptation bank 1, additive: limit exceeded",
    0x10A205: "Mixture adaptation bank 1, additive: limit exceeded (rich)",
    0x480AB2: "Interface to DME: implausible torque signal",
    0xD35A11: "Signal invalid: engine data message missing",
}

LIVE_DIDS = {
    0x4001: ("maf_g_per_s", lambda b: struct.unpack(">H", b)[0] / 100.0),
    0x4002: ("long_term_fuel_trim_pct", lambda b: struct.unpack(">h", b)[0] / 100.0),
    0x4003: ("engine_speed_rpm", lambda b: struct.unpack(">H", b)[0]),
    0x4004: ("intake_manifold_pressure_hpa", lambda b: struct.unpack(">H", b)[0]),
}

STATUS_BITS = [
    (0x01, "testFailed"),
    (0x02, "testFailedThisOperationCycle"),
    (0x04, "pendingDTC"),
    (0x08, "confirmedDTC"),
    (0x10, "testNotCompletedSinceLastClear"),
    (0x20, "testFailedSinceLastClear"),
    (0x40, "testNotCompletedThisOperationCycle"),
    (0x80, "warningIndicatorRequested"),
]


def decode_status(status: int):
    return [name for bit, name in STATUS_BITS if status & bit]


def raw_uds(client: DoIPClient, payload: bytes, timeout: float = 2.0) -> bytes:
    """Send a UDS request to the client's ECU and return the raw response.

    One DoIPClient is bound to one logical address: it drops diagnostic messages
    whose source is any other ECU, so each ECU gets its own activated socket.
    """
    client.send_diagnostic(bytearray(payload), timeout=timeout)
    return bytes(client.receive_diagnostic(timeout=timeout))


def read_vin(client: DoIPClient):
    response = raw_uds(client, bytes([0x22, 0xF1, 0x90]))
    if response[0] == 0x62:
        return response[3:].decode("ascii", errors="replace")
    return None


def scan_ecu(host: str, port: int, address: int, name: str):
    result = {"address": f"0x{address:04X}", "name": name, "dtcs": [], "live_data": {}}
    client = DoIPClient(host, address, tcp_port=port)
    try:
        _scan_ecu_on(client, result)
    finally:
        client.close()
    return result


def _scan_ecu_on(client: DoIPClient, result: dict):
    # Extended diagnostic session, then keep-alive, exactly as a tester would.
    session = raw_uds(client, bytes([0x10, 0x03]))
    result["session_response"] = session.hex(" ").upper()
    raw_uds(client, bytes([0x3E, 0x00]))

    # 0x19 0x02: report DTCs by status mask (confirmed + testFailed).
    response = raw_uds(client, bytes([0x19, 0x02, 0xFF]))
    result["raw_0x19_response"] = response.hex(" ").upper()
    if response[0] == 0x59:
        body = response[3:]
        for offset in range(0, len(body) - 3, 4):
            number = int.from_bytes(body[offset : offset + 3], "big")
            status = body[offset + 3]
            result["dtcs"].append(
                {
                    "code": f"{number:06X}",
                    "status_byte": f"0x{status:02X}",
                    "status": decode_status(status),
                    "description": DTC_TEXT.get(number, "unknown (no local text entry)"),
                }
            )

    for did, (key, decode) in LIVE_DIDS.items():
        try:
            response = raw_uds(client, bytes([0x22]) + struct.pack(">H", did), timeout=1.0)
        except (TimeoutException, TimeoutError, IOError):
            continue
        if response[0] == 0x62:
            result["live_data"][key] = decode(response[3:])


def full_scan(host: str, port: int = 13400, gateway_address: int = 0x0010):
    started = time.time()
    activation = {"gateway": f"0x{gateway_address:04X}", "transport": f"DoIP tcp://{host}:{port}"}
    gateway = DoIPClient(host, gateway_address, tcp_port=port)
    try:
        vin = read_vin(gateway)
    finally:
        gateway.close()
    ecus = [scan_ecu(host, port, address, name) for address, name in ECU_MAP]
    return {
        "vin": vin,
        "connection": activation,
        "scan_seconds": round(time.time() - started, 3),
        "ecus": ecus,
    }


def main():
    parser = argparse.ArgumentParser(description="DoIP/UDS vehicle scan")
    parser.add_argument("--host", default="127.0.0.1", help="169.254.1.1 for a real ENET cable")
    parser.add_argument("--port", type=int, default=13400)
    args = parser.parse_args()
    print(json.dumps(full_scan(args.host, args.port), indent=2))


if __name__ == "__main__":
    main()
