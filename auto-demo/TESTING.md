# Building a test environment: options, and what each one actually buys

## First, a correction

"A real car is the only option" was too strong. The claim that survives is
narrower, and it is about three requirements that are usually conflated:

| Requirement | Why it matters |
| --- | --- |
| **Right modality** | The engine consumes DoIP/UDS scans. A test that feeds it something else needs a transcoding step, and that step is where the answer leaks in. |
| **Unchosen values** | If the same person sets the test values and the thresholds, the test measures agreement with itself. |
| **Known truth** | Something independent must establish what the fault really was. |

A real car with a deliberately induced fault satisfies all three cheaply. That is
the actual argument. Other tools satisfy one or two, and several are worth
having for what they do satisfy — they are just not substitutes for each other.

## Options found

### 1. Freematics OBD-II Emulator MK2 — real hardware, wrong bus

A physical 16-pin female OBD-II port that answers diagnostic requests like a
vehicle. Emulates CAN, KWP2000 and ISO 9141-2. Responds to Mode 01 PIDs
0100–0163 (which covers fuel trim 06/07), Modes 03/07/0A with up to 6 active
DTCs, readiness monitors, CALID and Mode 09 VIN. Controlled over USB with an
open-source GUI, over BLE from iOS, or programmatically through a TTL serial
connector. [Likely — vendor documentation via search; product pages are
egress-blocked from this container, so price and the serial command set are
unverified]

- **Buys you:** a real electrical and protocol layer. Cable faults, bus timing,
  malformed responses, a tool that works on the bench but not in the car.
- **Does not buy you:** DoIP. It is a 16-pin CAN/K-line device, and this stack
  speaks ISO 13400 over Ethernet, so it cannot answer our socket at all.
- **Does not buy you:** unchosen values. You type the PID values in. Identical
  circularity to `doip_simulator.py`, with added shipping cost.

### 2. A bench ECU — the only way to test protocol reality

A real BMW DME on a bench with an ENET harness.

- **Buys you:** everything about a real ECU our simulator invents. Security
  access on write services. The real negative-response codes. Session timing
  and P2/P2* behaviour. Whether routing activation actually behaves as we
  assume. Our simulator answers every request politely, which no real ECU does.
- **Does not buy you:** engine physics. A DME on a bench has no air flowing
  through it, so MAF, trim and manifold pressure are meaningless or absent.
  It validates the transport and the UDS layer, not the diagnosis.

### 3. A mean-value engine model — the one route to unchosen numbers without a car

Model the air path from first principles (conservation of mass across the
throttle and manifold), add a leak area as a parameter, and let the model
produce MAF, manifold pressure and the resulting lambda error. The numbers then
come from thermodynamics rather than from my intuition, which genuinely breaks
the circularity.

- **Buys you:** thresholds derived from physics, and an arbitrary number of
  cases, including ones the author did not think of.
- **Costs:** the open-source validated models found are **heavy-duty diesel** and
  Simulink-based. [Likely — that is what the literature search returned] Wrong
  fuel type again, and paid tooling. Writing a petrol air-path model from
  scratch is real work, and its own assumptions would then need validating.
- **Verdict:** the most intellectually honest option and the most expensive in
  effort. Worth it only if the project needs many cases rather than a few.

### 4. Public petrol OBD logs with fuel trim — worth one focused hunt

A targeted search for gasoline logs carrying Mode 01 PID `0x07` did not surface
a specific usable dataset; results were mostly explanatory articles about what
fuel trim is. [Certain — that is what came back] This does not mean none exists;
it means a keyword search was the wrong instrument. A direct sweep of Kaggle and
GitHub dataset listings is the next step, and it is cheap.

- **Buys you:** unchosen values in roughly the right modality, if found.
- **Still missing:** known truth. A log without a verified repair cannot score a
  verdict, only calibrate a threshold.

### 5. What was already ruled out

- **DieselOBD** — no fuel trim channel, all diesel, imperial units mislabelled
  as metric. See `CALIBRATION.md`.
- **NHTSA Manufacturer Communications** — bulk flat file, but a short Summary
  field rather than bulletin text. An index of which bulletins exist, not a
  repair corpus.
- **Pico case studies** — real verified repairs, but oscilloscope-centric, so
  using them requires transcoding to a DTC scan, and they are public web pages
  an LLM may have memorised. Contaminated as an eval, valuable as methodology.

## What to combine

No single tool covers the three requirements. A defensible environment layers
them:

| Layer | Tool | Requirement it satisfies |
| --- | --- | --- |
| Transport and UDS realism | Bench ECU with ENET | right modality, unchosen protocol behaviour |
| Physical bus faults | Freematics emulator | right modality (CAN side), real electrical layer |
| Threshold calibration | Petrol log with PID 0x07, or an air-path model | unchosen values |
| Diagnostic truth | Real car, fault induced deliberately | all three, at n=2 per session |
| Stability and failure modes | `robustness.py` | none of the three, and it does not need to |

The last row matters: `robustness.py` found two real bugs without any vehicle,
because stability, degradation and wasted-step rate are properties of the code,
not of the world. Those tests were free and should have been written first.

## Cheapest honest sequence

1. Sweep Kaggle and GitHub dataset listings for a petrol log carrying PID `0x07`.
   Zero cost, and it is the one thing that would retire the guessed thresholds
   without hardware.
2. Buy the ENET cable. One baseline log from a healthy petrol car gives all four
   channels, in the right units, from the right kind of engine — and the same
   cable then runs the induced-fault test.
3. Only if the project needs many cases: write the air-path model, and validate
   it against the baseline log from step 2.

A bench ECU is worth it when the write services matter — security access and
real NRCs are where a diagnostic tool actually breaks on a real vehicle, and
nothing simulated here tests them.
