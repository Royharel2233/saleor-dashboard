# Threshold calibration: DieselOBD cannot do it

Checked 2026-09-28 against the cloned dataset, not against its README.

<https://github.com/AbouAbdallah-Lounis/OBD-Dataset> — 118,477 rows, 9 vehicles,
real ANCEL ELITE 610 scanner logs. The data is real. It is the wrong data for
this engine, for three independent reasons, any one of which is disqualifying.

## 1. There is no fuel trim PID

Actual columns: `LOAD_PCT, ECT, MAP, RPM, VSS, IAT, MAF, FRP, BARO, VPWR, AAT`,
then one-hot DTC labels, then `Mode`.

Long-term fuel trim is absent. It is the signal our engine uses to decide
whether a mixture error exists at all — the difference between "air is leaking"
and "the sensor is lying". Nothing in this dataset can calibrate a threshold on
a channel it does not contain.

## 2. Every vehicle is a diesel; the case is a petrol B48

All nine: SsangYong Rodius, Peugeot 308/Expert/2008, Chevrolet Captiva, Kia
Sportage, Citroën Berlingo, Ford Fiesta — diesel, turbocharged.

This breaks the manifold-pressure reasoning at the root, not at the margins. A
diesel is unthrottled: it controls load with fuelling, so manifold pressure at
idle sits near ambient rather than in deep vacuum. `IDLE_MAP_SEALED_MAX` exists
to detect a petrol engine losing throttle-plate control of the manifold. That
state has no diesel equivalent, so no diesel measurement can set its value.

## 3. The units are imperial and contradict the README

The README states kPa, °C and g/s. The data says otherwise:

| Column | Observed range | README | Actually |
| --- | --- | --- | --- |
| `BARO` | 14.1 – 14.6 | kPa | psi (atmosphere = 14.7) |
| `ECT` | 72 – 241, median 189 | °C | °F |
| `MAP` | 2.8 – 37.0 | kPa | psi |
| `MAF` | 0.01 – 0.26 | g/s | lb/s (0.02 lb/s ≈ 9 g/s, a plausible idle) |

Taking any figure at face value would be wrong by a factor. This is worth
recording because it is exactly the kind of error a RAG pipeline ingests
silently: the README is the document a retriever indexes, and it is wrong about
its own data.

## What the dataset is good for

A real multi-label DTC classification corpus: 14 fault labels including `P0102`
(mass air flow circuit low) and `P0107` (manifold pressure circuit low) across
118k labelled rows. That is a supervised learning problem, and a legitimate one.
It is not a threshold calibration for a petrol BMW.

Register entry corrected in `sources.yaml`.

## What would actually calibrate these thresholds

A log needs all four channels our engine reads, from petrol vehicles:

- Mode 01 PID `0x04` calculated load, `0x0B` manifold absolute pressure,
  `0x0C` engine speed, `0x10` mass air flow, and critically **`0x07` long-term
  fuel trim bank 1**
- healthy vehicles, to establish what normal looks like before deciding what
  abnormal is
- the same engine family as the target, or at minimum petrol and throttled

Until such a log exists, `IDLE_MAF_EXPECTED`, `IDLE_MAP_SEALED_MAX` and
`IDLE_RPM_NORMAL_MAX` remain estimates and no verdict resting on them is
reported above medium confidence. The cheapest honest source is a baseline log
from one real petrol vehicle, which is the same hardware purchase already on the
list.
