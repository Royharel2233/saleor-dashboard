"""A vehicle's own normal, and deviation from it.

Global absolute thresholds cannot separate a fault from vehicle-to-vehicle
variation: measured on real logs, a fixed "MAF low" line fired on 70% of normal
running. The spread of healthy idle values across vehicles is wider than the
deviation a real leak produces.

So a verdict is expressed against a baseline: what this vehicle, or this engine
family, reads when it is healthy. Deviation is measured in robust units (median
and median absolute deviation) because idle data contains transients that would
drag a mean and inflate a standard deviation.
"""

import json
import statistics as st
from dataclasses import asdict, dataclass, field

# 1.4826 * MAD estimates the standard deviation of a normal distribution while
# staying robust to outliers.
MAD_TO_SIGMA = 1.4826

# How far outside its own normal a signal must sit before it counts as evidence.
# 3 robust sigma, and also outside the observed p5..p95 band, so a single noisy
# sample cannot carry a verdict on its own.
DEVIATION_SIGMA = 3.0


@dataclass
class Channel:
    """One signal's healthy distribution."""

    median: float
    mad: float
    p5: float
    p95: float
    n: int

    @property
    def sigma(self) -> float:
        # A degenerate MAD (every sample identical) would divide by zero and make
        # every deviation infinite, so fall back to a small fraction of the median.
        scaled = self.mad * MAD_TO_SIGMA
        if scaled > 0:
            return scaled
        return max(abs(self.median) * 0.02, 1e-6)

    def z(self, value: float) -> float:
        """Signed deviation in robust sigma."""
        return (value - self.median) / self.sigma

    def outside_band(self, value: float) -> bool:
        return value < self.p5 or value > self.p95

    def deviates(self, value: float, direction: str, sigma: float = DEVIATION_SIGMA,
                 min_effect: float = 0.0):
        """Is this value beyond the vehicle's normal, in the given direction?

        Three conditions, all required:

        1. a robust-sigma excursion, so the move is larger than this vehicle's noise;
        2. a value outside the observed healthy band, so one stray sample cannot
           carry a verdict;
        3. a minimum absolute effect, because statistical significance is not
           diagnostic significance. A very quiet baseline makes tiny differences
           enormous in sigma: fuel trim 0.3 percentage points from normal can be
           14σ and still mean nothing to a technician. Every channel has a scale
           below which a difference does not matter, whatever the statistics say.
        """
        score = self.z(value)
        if direction == "low" and score > -sigma:
            return False, score
        if direction == "high" and score < sigma:
            return False, score
        if direction == "either" and abs(score) < sigma:
            return False, score
        if abs(value - self.median) < min_effect:
            return False, score
        return self.outside_band(value), score


@dataclass
class Baseline:
    """What one vehicle or engine family reads at idle when healthy."""

    name: str
    source: str
    channels: dict = field(default_factory=dict)

    @classmethod
    def from_samples(cls, name: str, source: str, samples: dict):
        """samples: {channel_name: [values]} recorded while the vehicle was healthy."""
        channels = {}
        for channel, values in samples.items():
            values = [v for v in values if v is not None]
            if len(values) < 5:
                continue
            ordered = sorted(values)
            median = st.median(ordered)
            channels[channel] = Channel(
                median=round(median, 4),
                mad=round(st.median([abs(v - median) for v in ordered]), 4),
                p5=round(ordered[int(0.05 * len(ordered))], 4),
                p95=round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 4),
                n=len(ordered),
            )
        return cls(name=name, source=source, channels=channels)

    def channel(self, name: str):
        return self.channels.get(name)

    def covers(self, *names) -> bool:
        return all(name in self.channels for name in names)

    def save(self, path: str):
        with open(path, "w") as handle:
            json.dump(
                {"name": self.name, "source": self.source,
                 "channels": {k: asdict(v) for k, v in self.channels.items()}},
                handle, indent=2,
            )

    @classmethod
    def load(cls, path: str):
        with open(path) as handle:
            data = json.load(handle)
        return cls(
            name=data["name"],
            source=data["source"],
            channels={k: Channel(**v) for k, v in data["channels"].items()},
        )

    def describe(self) -> str:
        parts = [f"{name}: median {c.median} (±{round(c.sigma, 3)}σ, n={c.n})"
                 for name, c in self.channels.items()]
        return f"baseline '{self.name}' [{self.source}] — " + "; ".join(parts)
