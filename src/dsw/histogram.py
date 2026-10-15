"""Group native FPGA bins, calibrate edges, and retain out-of-range counts."""
import numpy as np

DEFAULT_GAIN = 4000 / 16384
GROUPS = (1, 8, 16, 24, 32, 40)


def energy_histogram(counts, calibration=(0., DEFAULT_GAIN), group=8):
    source = np.asarray(counts)
    counts = source.astype(float) if source.dtype.kind == 'f' else source.astype(np.uint64)
    offset, gain = calibration
    if group not in GROUPS or not np.isfinite([offset, gain]).all() or gain <= 0:
        raise ValueError('Use positive finite gain and grouping 1/8/16/24/32/40')
    if counts.ndim != 1 or not len(counts) or not np.isfinite(counts).all():
        raise ValueError('Use a nonempty finite one-dimensional histogram')
    native_edges = np.append(np.arange(0, len(counts), group), len(counts))
    edges = offset + gain * native_edges
    centers = offset + gain * (np.arange(len(counts)) + .5)
    below, above = centers < 0, centers >= 4000
    cast = float if counts.dtype.kind == 'f' else int
    underflow = cast(counts[below].sum())
    overflow = cast(counts[above].sum())
    retained = counts.copy()
    retained[below | above] = 0
    grouped = np.add.reduceat(retained, native_edges[:-1])
    visible = (edges[:-1] < 4000) & (edges[1:] > 0)
    if not visible.any():
        return np.array([0., 4000.]), np.zeros(1, dtype=counts.dtype), underflow, overflow
    first, last = np.flatnonzero(visible)[[0, -1]]
    return np.clip(edges[first:last+2], 0, 4000), grouped[first:last+1], underflow, overflow
