"""MCP server exposing DoIP/UDS vehicle diagnostics.

Read and write services are separate tools with separate gating, because the
distinction is not cosmetic: 0x22 and 0x19 observe an ECU, while 0x14 erases
fault memory a technician may still need and 0x31 actuates hardware on a
vehicle someone may be standing next to.

Read tools are always available. Write tools refuse unless the operator has
opted in out of band:

    VEHICLE_MCP_ALLOW_WRITES=1

and the caller passes an explicit confirmation and a reason. Without the
environment variable no prompt, jailbreak or agent loop can reach them.

Transport: stdio (local). Run with:
    python mcp_server.py
"""

import json
import os
import struct
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

import scan
from triage import triage as run_triage

mcp = MCPServer(
    name="vehicle_diagnostics_mcp",
    title="Vehicle Diagnostics (DoIP/UDS)",
    version="0.1.0",
    instructions=(
        "Reads diagnostic data from a road vehicle over DoIP (ISO 13400) using UDS "
        "(ISO 14229). Start with vehicle_list_ecus to see the bus topology, then "
        "vehicle_scan_all for a full picture, then vehicle_triage_scan to reduce it "
        "to a root cause. Fault codes are manufacturer 3-byte hex values, not "
        "generic OBD-II Pxxxx codes. Tools that modify the vehicle are disabled "
        "unless the operator has enabled them; never assume a write succeeded."
    ),
)

WRITES_ENABLED_VAR = "VEHICLE_MCP_ALLOW_WRITES"

Host = Annotated[
    str,
    Field(
        default="127.0.0.1",
        description="DoIP gateway address. 127.0.0.1 for the local simulator, "
        "169.254.1.1 for a BMW ENET cable on a real vehicle.",
    ),
]
Port = Annotated[int, Field(default=13400, ge=1, le=65535, description="DoIP TCP port (ISO 13400 default 13400).")]
EcuAddress = Annotated[
    str,
    Field(
        description="ECU logical address as hex, e.g. '0x0012' (DME), '0x0029' (DSC), "
        "'0x0040' (BDC). Use vehicle_list_ecus to enumerate.",
        pattern=r"^0[xX][0-9a-fA-F]{1,4}$",
    ),
]


class ConnectionError_(Exception):
    """Raised with an actionable message when the vehicle cannot be reached."""


def _parse_address(text: str) -> int:
    return int(text, 16)


def _connect_hint(host: str, port: int, exc: Exception) -> str:
    return (
        f"Could not reach a DoIP entity at {host}:{port} ({type(exc).__name__}: {exc}). "
        f"If you meant the simulator, start it with `python doip_simulator.py`. "
        f"If you meant a real vehicle, check the ENET cable is in the OBD-II port, "
        f"the ignition is on, and the address is 169.254.1.1."
    )


def _writes_enabled() -> bool:
    return os.environ.get(WRITES_ENABLED_VAR, "").strip().lower() in {"1", "true", "yes"}


def _refuse_write(tool: str) -> str:
    return json.dumps(
        {
            "status": "refused",
            "tool": tool,
            "reason": f"Vehicle-modifying tools are disabled. The operator must set "
            f"{WRITES_ENABLED_VAR}=1 in the server's environment and restart it.",
            "next_step": "Report this to the user. Do not retry; no argument to this "
            "tool can enable it. Read-only tools remain available.",
        },
        indent=2,
    )


# --------------------------------------------------------------------------
# Read-only tools
# --------------------------------------------------------------------------

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


@mcp.tool(name="vehicle_list_ecus", title="List known ECUs", annotations=READ_ONLY)
def vehicle_list_ecus() -> str:
    """List the ECU logical addresses this server knows how to interrogate.

    Takes no arguments and opens no connection. Call this first to learn which
    addresses the other tools accept.

    Returns: JSON list of {address, name}.
    """
    return json.dumps(
        {"ecus": [{"address": f"0x{address:04X}", "name": name} for address, name in scan.ECU_MAP],
         "gateway": "0x0010"},
        indent=2,
    )


@mcp.tool(name="vehicle_identify", title="Read VIN from gateway", annotations=READ_ONLY)
def vehicle_identify(host: Host = "127.0.0.1", port: Port = 13400) -> str:
    """Read the vehicle identification number from the gateway (UDS 0x22, DID 0xF190).

    The cheapest way to confirm the transport works and establish which vehicle
    is connected before interpreting any fault codes.

    Returns: JSON {vin, gateway, transport}.
    """
    from doipclient import DoIPClient

    try:
        client = DoIPClient(host, 0x0010, tcp_port=int(port))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)
    try:
        vin = scan.read_vin(client)
    finally:
        client.close()
    return json.dumps({"vin": vin, "gateway": "0x0010", "transport": f"DoIP tcp://{host}:{port}"}, indent=2)


