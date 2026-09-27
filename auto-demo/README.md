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
