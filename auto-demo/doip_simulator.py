"""Minimal ISO 13400 (DoIP) vehicle simulator with a UDS (ISO 14229) ECU stack.

Stands in for a BMW ENET cable + gateway so the diagnostic app can be developed
and demonstrated with no vehicle and no hardware present.

Implements, server side:
  UDP 13400  0x0001 vehicle identification request -> 0x0004 response
  TCP 13400  0x0005 routing activation request     -> 0x0006 response
             0x0007 alive check request            -> 0x0008 response
             0x8001 diagnostic message             -> 0x8002 ack + 0x8001 UDS response

UDS services per ECU: 0x10, 0x3E, 0x22, 0x19 (sub 0x01/0x02/0x06), 0x14, 0x31.
"""

import argparse
import logging
import random
import socket
import socketserver
import struct
import threading

from doipclient.messages import (
    AliveCheckResponse,
    DiagnosticMessage,
    DiagnosticMessagePositiveAcknowledgement,
    GenericDoIPNegativeAcknowledge,
    RoutingActivationResponse,
    VehicleIdentificationResponse,
)

logger = logging.getLogger("doip-sim")

TCP_PORT = 13400
UDP_PORT = 13400
GATEWAY_ADDRESS = 0x0010

VEHICLE = {
    "vin": "WBA8E9G51GNT12345",
    "model": "BMW F30 330i (B48, 2016)",
    "eid": b"\xde\xad\xbe\xef\x00\x01",
    "gid": b"\x00\x00\x00\x00\x00\x00",
}

NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_REQUEST_OUT_OF_RANGE = 0x31


