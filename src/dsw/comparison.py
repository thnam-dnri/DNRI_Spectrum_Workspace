"""Conservative energy rebinning and correlated Poisson uncertainty propagation."""
from dataclasses import dataclass

import numpy as np


@dataclass
class SpectrumNoise:
    foreground: np.ndarray
    reference: np.ndarray | None = None
    source: np.ndarray | None = None
    target: np.ndarray | None = None
    fractions: np.ndarray | None = None

    @property
    def diagonal(self):
        variance = self.foreground.astype(float).copy()
        if self.reference is not None:
            variance += np.bincount(self.target, weights=self.fractions**2*self.reference[self.source], minlength=len(variance))
        return variance

    def subset(self, selected):
        indices = np.flatnonzero(selected)
        if self.reference is None:
            return SpectrumNoise(self.foreground[selected])
        mapping = np.full(len(self.foreground), -1, dtype=int)
        mapping[indices] = np.arange(len(indices))
        retained = mapping[self.target] >= 0
        return SpectrumNoise(self.foreground[selected], self.reference, self.source[retained],
                             mapping[self.target[retained]], self.fractions[retained])

    def covariance_of(self, gradients):
        gradients = np.atleast_2d(gradients).astype(float)
        covariance = (gradients*self.foreground) @ gradients.T
        if self.reference is not None:
            projected = np.array([np.bincount(self.source, weights=gradient[self.target]*self.fractions,
                                               minlength=len(self.reference)) for gradient in gradients])
            covariance += (projected*self.reference) @ projected.T
        return covariance


def exposure_scale(foreground, reference, override=None):
    if override is not None:
        scale = float(override)
        if not np.isfinite(scale) or scale <= 0:
            raise ValueError('Reference scale must be positive and finite')
        return scale, 'manual scale'
    f_live, r_live = foreground.get('live_time', 0), reference.get('live_time', 0)
    if f_live > 0 and r_live > 0:
        return f_live/r_live, 'detector live time'
    def real(frame):
        return float(frame.get('real_time', frame.get('stats', {}).get('acquisition_ticks', 0)/50000000))
    f_real, r_real = real(foreground), real(reference)
    if f_real <= 0 or r_real <= 0:
        raise ValueError('Both spectra need measurement time; enter a manual scale for this reference')
    return f_real/r_real, 'real / acquisition-enabled time (uncorrected)'


def project_reference(reference, source_calibration, target_length, target_calibration, scale=1.):
    """Split source counts by energy overlap, retaining the projection for errors.

    Counts are assumed uniform within each source bin. Portions outside the target
    range are excluded. Split-bin covariance is retained rather than treating the
    resulting target bins as independent Poisson measurements.
    """
    reference = np.asarray(reference, dtype=float)
    if reference.ndim != 1 or not np.isfinite(reference).all() or np.any(reference < 0):
        raise ValueError('Reference must contain finite nonnegative counts')
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError('Reference scale must be positive and finite')
    for calibration in (source_calibration, target_calibration):
        if not np.isfinite(calibration).all() or calibration[1] <= 0:
            raise ValueError('Reference comparison requires positive finite energy calibration')
    source_edges = source_calibration[0] + source_calibration[1]*np.arange(len(reference)+1)
    target_edges = target_calibration[0] + target_calibration[1]*np.arange(target_length+1)
    sources, targets, fractions = [], [], []
    source = target = 0
    while source < len(reference) and target < target_length:
        low = max(source_edges[source], target_edges[target])
        high = min(source_edges[source+1], target_edges[target+1])
        if high > low:
            sources.append(source); targets.append(target)
            fractions.append(scale*(high-low)/source_calibration[1])
        if source_edges[source+1] <= target_edges[target+1]:
            source += 1
        else:
            target += 1
    sources, targets, fractions = np.array(sources, dtype=int), np.array(targets, dtype=int), np.array(fractions)
    counts = np.bincount(targets, weights=fractions*reference[sources], minlength=target_length)
    return counts, sources, targets, fractions


def subtract_background(foreground, background, foreground_calibration, background_calibration, scale):
    projected, source, target, fractions = project_reference(background, background_calibration,
                                                            len(foreground), foreground_calibration, scale)
    noise = SpectrumNoise(np.asarray(foreground, dtype=float), np.asarray(background, dtype=float), source, target, fractions)
    return np.asarray(foreground, dtype=float)-projected, noise
