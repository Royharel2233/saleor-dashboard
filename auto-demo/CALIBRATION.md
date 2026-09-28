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

---

# A usable source, and what it says about the thresholds

`raleighlittles/bluedriver-visualization` — 211 BlueDriver dongle logs from one
petrol vehicle, recorded 2018 by a stranger for unrelated reasons. That last part
is the point: nobody chose this data to make this engine look right.

Found by GitHub code search on the column header a logging tool emits
(`"Long Term Fuel Trim Bank 1" extension:csv`) rather than by searching for
datasets. Searching for the artefact beats searching for the category.

**Coverage: 112 of 211 logs carry MAF, long-term fuel trim and RPM. Zero carry
manifold absolute pressure.** So three of five thresholds can be checked against
reality and `IDLE_MAP_SEALED_MAX` still cannot.

## The real idle distributions (1,542 idle samples)

| Signal | min | p5 | median | p95 | max |
| --- | --- | --- | --- | --- | --- |
| MAF g/s | 0.72 | 1.95 | **2.69** | 4.44 | 8.96 |
| LTFT % | −30.1 | −10.2 | **−6.8** | −2.3 | 5.5 |
| RPM | 553 | 584 | 705 | 861 | 994 |

## What that does to the thresholds

Every one of them was wrong, and not marginally:

| Rule | Fires on this share of **normal running** |
| --- | --- |
| `MAF low < 3.5 g/s` | **70.2%** |
| `rich <= -10%` | **24.1%** |
| `neutral trim <= ±5%` covers | only 32.5% |
| `idle elevated > 900 rpm` | 2.4% |

The MAF number is the worst: the median healthy idle on this vehicle is 2.69 g/s,
well under the 3.5 g/s I called abnormally low. The scenario value of 2.10 g/s
that the whole demo verdict rests on sits inside this vehicle's **normal** idle
range, around its 10th percentile.

The trim number is nearly as bad. This vehicle idles at about −7%, so the engine
reads normal running as "not neutral, heading rich".

## False-positive rate: 49.7%

Feed each of the 1,542 real idle samples to the engine alongside a stored
`101E01` — a code that can be set by a transient and then latch — and ask what it
concludes:

```
 775 (50.3%)  no substantive verdict
 411 (26.7%)  "signal implausible with no mixture error: sensor or wiring"
 356 (23.1%)  "over-fuelling: injector leak, fuel pressure regulation"
 false-positive rate: 49.7%
```

Half the time it names a fault on a vehicle that is running normally, and in
23.1% of cases it would send a technician at the fuel system.

One measurement here was wrong first time and is worth recording: run with no
stored DTCs, the rate came out at 0.0% — because the engine short-circuits to
"no faults" before reaching the mixture logic, so that configuration tested
nothing. A clean-looking result from a test that never exercised the code is
worse than a bad result.

## The architectural conclusion

Do not recalibrate these constants to this vehicle. That would move the
circularity from my intuition to one unknown 2018 petrol car.

The finding is that **global absolute thresholds cannot work**. The spread of
normal idle MAF across vehicles is wider than the deviation a real leak produces,
so any single number is either blind or a false-positive generator. What the
engine needs is a **per-vehicle or per-engine-family baseline** and a verdict
based on deviation from it — the vehicle's own normal, captured when it was
healthy, or a model-specific reference built from logs.

Until that exists, the honest behaviour is to report low confidence whenever no
baseline is available for the vehicle being scanned, rather than medium.
