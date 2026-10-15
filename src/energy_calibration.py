"""Host-only linear energy calibration and conservative histogram rebinning."""
import numpy as np


def fit_linear_calibration(channel_centroids, energies_kev):
    channels = np.asarray(channel_centroids, dtype=float)
    energies = np.asarray(energies_kev, dtype=float)
    if channels.ndim != 1 or channels.shape != energies.shape or len(channels) < 2:
        raise ValueError('At least two matched centroid/energy pairs are required')
    if not np.all(np.isfinite(channels)) or not np.all(np.isfinite(energies)) or np.ptp(channels) == 0:
        raise ValueError('Finite, distinct centroid positions are required')
    slope, intercept = np.polyfit(channels, energies, 1)
    if slope <= 0:
        raise ValueError('Calibration must increase with channel')
    return dict(a_kev_per_channel=float(slope), b_kev=float(intercept),
                centroid_coordinate='native_bin_center',
                reference_channels=channels.tolist(), reference_energies_kev=energies.tolist(),
                residuals_kev=(energies-(slope*channels+intercept)).tolist())


def calibrated_edges(counts, calibration):
    slope = float(calibration['a_kev_per_channel'])
    intercept = float(calibration['b_kev'])
    if not np.isfinite(slope) or not np.isfinite(intercept) or slope <= 0:
        raise ValueError('Finite positive calibration slope is required')
    return slope*np.arange(len(counts)+1, dtype=float)+intercept


def rebin_kev(counts, calibration, width_kev, minimum_kev, maximum_kev):
    """Distribute counts by bin overlap; uniform density within each native bin.

    This preserves totals including returned under/overflow; fractional output
    counts are inferred overlaps, not recovered individual event energies.
    """
    counts = np.asarray(counts, dtype=float)
    if counts.ndim != 1 or not np.all(np.isfinite(counts)) or np.any(counts < 0):
        raise ValueError('Counts must be a finite nonnegative vector')
    if not np.all(np.isfinite([width_kev, minimum_kev, maximum_kev])) or width_kev <= 0 or maximum_kev <= minimum_kev:
        raise ValueError('Finite positive bin width and increasing limits are required')
    number = int(np.ceil((maximum_kev-minimum_kev)/width_kev))
    if number > 1000000:
        raise ValueError('Requested energy grid is too large')
    edges = minimum_kev+np.arange(number+1)*width_kev
    edges[-1] = maximum_kev
    native_edges = calibrated_edges(counts, calibration)
    cumulative = np.concatenate(([0.0], np.cumsum(counts)))
    mapped = np.interp(edges, native_edges, cumulative, left=0, right=cumulative[-1])
    result = np.diff(mapped)
    return dict(edges_kev=edges, counts=result, underflow=float(mapped[0]),
                overflow=float(cumulative[-1]-mapped[-1]), original_total=float(cumulative[-1]))
