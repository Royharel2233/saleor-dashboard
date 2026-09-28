"""Detection test on real vehicles carrying real fault labels.

The BlueDriver logs gave a false-positive rate on real healthy running. They
cannot give a true-positive rate, because nothing in them is broken. This script
uses the DieselOBD dataset instead: real vehicles, real stored DTCs, and rows
labelled with which codes were present.

What it can test: whether the baseline machinery in baseline.py separates a
labelled fault from healthy running on data nobody here produced.

What it cannot test, and the limitation is not small: a `P0107` label means the
ECU judged its manifold pressure signal implausible. Detecting that fault FROM
the manifold pressure reading is close to tautological -- the code exists because
that reading was wrong. This measures the plumbing, not the diagnosis. A real
diagnostic test needs a fault whose evidence is a DIFFERENT channel from the one
that set the code, with a repair that confirmed the cause.

    python realfault_check.py --data ../OBD-Dataset/MASTER_TRAIN_X.xlsx
"""

import argparse
import statistics as st

from baseline import Baseline

IDLE_RPM = (600, 1100)
# Units are imperial and the README misstates them (see CALIBRATION.md), but the
# analysis is a comparison within one dataset, so the scale cancels.
MAP_CHANNEL = "intake_manifold_pressure_hpa"


def load(path):
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = sheet.iter_rows(values_only=True)
    header = list(next(rows))
    index = {name: header.index(name) for name in ("MAF", "MAP", "RPM", "VSS", "P0000", "P0107")}

    healthy, faulted = [], []
    for row in rows:
        try:
            rpm = float(row[index["RPM"]])
            speed = float(row[index["VSS"]])
            map_value = float(row[index["MAP"]])
            maf = float(row[index["MAF"]])
        except (TypeError, ValueError):
            continue
        if not (IDLE_RPM[0] <= rpm <= IDLE_RPM[1] and speed == 0):
            continue
        sample = {"map": map_value, "maf": maf, "rpm": rpm}
        if row[index["P0000"]]:
            healthy.append(sample)
        elif row[index["P0107"]]:
            faulted.append(sample)
    return healthy, faulted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="../OBD-Dataset/MASTER_TRAIN_X.xlsx")
    args = parser.parse_args()

    healthy, faulted = load(args.data)
    if not healthy or not faulted:
        print(f"No usable rows in {args.data}")
        return

    half = len(healthy) // 2
    build, held_out = healthy[:half], healthy[half:]
    reference = Baseline.from_samples(
        name="DieselOBD fault-free vehicles",
        source=f"{len(build)} idle rows labelled P0000",
        samples={MAP_CHANNEL: [s["map"] for s in build]},
    )
    channel = reference.channel(MAP_CHANNEL)
    print(f"baseline: MAP median {channel.median}, band {channel.p5}..{channel.p95}, "
          f"sigma {channel.sigma:.3f}, n={channel.n}")

    # A MAP circuit reading low is a downward excursion, so test that direction.
    min_effect = abs(channel.median) * 0.15
    detected = sum(channel.deviates(s["map"], "low", min_effect=min_effect)[0] for s in faulted)
    false_alarms = sum(channel.deviates(s["map"], "low", min_effect=min_effect)[0] for s in held_out)

    print(f"\nheld-out healthy rows : {len(held_out):6}   flagged: {false_alarms:6} "
          f"({false_alarms/len(held_out):.1%})")
    print(f"P0107-labelled rows   : {len(faulted):6}   detected: {detected:6} "
          f"({detected/len(faulted):.1%})")
    print(f"\nMAP median healthy {st.median([s['map'] for s in build]):.2f} "
          f"vs faulted {st.median([s['map'] for s in faulted]):.2f}")

    maf_values = sorted({round(s["maf"], 3) for s in healthy})
    print(f"\nMAF distinct values at idle across all healthy rows: {maf_values}")
    print("The MAF channel is quantised to 0.01, giving three levels at idle, so no")
    print("baseline built on it can resolve a deviation. Only the MAP path is testable here.")


if __name__ == "__main__":
    main()