class Ecu:
    """One simulated ECU: a logical address, a DTC memory, and a DID table."""

    def __init__(self, address, name, dtcs, dids, snapshots=None):
        self.address = address
        self.name = name
        # dtcs: list of (dtc_number:int 3-byte, status:int, text:str)
        self.dtcs = list(dtcs)
        self.dids = dict(dids)
        # snapshots: {dtc_number: {record_number: {did: packed bytes}}}
        # The freeze frame: what the ECU recorded at the moment the code set.
        self.snapshots = dict(snapshots or {})
        self.session = 0x01

    def handle_uds(self, request: bytes) -> bytes:
        if not request:
            return b""
        sid = request[0]
        if sid == 0x10:
            return self._session_control(request)
        if sid == 0x3E:
            return bytes([0x7E, request[1] if len(request) > 1 else 0x00])
        if sid == 0x22:
            return self._read_data_by_identifier(request)
        if sid == 0x19:
            return self._read_dtc_information(request)
        if sid == 0x14:
            self.dtcs.clear()
            return bytes([0x54])
        if sid == 0x31:
            return self._routine_control(request)
        return self._nrc(sid, NRC_SERVICE_NOT_SUPPORTED)

    @staticmethod
    def _nrc(sid, code):
        return bytes([0x7F, sid, code])

    def _session_control(self, request):
        if len(request) < 2:
            return self._nrc(0x10, NRC_REQUEST_OUT_OF_RANGE)
        sub = request[1] & 0x7F
        if sub not in (0x01, 0x02, 0x03):
            return self._nrc(0x10, NRC_SUBFUNCTION_NOT_SUPPORTED)
        self.session = sub
        # P2server = 50ms, P2*server = 5000ms (10ms resolution)
        return bytes([0x50, sub]) + struct.pack(">HH", 50, 500)

    # Live sensor channels. A real sensor never returns the same number twice,
    # and a baseline built from identical readings would have no spread to
    # measure a deviation against, so these are jittered on every read.
    LIVE_DIDS = {0x4001: ">H", 0x4002: ">h", 0x4003: ">H", 0x4004: ">H"}
    JITTER = 0.03

    def _read_data_by_identifier(self, request):
        if len(request) < 3:
            return self._nrc(0x22, NRC_REQUEST_OUT_OF_RANGE)
        did = struct.unpack_from(">H", request, 1)[0]
        value = self.dids.get(did)
        if value is None:
            return self._nrc(0x22, NRC_REQUEST_OUT_OF_RANGE)
        fmt = self.LIVE_DIDS.get(did)
        if fmt:
            raw = struct.unpack(fmt, value)[0]
            noisy = int(round(raw * (1 + random.gauss(0, self.JITTER))))
            limit = 32767 if fmt == ">h" else 65535
            value = struct.pack(fmt, max(-limit, min(limit, noisy)))
        return bytes([0x62]) + struct.pack(">H", did) + value

    def _read_dtc_information(self, request):
        if len(request) < 2:
            return self._nrc(0x19, NRC_REQUEST_OUT_OF_RANGE)
        sub = request[1]
        mask_available = 0xFF
        if sub == 0x01:  # reportNumberOfDTCByStatusMask
            status_mask = request[2] if len(request) > 2 else 0xFF
            count = sum(1 for _, status, _ in self.dtcs if status & status_mask)
            return bytes([0x59, 0x01, mask_available, 0x01]) + struct.pack(">H", count)
        if sub in (0x02, 0x0A):  # reportDTCByStatusMask / supportedDTC
            status_mask = request[2] if (len(request) > 2 and sub == 0x02) else 0xFF
            body = b""
            for number, status, _ in self.dtcs:
                if status & status_mask:
                    body += number.to_bytes(3, "big") + bytes([status])
            return bytes([0x59, sub, mask_available]) + body
        if sub == 0x04:  # reportDTCSnapshotRecordByDTCNumber
            if len(request) < 6:
                return self._nrc(0x19, NRC_REQUEST_OUT_OF_RANGE)
            number = int.from_bytes(request[2:5], "big")
            wanted = request[5]
            records = self.snapshots.get(number)
            if records is None:
                return self._nrc(0x19, NRC_REQUEST_OUT_OF_RANGE)
            status = next((s for n, s, _ in self.dtcs if n == number), 0x00)
            body = bytes([0x59, 0x04]) + number.to_bytes(3, "big") + bytes([status])
            for record_number, identifiers in sorted(records.items()):
                if wanted not in (0xFF, record_number):
                    continue
                # Per ISO 14229: record number, count of identifiers, then each
                # identifier followed by its data. The data LENGTH is not in the
                # response -- a client needs the OEM's identifier database to
                # know where one value ends and the next begins.
                body += bytes([record_number, len(identifiers)])
                for did, value in sorted(identifiers.items()):
                    body += struct.pack(">H", did) + value
            return body

        if sub == 0x06:  # reportDTCExtendedDataRecordByDTCNumber
            if len(request) < 5:
                return self._nrc(0x19, NRC_REQUEST_OUT_OF_RANGE)
            number = int.from_bytes(request[2:5], "big")
            for dtc_number, status, _ in self.dtcs:
                if dtc_number == number:
                    # record 0x01: occurrence counter, aging counter
                    return (
                        bytes([0x59, 0x06])
                        + number.to_bytes(3, "big")
                        + bytes([status, 0x01, 0x07, 0x00])
                    )
            return self._nrc(0x19, NRC_REQUEST_OUT_OF_RANGE)
        return self._nrc(0x19, NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _routine_control(self, request):
        if len(request) < 4:
            return self._nrc(0x31, NRC_REQUEST_OUT_OF_RANGE)
        sub = request[1]
        routine = struct.unpack_from(">H", request, 2)[0]
        if sub not in (0x01, 0x02, 0x03):
            return self._nrc(0x31, NRC_SUBFUNCTION_NOT_SUPPORTED)
        # routineStatusRecord: 0x00 = completed
        return bytes([0x71, sub]) + struct.pack(">H", routine) + bytes([0x00])


# Fault scenarios. Each one is a whole vehicle state: which codes are stored
# and what the live values read. They exist so the diagnostic engine can be
# tested for DISCRIMINATION -- an engine that returns the same verdict for an
# intake leak, a manifold leak and a dead sensor is not reading the signals.
#
# Live values are encoded as the ECU serves them: MAF and fuel trim x0.01.
SCENARIOS = {
    "intake_leak": {
        "description": "Unmetered air between the MAF and the throttle plate",
        "dme_dtcs": [
            (0x101E01, 0x2F, "Air mass sensor, plausibility: signal too low"),
            (0x10A204, 0x2F, "Mixture adaptation bank 1, additive: limit exceeded"),
        ],
        "dme_live": {0x4001: 210, 0x4002: 2380, 0x4003: 762, 0x4004: 344},
        "downstream": True,
    },
    "manifold_leak": {
        "description": "Vacuum leak downstream of the throttle plate (manifold gasket / PCV)",
        "dme_dtcs": [
            (0x101E01, 0x2F, "Air mass sensor, plausibility: signal too low"),
            (0x10A204, 0x2F, "Mixture adaptation bank 1, additive: limit exceeded"),
        ],
        # Same two codes as intake_leak. The DTCs alone cannot tell these apart;
        # only MAP and idle speed can. Manifold vacuum is lost and idle is high.
        "dme_live": {0x4001: 265, 0x4002: 2010, 0x4003: 1140, 0x4004: 641},
        "downstream": True,
    },
    "maf_signal_fault": {
        "description": "MAF signal implausible with no mixture error: sensor or wiring, not a leak",
        "dme_dtcs": [
            (0x101E01, 0x2F, "Air mass sensor, plausibility: signal too low"),
        ],
        # Low measured air mass, but trim is near zero: the mixture is correct,
        # so no unmetered air is entering. The reading itself is wrong.
        "dme_live": {0x4001: 195, 0x4002: 90, 0x4003: 758, 0x4004: 338},
        "downstream": True,
    },
    "over_fuelling": {
        "description": "Rich mixture: negative trim with normal air mass",
        "dme_dtcs": [
            (0x10A205, 0x2F, "Mixture adaptation bank 1, additive: limit exceeded (rich)"),
        ],
        "dme_live": {0x4001: 402, 0x4002: -1880, 0x4003: 771, 0x4004: 349},
        "downstream": False,
    },
    "healthy": {
        "description": "No stored faults",
        "dme_dtcs": [],
        "dme_live": {0x4001: 398, 0x4002: 120, 0x4003: 768, 0x4004: 347},
        "downstream": False,
    },
}

DOWNSTREAM_DTCS = {
    0x0029: (0x480AB2, 0x2F, "Interface to DME: implausible torque signal"),
    0x0040: (0xD35A11, 0x2F, "Signal invalid: engine data message missing"),
}


def build_vehicle(scenario_name="intake_leak"):
    """Assemble the ECU set for one scenario.

    The DME is the upstream node. When its state is unhealthy the dependent
    ECUs also store a code, which is what the triage engine has to recognise
    as consequential rather than as three separate problems.
    """
    scenario = SCENARIOS[scenario_name]
    vin_bytes = VEHICLE["vin"].encode("ascii")

    dme_dids = {0xF190: vin_bytes, 0xF186: bytes([0x01])}
    for did, value in scenario["dme_live"].items():
        # Fuel trim is signed; the others are not.
        dme_dids[did] = struct.pack(">h" if did == 0x4002 else ">H", value)

    # The freeze frame holds the conditions when the code set, which is the
    # evidence a technician actually wants: the fault rarely repeats on demand
    # at idle in the workshop.
    snapshots = {}
    for number, _, _ in scenario["dme_dtcs"]:
        snapshots[number] = {0x01: {did: dme_dids[did] for did in sorted(scenario["dme_live"])}}
    dme = Ecu(0x0012, "DME (Engine Management)", scenario["dme_dtcs"], dme_dids, snapshots)
    dsc = Ecu(0x0029, "DSC (Dynamic Stability Control)", [], {0xF190: vin_bytes})
    bdc = Ecu(0x0040, "BDC (Body Domain Controller)", [], {0xF190: vin_bytes})
    if scenario["downstream"]:
        dsc.dtcs.append(DOWNSTREAM_DTCS[0x0029])
        bdc.dtcs.append(DOWNSTREAM_DTCS[0x0040])

    gateway = Ecu(GATEWAY_ADDRESS, "Gateway", [], {0xF190: vin_bytes})
    return {e.address: e for e in (gateway, dme, dsc, bdc)}


ECUS = build_vehicle()  # replaced at startup by --scenario


def pack_doip(payload_type, payload, protocol_version=0x02):
    return (
        struct.pack(
            "!BBHL", protocol_version, 0xFF ^ protocol_version, payload_type, len(payload)
        )
        + payload
    )


class DoIPTCPHandler(socketserver.BaseRequestHandler):
    """One tester connection. Parses DoIP headers off the stream and answers."""

    def setup(self):
        self.buffer = b""
        self.client_address_logical = None
        self.activated = False
        self.request.settimeout(300)

    def handle(self):
        logger.info("tester connected from %s", self.client_address)
        while True:
            try:
                chunk = self.request.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            self.buffer += chunk
            while True:
                message = self._take_message()
                if message is None:
                    break
                self._dispatch(*message)
        logger.info("tester disconnected from %s", self.client_address)

    def _take_message(self):
        if len(self.buffer) < 8:
            return None
        version, inverse, payload_type, length = struct.unpack_from("!BBHL", self.buffer)
        if inverse != (0xFF ^ version):
            logger.warning("bad inverse protocol version; dropping connection buffer")
            self.buffer = b""
            return None
        if len(self.buffer) < 8 + length:
            return None
        payload = self.buffer[8 : 8 + length]
        self.buffer = self.buffer[8 + length :]
        return version, payload_type, payload

    def _send(self, payload_type, payload, version=0x02):
        self.request.sendall(pack_doip(payload_type, payload, version))

    def _dispatch(self, version, payload_type, payload):
        if payload_type == 0x0005:
            self._routing_activation(version, payload)
        elif payload_type == 0x0007:
            self._send(0x0008, AliveCheckResponse(self.client_address_logical or 0).pack(), version)
        elif payload_type == 0x8001:
            self._diagnostic_message(version, payload)
        else:
            logger.info("unhandled payload type 0x%04X", payload_type)
            self._send(
                0x0000,
                GenericDoIPNegativeAcknowledge(
                    GenericDoIPNegativeAcknowledge.NackCodes.UnknownPayloadType
                ).pack(),
                version,
            )

    def _routing_activation(self, version, payload):
        source_address, activation_type = struct.unpack_from("!HB", payload)
        self.client_address_logical = source_address
        self.activated = True
        logger.info(
            "routing activation from 0x%04X (type 0x%02X) -> success", source_address, activation_type
        )
        self._send(
            0x0006,
            RoutingActivationResponse(
                source_address,
                GATEWAY_ADDRESS,
                RoutingActivationResponse.ResponseCode.Success,
                0,
            ).pack(),
            version,
        )

    def _diagnostic_message(self, version, payload):
        source_address, target_address = struct.unpack_from("!HH", payload)
        uds_request = payload[4:]
        ecu = ECUS.get(target_address)
        if ecu is None:
            logger.info("no ECU at 0x%04X -> negative ack", target_address)
            self._send(
                0x8003,
                struct.pack("!HHB", target_address, source_address, 0x03) + bytes(uds_request),
                version,
            )
            return

        # ISO 13400: acknowledge the routed request first, then deliver the response.
        self._send(
            0x8002,
            DiagnosticMessagePositiveAcknowledgement(
                target_address, source_address, 0x00, b""
            ).pack(),
            version,
        )
        logger.info(
            "0x%04X -> %-32s req %s", target_address, ecu.name, uds_request.hex(" ").upper()
        )
        uds_response = ecu.handle_uds(bytes(uds_request))
        if uds_response:
            self._send(
                0x8001,
                DiagnosticMessage(target_address, source_address, uds_response).pack(),
                version,
            )


class ThreadedDoIPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def udp_identification_responder(bind_host, stop_event):
    """Answer 0x0001 vehicle identification requests so discovery works too."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    sock.bind((bind_host, UDP_PORT))
    response = VehicleIdentificationResponse(
        VEHICLE["vin"],
        GATEWAY_ADDRESS,
        VEHICLE["eid"],
        VEHICLE["gid"],
        VehicleIdentificationResponse.FurtherActionCodes.NoFurtherActionRequired,
    ).pack()
    while not stop_event.is_set():
        try:
            data, peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) < 8:
            continue
        version, _, payload_type, _ = struct.unpack_from("!BBHL", data)
        if payload_type in (0x0001, 0x0002, 0x0003):
            logger.info("vehicle identification request from %s", peer)
            sock.sendto(pack_doip(0x0004, response, version), peer)
    sock.close()


def main():
    parser = argparse.ArgumentParser(description="DoIP / ISO 13400 vehicle simulator")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--tcp-port", type=int, default=TCP_PORT)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--scenario",
        default="intake_leak",
        choices=sorted(SCENARIOS),
        help="which vehicle fault state to serve",
    )
    args = parser.parse_args()

    global ECUS
    ECUS = build_vehicle(args.scenario)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s  %(name)s  %(message)s",
    )

    stop_event = threading.Event()
    udp_thread = threading.Thread(
        target=udp_identification_responder, args=(args.host, stop_event), daemon=True
    )
    udp_thread.start()

    server = ThreadedDoIPServer((args.host, args.tcp_port), DoIPTCPHandler)
    logger.info("DoIP simulator listening on %s:%d (TCP) and :%d (UDP)", args.host, args.tcp_port, UDP_PORT)
    logger.info("VIN %s  %s", VEHICLE["vin"], VEHICLE["model"])
    logger.info("scenario '%s': %s", args.scenario, SCENARIOS[args.scenario]["description"])
    for address, ecu in sorted(ECUS.items()):
        logger.info("  ECU 0x%04X  %-32s %d stored DTC(s)", address, ecu.name, len(ecu.dtcs))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