@mcp.tool(name="vehicle_scan_all", title="Full vehicle scan", annotations=READ_ONLY)
def vehicle_scan_all(host: Host = "127.0.0.1", port: Port = 13400) -> str:
    """Scan every known ECU: stored DTCs with decoded status bits, plus live data.

    This is the broad first look. It opens one activated DoIP socket per ECU and
    issues 0x10 (extended session), 0x3E, 0x19 0x02 (DTCs by status mask) and
    0x22 for each supported data identifier. Read-only throughout.

    Returns: JSON {vin, connection, scan_seconds, ecus[]} where each ECU carries
    dtcs[] (code, status_byte, decoded status bits, description), live_data and
    the raw 0x19 response bytes for verification.
    """
    try:
        return json.dumps(scan.full_scan(host, int(port)), indent=2)
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)


@mcp.tool(name="vehicle_read_dtcs", title="Read one ECU's fault memory", annotations=READ_ONLY)
def vehicle_read_dtcs(ecu_address: EcuAddress, host: Host = "127.0.0.1", port: Port = 13400) -> str:
    """Read stored DTCs from a single ECU (UDS 0x19 subfunction 0x02).

    Prefer vehicle_scan_all unless you already know which ECU matters; a
    single-ECU read cannot show you the cross-ECU pattern that distinguishes a
    root cause from its consequences.

    Returns: JSON for that ECU only.
    """
    address = _parse_address(ecu_address)
    name = dict(scan.ECU_MAP).get(address, f"unknown ECU {ecu_address}")
    try:
        return json.dumps(scan.scan_ecu(host, int(port), address, name), indent=2)
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)


@mcp.tool(name="vehicle_read_data_identifier", title="Read a data identifier", annotations=READ_ONLY)
def vehicle_read_data_identifier(
    ecu_address: EcuAddress,
    did: Annotated[str, Field(description="Data identifier as 4 hex digits, e.g. '0xF190' (VIN) or '0x4001'.", pattern=r"^0[xX][0-9a-fA-F]{1,4}$")],
    host: Host = "127.0.0.1",
    port: Port = 13400,
) -> str:
    """Read one arbitrary data identifier from an ECU (UDS 0x22).

    Returns the raw response bytes as hex. Interpretation is vehicle-specific:
    do not guess a scaling factor. If the ECU answers 0x7F 0x22 0x31 the
    identifier is not supported there, which is a valid answer, not a failure.

    Returns: JSON {ecu, did, raw_response, payload_hex, supported}.
    """
    from doipclient import DoIPClient

    address = _parse_address(ecu_address)
    did_value = int(did, 16)
    try:
        client = DoIPClient(host, address, tcp_port=int(port))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)
    try:
        response = scan.raw_uds(client, bytes([0x22]) + struct.pack(">H", did_value))
    except (TimeoutError, IOError) as exc:
        return json.dumps({"status": "error", "message": f"ECU {ecu_address} did not answer: {exc}"}, indent=2)
    finally:
        client.close()

    supported = bool(response) and response[0] == 0x62
    return json.dumps(
        {
            "ecu": ecu_address,
            "did": f"0x{did_value:04X}",
            "raw_response": response.hex(" ").upper(),
            "payload_hex": response[3:].hex(" ").upper() if supported else None,
            "supported": supported,
        },
        indent=2,
    )


@mcp.tool(name="vehicle_triage_scan", title="Root-cause triage of a scan", annotations=READ_ONLY)
def vehicle_triage_scan(host: Host = "127.0.0.1", port: Port = 13400) -> str:
    """Scan the vehicle, then reduce the fault set to one root cause.

    Separates consequential faults (a downstream ECU reporting an invalid signal
    because its upstream source is unhealthy) from independent ones, and cites
    the live values supporting the verdict. The reported `engine` field says
    whether an LLM or the deterministic model produced it -- surface that to the
    user rather than presenting the verdict as unconditional.

    Returns: JSON {primary, consequential[], independent[], evidence[], plan[], engine}.
    """
    try:
        scan_result = scan.full_scan(host, int(port))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)
    return json.dumps({"scan": scan_result, "verdict": run_triage(scan_result)}, indent=2)


# --------------------------------------------------------------------------
# Write tools -- gated
# --------------------------------------------------------------------------

DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


