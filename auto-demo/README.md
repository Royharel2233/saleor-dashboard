# auto-demo — DoIP diagnostic engine + simulator

Four working parts. No vehicle, no cable, no hardware required to run or demo.

| File | What it is |
| --- | --- |
| `doip_simulator.py` | An ISO 13400 **server** — routing activation, alive check, diagnostic message routing, plus a UDS 14229 stack (0x10, 0x3E, 0x22, 0x19, 0x14, 0x31) across four ECUs. Written from scratch: `doipclient` is a client only. |
| `scan.py` | The tester. Opens a real TCP socket, activates routing, reads VIN + DTCs + live DIDs. Same code runs against a car at `169.254.1.1`. |
| `triage.py` | Root-cause engine. `llm` mode (Anthropic, strict JSON contract) when `ANTHROPIC_API_KEY` is set; deterministic fault-propagation model otherwise. The UI always names which ran. |
| `app.py` | Streamlit dashboard. Every value is read at click time. |

## Run it

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install doipclient udsoncan streamlit anthropic
# terminal 1
.\.venv\Scripts\python.exe doip_simulator.py
# terminal 2
.\.venv\Scripts\streamlit.exe run app.py
```

Or `.\run_demo.ps1` to do all of it.

Against a real car: plug in the ENET cable, set the sidebar gateway to `169.254.1.1`.
Nothing else changes.

## The simulated fault picture

One real cause, three downstream symptoms — the thing the triage engine has to get right:

- **DME 0x0012** `101E01` air mass plausibility (low) + `10A204` mixture adaptation limit
- **DSC 0x0029** `480AB2` implausible torque signal from DME
- **BDC 0x0040** `D35A11` engine data message missing
- Live data: MAF 2.10 g/s at 762 rpm, long-term fuel trim +23.8%

The lean trim alongside a low measured air mass is what separates "unmetered air
leak downstream of the sensor" from "dead sensor" — a verdict on the DTC pattern
alone cannot make that call, which is why the DIDs are in the scan.

## Verify it is not a slideshow

Stop the simulator and click scan: the app reports the connection failure.
Edit `build_vehicle()` in `doip_simulator.py` to change the fault set: the
verdict changes with it.

## Source register

`sources.yaml` holds the RAG source pack: 18 sources, each tagged with a tier
(what it may be used *for*) and a verification status. Two entries were
corrected against their source on 2026-09-27 — see `verified: corrected`.

The governing rule: a dataset is not diagnostic ground truth merely because it
contains automotive data. Tier A requires a verified repair outcome.

Note for ingestion: `iso.org`, `nhtsa.gov` and `data.transportation.gov` are
denied by this environment's egress policy, so those downloads must run
elsewhere.

## MCP server

`mcp_server.py` exposes the scan layer as MCP tools over stdio. Eight tools,
split by what they do to the vehicle:

| Tool | Annotations | Notes |
| --- | --- | --- |
| `vehicle_list_ecus` | readOnly | No connection opened; call first |
| `vehicle_identify` | readOnly | VIN via 0x22 F190 |
| `vehicle_scan_all` | readOnly | Every ECU: DTCs, status bits, live data |
| `vehicle_read_dtcs` | readOnly | One ECU, 0x19 02 |
| `vehicle_read_data_identifier` | readOnly | Arbitrary DID, raw hex, no guessed scaling |
| `vehicle_triage_scan` | readOnly | Scan + root cause, reports which engine ran |
| `vehicle_clear_dtcs` | **destructive** | 0x14, gated |
| `vehicle_run_routine` | **destructive** | 0x31, gated, actuates hardware |

### The write gate

The two destructive tools refuse unless the operator sets, out of band:

```powershell
$env:VEHICLE_MCP_ALLOW_WRITES = "1"
```

*and* the call passes `confirm: true` with a `reason`. Without the environment
variable no prompt, tool argument or agent loop can reach them — the refusal
happens before any socket opens. Verified both ways: `refused` by default,
`cleared` only with the variable set, and `confirm: false` refused in both
configurations.

### Client configuration

```json
{
  "mcpServers": {
    "vehicle-diagnostics": {
      "command": "C:\\path\\to\\auto-demo\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\auto-demo\\mcp_server.py"]
    }
  }
}
```

Add `"env": {"VEHICLE_MCP_ALLOW_WRITES": "1"}` only when you intend to let an
agent clear fault memory and actuate hardware.

### SDK note

Built against `mcp` 2.2.0, where `FastMCP` was renamed to `MCPServer` and the
annotation fields are snake_case (`read_only_hint`). Guides written for the 1.x
API do not apply.

## Does it actually read the signals?

`python discriminate.py` starts the simulator once per fault scenario, scans it,
triages it, and prints whether the verdicts differ:

```
healthy            -> no faults
intake_leak        -> unmetered air between the MAF and the throttle plate
manifold_leak      -> unmetered air downstream of the throttle plate
maf_signal_fault   -> signal implausible with no mixture error: sensor, not a leak
over_fuelling      -> rich mixture: fuel pressure / injectors
5 distinct verdicts across 5 scenarios. No collisions.
```

The result that matters: **`intake_leak` and `manifold_leak` store an identical
DTC set** — `101E01, 10A204, 480AB2, D35A11` — and still produce different
verdicts and different first inspection steps. The fault codes cannot separate
them. Fuel trim, manifold pressure and idle speed can:

| Signal | What it decides |
| --- | --- |
| Fuel trim near zero | The mixture is correct, so nothing unmetered is entering — the *reading* is wrong, not the air |
| Trim positive, manifold holds vacuum, idle normal | Air enters between sensor and throttle plate |
| Trim positive, manifold pressure raised or idle elevated | Throttle has lost control of the manifold: the leak is downstream of it |
| Trim negative | Rich, not lean. An air leak cannot cause it |

### What this does not prove

The scenario values and the thresholds were written by the same author, so the
test shows the engine reads the signals it is given and that those signals carry
enough information in principle. It does **not** validate the thresholds
(`IDLE_MAF_EXPECTED`, `IDLE_MAP_SEALED_MAX`, `IDLE_RPM_NORMAL_MAX`) against real
vehicles. They are estimates, which is why no verdict is reported above
`medium` confidence. Replacing them, and the scenario values, with DieselOBD
measurements is what would turn this from a demonstration into evidence.

## Robustness harness

`python robustness.py` asks four questions that have nothing to do with whether
a verdict is right:

1. **Boundary sweep** — walk each signal across its range and report where the
   verdict changes, flagging any substantive verdict entered from two
   disconnected bands.
2. **Sensitivity** — move each signal 1% either side of its value and of each
   threshold. A verdict that flips on 1% is a coin toss, not a diagnosis.
3. **Degradation** — drop live data, drop MAP, drop trim, drop the DTCs, break a
   DTC text lookup. Does confidence fall, and does the engine say why?
4. **Wasted inspection steps** — does plan step 1 name a component the data
   already excludes? This is the metric with commercial meaning.

### It found two real bugs on its first run

**Missing data read as evidence.** With no MAP reading, `manifold_sealed`
evaluated false rather than unknown, so the engine confidently placed a leak
downstream of the throttle plate using a measurement it never took. Absence of a
reading is not evidence; fixed by making the flag tri-state and requiring
positive evidence for that branch.

**Verdicts flipping on 1% moves.** Every threshold was a hard edge, so a reading
at MAF 3.465 vs 3.535 g/s produced two different faults and two different
repairs. Fixed with a 5% band around each threshold in which the engine returns
no verdict, says which reading is borderline, and asks for the measurement to be
repeated at 2000 rpm where the candidates separate.

After both fixes: no findings, wasted-step rate 0/4, and `discriminate.py` still
returns five distinct verdicts across five scenarios.

## Calibration status

See `CALIBRATION.md`. The DieselOBD dataset cannot calibrate these thresholds —
it has no fuel trim channel, every vehicle is an unthrottled diesel, and its
units are imperial while its README claims metric. The thresholds remain
estimates, which is why nothing reports above medium confidence.

## Baselines, not thresholds

Measured against 1,542 real idle samples from a petrol vehicle, the original
fixed thresholds named a fault on normal running **49.7%** of the time. A single
"MAF below 3.5 g/s" rule fired on 70% of healthy idle, because that vehicle's
healthy idle median is 2.69 g/s. Healthy values vary more between vehicles than
a fault moves them within one, so no global number can separate the two.

Every judgement is now made against a recorded baseline — what this vehicle
reads at idle when healthy — using robust statistics (median and median absolute
deviation). A reading counts as evidence only when it clears three bars:

1. **3 robust sigma** from the vehicle's healthy median;
2. **outside the observed healthy band** (p5..p95), so one stray sample cannot
   carry a verdict;
3. **a minimum absolute effect** — 5 trim points, 120 rpm, 15% of healthy MAF or
   MAP.

The third bar exists because of a bug the redesign introduced and the tests
caught: against a very quiet baseline, fuel trim 0.3 percentage points from
normal scores 14 sigma and the engine called a healthy vehicle over-fuelling.
Statistical significance is not diagnostic significance. Every channel has a
scale below which a difference does not matter, whatever the statistics say.

### With no baseline, it names nothing

That is the correct behaviour, not a gap to patch. An unreferenced reading does
not support a cause. `python discriminate.py` records one from the healthy
scenario and writes `baseline_simulated_b48.json`; on a real vehicle you log it
while it is known good.

### Measured result

| | false positives on real normal running |
| --- | --- |
| fixed thresholds | **49.7%** |
| vehicle's own baseline, out-of-sample | **0.2%** |

The baseline is built from 56 logs and evaluated on a disjoint 56, so the rate is
out-of-sample. What is **not** measured is sensitivity: there is no real faulty
data with a verified repair here, so the true-positive rate is only known against
the simulator, where `discriminate.py` still separates all five scenarios.
A 0% false-positive rate is trivial to achieve by never answering; the pairing of
0.2% false positives with 5/5 discrimination is the claim.

## Sensitivity without a vehicle

`python injection.py` applies a physically-derived fault to real healthy logs.
The substrate is real — 638 held-out idle samples from a petrol vehicle, with
that vehicle's own noise — and only the perturbation is modelled, from
conservation of mass rather than chosen to be detectable:

At idle the ECU governs engine speed, so total charge is roughly constant. Air
admitted downstream of the sensor displaces air through it, as the throttle
closes to compensate:

```
measured MAF   ->  m * (1 - f)
long-term trim ->  t + 100 * f / (1 - f)
```

| leak, as fraction of idle air flow | Δtrim | detected |
| --- | --- | --- |
| 0% | +0.0 | **0.2%** (false positives) |
| 5% | +5.3 | 0.6% |
| 10% | +11.1 | 5.3% |
| 15% | +17.6 | 63.8% |
| 20% | +25.0 | 99.8% |
| 30% | +42.9 | 100.0% |

### The trade-off, quantified

Detection crosses 50% at a leak of roughly **15% of idle air flow**, an 18-point
trim shift. Below 10% the engine is effectively blind: a leak producing an
11-point trim shift is caught 5% of the time.

That is the price of the fix for the 49.7% false-positive rate, and it is
legible rather than hidden. This vehicle's trim baseline has a robust sigma of
4.7 points, so a 3-sigma bar is a 14-point shift before anything is called. A
quieter baseline — warm idle only, more samples, tighter conditions — buys
sensitivity back directly. That is a calibration decision with a visible cost on
both sides, not a threshold someone guessed.

### What it still does not establish

That a real leak of a given size produces this trim and air-mass response. The
model is the assumption under test, not evidence for it. One measurement on one
vehicle settles it, and the curve names exactly which measurement: induce a leak
of known area at idle, record the trim shift, and check it against
`100 * f / (1 - f)`.

## Recovering the small-leak blind spot

Two levers, measured on the same held-out real samples:

| baseline conditions | trim σ | rpm σ | detection floor (50%) | 10% leak |
| --- | --- | --- | --- | --- |
| any idle, single reading | 4.74 | 150 | 15% | 4.1% |
| settled warm idle, single reading | 3.93 | 76 | 15% | 4.1% |
| settled warm idle, median of 16 | 2.34 | 21 | **8%** | **100%** |

**Conditions**: warm coolant, closed throttle, settled idle band. Halves the idle
speed spread and lifts 15%-leak detection from 64% to 90%, but leaves the small
leaks invisible, because the trim bar is still about 12 points.

**Averaging**: the median of 16 readings has roughly a quarter of the spread, and
the decision bar is a multiple of the spread, so the smallest detectable leak
falls with it. One UDS read is a noisy sample; a tester can sit at idle and log
for half a minute. This is the lever that costs nothing but time. (Measured on
13 held-out windows — the mechanism is σ/√K, but the measurement itself is thin.)

**What did not work, and why it is worth recording**: requiring corroboration
between channels — air mass down *and* trim up together, each at a lower bar —
changed nothing measurable. On this vehicle a 10% leak moves MAF by only 0.37σ,
because its healthy idle air mass spans 1.95 to 4.44 g/s. At idle the leak
signature is carried almost entirely by fuel trim. The idea is sound and the code
is kept for vehicles where manifold pressure is available; it simply has no
purchase on this data.

## The full cycle

`python cycle.py --scenario intake_leak` runs the sequence a tester actually
performs:

```
routing activation -> extended session -> read fault memory (0x19 0x02)
  -> read each fault's freeze frame (0x19 0x04)
    -> compare against the vehicle's healthy baseline -> verdict
```

The freeze frame is the point. A fault rarely reproduces on demand while the car
sits at idle in a workshop, and the ECU already recorded the conditions at the
moment the code set. `0x19 0x04` returns the DTC, its status, then per record a
record number, an identifier count, and each identifier with its data.

**The identifier's data length is not in the response.** A client needs the
manufacturer's identifier database to know where one value ends and the next
begins; without it the remainder of the record is unparseable, and the parser
reports the unparsed tail rather than guessing. This is why real tools ship an
OEM database, and it is a hard constraint on any generic diagnostic agent.

The cycle also refuses a comparison it cannot make: a freeze frame captured at
2,500 rpm under load, judged against an idle baseline, would make normal running
look catastrophic. The conditions are checked before the verdict.
