# What was checked, how, and what it changed

Every claim in this project that could be checked from this container, with the
method. Sessions of 2026-09-27 and 2026-09-28.

## Checked by running code

| Claim | Method | Result |
| --- | --- | --- |
| No DoIP server exists on PyPI | Read `doipclient`'s installed source: client, connectors, messages | Confirmed client-only. Wrote the server from scratch, reusing its `pack()` message classes |
| The DoIP transport works | Started the simulator, ran `scan.py`, read the log | Routing activation from `0x0E00` → Success; VIN `WBA8E9G51GNT12345`; raw `0x19 02` response `59 02 FF 10 1E 01 2F 10 A2 04 2F`; 0.97 s |
| A `DoIPClient` can scan several ECUs on one socket | Tried it | **False.** It filters received messages by the one ECU address it was constructed with. Fixed: one activated socket per ECU |
| Streamlit app serves | `curl` against the headless server | HTTP 200 |
| An LLM call works from here | `anthropic.Anthropic()` with a live call | **Failed.** No `ANTHROPIC_API_KEY`; only `ANTHROPIC_BASE_URL` is set. Triage has only ever run its deterministic engine |
| MCP tool annotations reach the client | Drove the server over stdio as an MCP client | Set correctly. My first test read `readOnlyHint`; this SDK uses `read_only_hint`, so the bug I reported was in the test |
| The write gate holds | Called `vehicle_clear_dtcs` with and without `VEHICLE_MCP_ALLOW_WRITES` | `refused` by default, `cleared` with it set, `confirm: false` refused in both |
| The write path really writes | Noticed triage's primary fault change from `101E01` to `480AB2` mid-test | An earlier `clear_dtcs` had erased the DME's fault memory. Unplanned proof the pipeline is live |
| The engine reads its signals | `discriminate.py` across five scenarios | 5 distinct verdicts, no collisions. `intake_leak` and `manifold_leak` share an identical DTC set and still separate |
| The engine is stable | `robustness.py`: boundary sweep, 1% sensitivity, degradation, wasted-step | **Two real bugs.** Missing MAP read as evidence of lost vacuum; verdicts flipping on 1% moves. Both fixed; now no findings, wasted-step 0/4 |
| The MCP SDK matches the guide | Installed it | **No.** `mcp` 2.2.0 renamed `FastMCP` to `MCPServer`; the skill's guide is 1.x |

## Checked by reading the source

| Claim | Method | Result |
| --- | --- | --- |
| EdiabasLib `.sim` can be the vehicle-side fixture | Read the format documentation | **False.** It simulates telegrams inside the EDIABAS interpreter — BMW-FAST, DS2, ISO 9141, KWP 2000, TP2.0, UDS byte pairs. No DoIP payload types, no TCP 13400 listener. Reclassified as a source of recorded real-ECU byte pairs |
| DieselOBD can calibrate the thresholds | Cloned it, read 118,477 rows of the xlsx | **False, three ways.** No fuel trim channel at all; all nine vehicles are unthrottled diesels against a petrol B48 target; units are imperial (`BARO` 14.1–14.6 psi, `ECT` median 189 °F, `MAF` in lb/s) while the README claims kPa, °C, g/s |
| MCP-CAN overlaps this work | Read its README | Partly. Covers CAN, OBD-II J1979, J1939 with a virtual bus and runtime fault injection, but no DoIP and only a DBC-based UDS layer. Complementary. Its `activate_fault_scenario` tool is the pattern adopted here |
| tars-car-mcp is usable | Read its README | No. Proxies to a closed desktop app, cannot run standalone, 0 stars, 1 commit. Its UDS tool decomposition is the best reference of the four |
| obd2-mcp-server has patterns worth copying | Read its README | Yes: a first-class `--mock` mode, and `clear_dtc` behind a safety confirmation |

## Checked against the web

| Claim | Method | Result |
| --- | --- | --- |
| ISO 14229-1:2026 is current | Search (iso.org is egress-blocked) | Confirmed. Edition 4, June 2026, superseding the withdrawn 2020. The register was right and I expected to correct it |
| NHTSA manufacturer communications are search-form only | Search | **Better and worse.** A bulk TAB-delimited flat file exists (`MfrComms.txt`, since 1995-01-01, renamed from `TSBS.txt` in May 2024). But its fields carry a short `Summary`, not bulletin text — an index, not a repair corpus |
| An automotive MCP connector exists to install | Searched the MCP connector registry directly | **None.** Keywords across automotive diagnostics, vehicle OBD, CAN bus, car repair, telematics and serial hardware returned groceries and air cargo |
| Klartext and Canopy are prior art | Search | No matching automotive-diagnostic project found. Not carried forward |
| A hardware OBD emulator could replace a car | Search | Freematics OBD-II Emulator MK2 exists: CAN/KWP2000/ISO 9141, Mode 01 PIDs 0100–0163, up to 6 DTCs, open-source GUI. But no DoIP, and you set the values, so the circularity is unchanged |
| An open-source petrol air-path model exists | Search | The validated open models found are heavy-duty diesel and Simulink-based |
| A public petrol log with PID 0x07 exists | Search | Not surfaced. Results were explanatory articles about fuel trim. A direct listing sweep is still owed |

## Could not be checked from here

These hosts are denied by this environment's network policy: `iso.org`,
`nhtsa.gov`, `data.transportation.gov`, `link.springer.com`, `www.mdpi.com`,
`modelcontextprotocol.io`, `freematics.com`, `wiki.dfrobot.com`. GitHub, PyPI
and general web search are reachable.

Consequently: the ISO texts, the NHTSA bulk files, the two journal papers on
LLM-based diagnosis, the MCP specification itself, and the Freematics price and
serial command set are all recorded as leads rather than findings.

## Score

Twelve claims checked by running code, five by reading source, seven against the
web. **Nine turned out to be wrong**, six of them mine. The two most expensive
errors were both mine: recommending DieselOBD for calibration across three
consecutive turns without opening it, and reporting a test failure as a server
bug when the test was at fault.
