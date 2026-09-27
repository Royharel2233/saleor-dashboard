# Prior art: diagnostic agents and MCP servers

Research pass 2026-09-27. Verification status is stated per item; several
sources could not be fetched because this environment's egress policy denies
the host, and those are marked as leads, not findings.

## The headline finding is a negative one

The public MCP connector registry has **no automotive, OBD, CAN or vehicle
diagnostic connector**. A keyword search across automotive diagnostics, vehicle
OBD, CAN bus, car repair, telematics and serial hardware returns groceries and
air cargo. [verified]

Consequence: there is nothing to install. This is a build. The value of the
projects below is their *design*, not their code — every one is pre-1.0 and
none is a dependency you would ship on.

## Existing automotive MCP servers, ranked by what they are worth to us

### 1. MCP-CAN — closest prior art, and it does not overlap where it matters
<https://github.com/farzadnadiri/mcp-can> · MIT · 18 stars, 3 forks, 15 commits [verified]

Exposes 14 tools: `read_can_frames`, `filter_frames`, `decode_can_frame`,
`monitor_signal`, `get_vehicle_snapshot`, `send_obd_request`,
`send_diagnostic_request`, four J1939 tools, `activate_fault_scenario`, and a
`dbc_info` resource. Ships a virtual CAN bus (python-can) with four correlated
ECU responders, and three fault presets (`overheat` → P0217, `abs_fault` →
C0035, `low_fuel`), with DTCs in both OBD-II Mode 03 and J1939 DM1.

**It covers CAN, OBD-II (J1979) and J1939. It does not do DoIP (ISO 13400),
and its UDS is a custom layer over `vehicle.dbc` rather than ISO 14229.**
So it is complementary to `doip_simulator.py`, not a replacement: it owns the
CAN side, we own the Ethernet side.

Steal from it: `activate_fault_scenario` as a *tool*. Our simulator's fault set
is edited in source; theirs is switchable at runtime, which is what makes a
demo survive questioning.

### 2. tars-car-mcp — unusable as code, best UDS tool surface to copy
<https://github.com/RobertSloan22/tars-car-mcp> · license unstated · 0 stars, 1 commit [verified]

Proxies over HTTP to a TARS desktop app and "never opens the adapter itself,"
so it cannot run standalone and needs closed software plus an ELM/serial
adapter. No simulator. As a dependency: worthless.

As a **naming and decomposition reference it is the best of the four**:
`car_uds_session`, `car_uds_read_did`, `car_uds_routine`, `car_uds_send`,
`can_uds_did_sweep`, `can_enumerate_modules`, plus Mode $06 and freeze-frame
tools and GM bidirectional actuators. That is roughly the tool surface our
scan layer should expose. Note the deliberate split between a session tool and
a send tool — it keeps the LLM from re-opening a session on every call.

### 3. obd2-mcp-server — two patterns worth copying outright
<https://github.com/petrpatek/obd2-mcp-server> · MIT · 4 stars, 5 commits, alpha [verified]

Seven tools: `read_dtc`, `clear_dtc`, `get_live_data`, `get_vehicle_info`,
`list_modules`, `get_freeze_frame`, `explain_dtc`. ELM327 adapter, `python-obd`.
Carries 1,937 Ford-specific DTC definitions.

Two things it gets right that we do not:
- **`--mock` mode** as a first-class flag (simulates a 2010 Ford Focus with
  fault scenarios), so the whole thing is developable with no car.
- **`clear_dtc` requires a safety confirmation** before it wipes fault memory.

### 4. Vehicle-Diagnostic-Assistant
<https://github.com/castlebbs/Vehicle-Diagnostic-Assistant> · [lead — tool surface and licence not inspected]

## A finding about our own code

Three of the four projects gate or separate state-changing operations. Our
`triage.py` currently recommends "clear adaptations, run UDS RoutineControl
0x31 on the tank ventilation valve" as plan step 3 — a write and an actuator
command, emitted as plain text with no gate and no distinction from the
read-only steps. Before any of this is exposed as MCP tools, the read set
(0x22, 0x19, 0x10 session) and the write set (0x14 clear, 0x31 routine,
actuator tests) need to be separate tools with confirmation on the second
group. The register's own note that we "deliberately expose only safe/read-only
subsets" is not yet true of the code.

## Reasoning-layer research — leads, not findings

All four hosts are egress-denied here, so these are unverified beyond title and
abstract-level search snippets. They matter because they address the part no
MCP server above attempts: how the agent should *reason*, and how to evaluate
whether it reasoned correctly.

- Multi-step RAG for automated vehicle fault diagnosis and report generation,
  *Discover Computing* (Springer), 2025 —
  <https://link.springer.com/article/10.1007/s10791-025-09823-8> [lead]
  Claims a multi-step retrieval architecture over unstructured manuals plus
  structured fault-code databases. Closest published match to our RAG design.
- Knowledge graphs + LLMs for automotive fault diagnosis, *Electronics* (MDPI)
  14(21):4180 — <https://www.mdpi.com/2079-9292/14/21/4180> [lead]
  Relevant to the fault-propagation problem our rules engine solves by hand:
  a graph encodes which ECU consumes which signal, instead of a hardcoded
  dependants set.
- "Advancing Vehicle Diagnostic: LLMs in the Automotive Industry", Chalmers
  thesis — <https://odr.chalmers.se/items/b79a05af-5f92-4fc4-9ad2-49efbad7bd31> [lead]
- SAE 2026-26-0664, EV powertrain diagnostics and prognostics with LLMs —
  <https://saemobilus.sae.org/papers/ev-powertrain-systems-diagnostics-prognostics-utilizing-ai-ml-llm-based-approach-2026-26-0664> [lead, paywalled]
- Vehicle diagnostics LLM training sample (Hugging Face) —
  <https://huggingface.co/datasets/CJJones/Vehicle_Diagnostics_LLM_Training_Sample> [lead]
  Inspect provenance before use; a training sample is not ground truth.

Treat the "50-70% reduction in troubleshooting time" figure circulating in
these summaries as vendor-adjacent until read in the paper.

## Two register entries I could not corroborate

**Klartext** and **Canopy** produce no matching automotive-diagnostic project
in search. They may be private, renamed, or wrong. Do not carry them forward as
prior art without a URL.

## Libraries the prior art converges on

`python-can` (virtual bus), `cantools` (DBC decoding), `python-obd` (ELM327),
alongside the `doipclient` + `udsoncan` pair we already use. A DBC-based
decoder is the piece our stack lacks if we ever move from DoIP to raw CAN.

## Building the MCP server itself

Official spec and SDK docs at `modelcontextprotocol.io` are egress-denied here
[verified blocked]. This session has an `mcp-builder` skill covering
MCP server construction in Python and TypeScript — that is the resource to use
for the server scaffold rather than copying any of the four projects above.