@mcp.tool(name="vehicle_clear_dtcs", title="Clear fault memory (destructive)", annotations=DESTRUCTIVE)
def vehicle_clear_dtcs(
    ecu_address: EcuAddress,
    confirm: Annotated[bool, Field(description="Must be true. Set it only after the user has explicitly asked for fault memory to be erased.")],
    reason: Annotated[str, Field(description="Why the clear is being performed, recorded in the response.", min_length=8, max_length=300)],
    host: Host = "127.0.0.1",
    port: Port = 13400,
) -> str:
    """Erase an ECU's stored fault memory (UDS 0x14). Destructive and irreversible.

    Clearing discards the evidence any later diagnosis would rely on, including
    occurrence counters and freeze frames. Read the fault memory first and
    preserve it. Do not call this to 'reset' or 'tidy' a vehicle, and never as a
    step toward another goal -- only when the user has asked for it directly.

    Returns: JSON {status, ecu, raw_response} or a refusal.
    """
    from doipclient import DoIPClient

    if not _writes_enabled():
        return _refuse_write("vehicle_clear_dtcs")
    if not confirm:
        return json.dumps(
            {"status": "refused", "reason": "confirm was not true.",
             "next_step": "Ask the user to confirm they want fault memory erased, and tell them it cannot be undone."},
            indent=2,
        )

    address = _parse_address(ecu_address)
    try:
        client = DoIPClient(host, address, tcp_port=int(port))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)
    try:
        scan.raw_uds(client, bytes([0x10, 0x03]))
        response = scan.raw_uds(client, bytes([0x14, 0xFF, 0xFF, 0xFF]))
    except (TimeoutError, IOError) as exc:
        return json.dumps({"status": "error", "message": f"Clear not confirmed by ECU: {exc}"}, indent=2)
    finally:
        client.close()

    ok = bool(response) and response[0] == 0x54
    return json.dumps(
        {
            "status": "cleared" if ok else "rejected",
            "ecu": ecu_address,
            "reason": reason,
            "raw_response": response.hex(" ").upper(),
            "note": None if ok else "ECU returned a negative response; fault memory was NOT cleared.",
        },
        indent=2,
    )


@mcp.tool(name="vehicle_run_routine", title="Run a diagnostic routine (actuates hardware)", annotations=DESTRUCTIVE)
def vehicle_run_routine(
    ecu_address: EcuAddress,
    routine_id: Annotated[str, Field(description="Routine identifier as 4 hex digits, e.g. '0x0203'.", pattern=r"^0[xX][0-9a-fA-F]{1,4}$")],
    action: Annotated[Literal["start", "stop", "request_results"], Field(description="start = subfunction 0x01, stop = 0x02, request_results = 0x03.")],
    confirm: Annotated[bool, Field(description="Must be true. Routines move physical hardware.")],
    reason: Annotated[str, Field(description="Why this routine is being run.", min_length=8, max_length=300)],
    host: Host = "127.0.0.1",
    port: Port = 13400,
) -> str:
    """Start, stop or query a diagnostic routine (UDS 0x31). Actuates vehicle hardware.

    A routine can open valves, run pumps, cycle relays and move actuators. On a
    real vehicle someone may have hands in the engine bay. Confirm with the user
    that the vehicle is in a safe state first, and prefer `request_results` over
    `start` when you only need the outcome of a routine already run.

    Returns: JSON {status, routine, raw_response} or a refusal.
    """
    from doipclient import DoIPClient

    if not _writes_enabled():
        return _refuse_write("vehicle_run_routine")
    if not confirm:
        return json.dumps(
            {"status": "refused", "reason": "confirm was not true.",
             "next_step": "Confirm with the user that the vehicle is safe to actuate before retrying."},
            indent=2,
        )

    subfunction = {"start": 0x01, "stop": 0x02, "request_results": 0x03}[action]
    address = _parse_address(ecu_address)
    routine = int(routine_id, 16)
    try:
        client = DoIPClient(host, address, tcp_port=int(port))
    except (ConnectionRefusedError, OSError, TimeoutError) as exc:
        return json.dumps({"status": "error", "message": _connect_hint(host, port, exc)}, indent=2)
    try:
        scan.raw_uds(client, bytes([0x10, 0x03]))
        response = scan.raw_uds(client, bytes([0x31, subfunction]) + struct.pack(">H", routine))
    except (TimeoutError, IOError) as exc:
        return json.dumps({"status": "error", "message": f"Routine not confirmed by ECU: {exc}"}, indent=2)
    finally:
        client.close()

    ok = bool(response) and response[0] == 0x71
    return json.dumps(
        {
            "status": "accepted" if ok else "rejected",
            "ecu": ecu_address,
            "routine": f"0x{routine:04X}",
            "action": action,
            "reason": reason,
            "raw_response": response.hex(" ").upper(),
            "note": None if ok else "ECU returned a negative response; the routine did NOT run.",
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
