"""Host-only peak measurements on native FPGA bins, independent of display grouping."""
import numpy as np

from histogram import DEFAULT_GAIN
from comparison import SpectrumNoise


def _spectrum(counts, calibration, allow_signed=False):
    counts = np.asarray(counts)
    offset, gain = calibration
    if counts.ndim != 1 or not np.isfinite(counts).all() or (not allow_signed and np.any(counts < 0)):
        raise ValueError('Use a one-dimensional spectrum of nonnegative finite counts')
    if not np.isfinite([offset, gain]).all() or gain <= 0:
        raise ValueError('Use a positive finite calibration gain')
    return offset + gain * (np.arange(len(counts)) + .5), counts


def _smooth(values, width):
    if width == 1:
        return values.astype(float)
    # A triangular kernel preserves isolated-peak positions and avoids boxcar plateaus.
    ramp = np.arange(1, width // 2 + 2, dtype=float)
    kernel = np.r_[ramp, ramp[-2::-1]]
    padded = np.pad(values.astype(float), width // 2, mode='edge')
    return np.convolve(padded, kernel / kernel.sum(), mode='valid')


def _crossing(x, y, peak, level, direction):
    index = peak
    while 0 <= index + direction < len(y):
        other = index + direction
        if y[other] <= level < y[index]:
            fraction = (level - y[other]) / (y[index] - y[other])
            return float(x[other] + fraction * (x[index] - x[other]))
        index = other
    return None


def fit_gaussian_roi(x, raw, calibration, bounds, noise=None):
    """Poisson-likelihood Gaussian fit, or weighted fit for subtracted data.

    Parameter errors are statistical one-sigma errors. Reference rebinning
    correlations are propagated using the noise projection, without a dense
    channel-by-channel covariance matrix.
    """
    offset, gain = calibration
    low, high = map(float, bounds)
    b = (x - offset) / gain
    n = len(raw)
    if n < 6:
        return dict(status='ROI needs at least 6 native bins for a five-parameter Gaussian fit', converged=False)
    if not raw.any():
        return dict(status='No counts in ROI')

    side = min(8, max(1, n // 8))
    left_y, right_y = float(raw[:side].mean()), float(raw[-side:].mean())
    left_b, right_b = float(b[:side].mean()), float(b[-side:].mean())
    slope = 0.0 if right_b == left_b else (right_y - left_y) / (right_b - left_b)
    bg = left_y + slope * (b - left_b)
    if noise is None:
        bg = np.maximum(0.0, bg)
    sig = np.maximum(0.0, raw.astype(float) - bg)

    total_sig = float(sig.sum())
    if total_sig <= 0:
        return dict(status='No peak above estimated background')

    b_mid = float(b.mean())
    mu_init = float(np.dot(b, sig) / total_sig)
    var_init = float(np.dot((b - mu_init)**2, sig) / total_sig)
    sigma_init = max(np.sqrt(max(var_init, 1e-4)), 0.5)
    amp_init = float(sig.max())
    c0_init = float((left_y + right_y) / 2.0)
    c1_init = float(slope)

    p = np.array([amp_init, mu_init, sigma_init, c0_init, c1_init], dtype=float)
    lam = 1e-2
    y = raw.astype(float)

    def model(parameters):
        amp, mu, sigma, c0, c1 = parameters
        z = (b - mu) / sigma
        gauss = np.exp(-0.5 * np.clip(z**2, 0, 100))
        f = amp * gauss + c0 + c1 * (b - b_mid)
        J = np.empty((n, 5), dtype=float)
        J[:, 0] = gauss
        J[:, 1] = (amp / sigma) * gauss * z
        J[:, 2] = (amp / sigma) * gauss * (z**2)
        J[:, 3] = 1.0
        J[:, 4] = b - b_mid
        return f, J

    def objective(f):
        if noise is not None:
            return float(np.sum((y-f)**2 / np.maximum(noise.diagonal, 1.)))
        f = np.maximum(f, 1e-9)
        positive = y > 0
        terms = f-y
        terms[positive] += y[positive]*np.log(y[positive]/f[positive])
        return float(2*terms.sum())

    converged = False
    p[2] = max(p[2], .2)
    if noise is None:
        p[3] = max(p[3], 0.)
        p[4] = np.clip(p[4], -p[3]/max(1., abs(b-b_mid).max()), p[3]/max(1., abs(b-b_mid).max()))
    for iteration in range(120):
        f, J = model(p)
        r = y-f
        variance = np.maximum(noise.diagonal if noise is not None else f, 1.)
        weights = 1/variance
        JtJ = J.T @ (J*weights[:, None])
        Jtr = J.T @ (weights*r)

        diag = np.diag(np.diag(JtJ))
        try:
            delta = np.linalg.solve(JtJ + lam * (diag + 1e-4 * np.eye(5)), Jtr)
        except np.linalg.LinAlgError:
            break

        p_new = p + delta
        p_new[0] = max(0.0, p_new[0])
        p_new[2] = np.clip(p_new[2], .2, max(1., n))
        if noise is None:
            p_new[3] = max(p_new[3], 0.)
            limit = p_new[3]/max(1., abs(b-b_mid).max())
            p_new[4] = np.clip(p_new[4], -limit, limit)
        f_new, _ = model(p_new)
        previous, proposed = objective(f), objective(f_new)
        if proposed <= previous + 1e-8:
            p = p_new
            lam = max(1e-10, lam*.5)
            if np.max(np.abs(delta / (np.abs(p) + 1))) < 1e-6:
                converged = True
                break
        else:
            lam *= 2.0
            if lam > 1e12:
                break

    if not converged:
        return dict(status='Gaussian fit did not converge; adjust ROI bounds', converged=False, fit_iterations=iteration+1)

    amp, mu_b, sigma_b, c0, c1 = p
    mu_kev = float(offset + gain * mu_b)
    fwhm_kev = float(gain * 2.3548200450309493 * sigma_b)
    fwhm_low = float(mu_kev - fwhm_kev / 2.0)
    fwhm_high = float(mu_kev + fwhm_kev / 2.0)
    net_counts = float(amp * sigma_b * np.sqrt(2 * np.pi))
    bg_eval = np.maximum(0.0, c0 + c1 * (b - b_mid))
    bg_counts = float(bg_eval.sum())

    if fwhm_low < low or fwhm_high > high or mu_b < b[0] or mu_b > b[-1]:
        return dict(status='Peak is cut off; widen ROI for Gaussian fit')

    x_dense = np.linspace(x[0], x[-1], max(100, n * 4))
    b_dense = (x_dense - offset) / gain
    z_dense = (b_dense - mu_b) / max(sigma_b, 0.2)
    y_dense = amp * np.exp(-0.5 * np.clip(z_dense**2, 0, 100)) + c0 + c1 * (b_dense - b_mid)
    if noise is None:
        y_dense = np.maximum(0.0, y_dense)

    z_res = (b - mu_b) / max(sigma_b, 0.2)
    fit_y = amp * np.exp(-0.5 * np.clip(z_res**2, 0, 100)) + c0 + c1 * (b - b_mid)
    res = y - fit_y
    dof = n - 5
    variance = np.maximum(noise.diagonal if noise is not None else fit_y, 1.)
    chi2_red = float((res**2 / variance).sum() / dof)
    _, J = model(p)
    information = J.T @ (J/variance[:, None])
    scales = np.sqrt(np.maximum(np.diag(information), 1e-30))
    standardized = information/scales[:, None]/scales[None, :]
    covariance = None
    if np.linalg.matrix_rank(standardized) == 5:
        inverse = np.linalg.inv(standardized)/scales[:, None]/scales[None, :]
        covariance = inverse if noise is None else noise.covariance_of((inverse @ J.T)/variance)

    result = dict(peak_keV=mu_kev, centroid_keV=mu_kev, fwhm_keV=fwhm_kev,
                  fwhm_low_keV=fwhm_low, fwhm_high_keV=fwhm_high,
                  net_counts=net_counts, background_counts=bg_counts,
                  chi2_red=chi2_red, amplitude=float(amp),
                  fit_curve=(x_dense, y_dense), status='Measured (Gaussian fit)',
                  residual_curve=(x.copy(), res/np.sqrt(variance)), converged=True,
                  fit_iterations=iteration+1, degrees_of_freedom=dof,
                  fit_objective='Poisson likelihood' if noise is None else 'weighted least squares with propagated reference errors',
                  resolution_percent=float(100 * fwhm_kev / mu_kev) if mu_kev > 0 else None)
    if covariance is not None and np.isfinite(covariance).all():
        area_gradient = np.array([sigma_b*np.sqrt(2*np.pi), 0., amp*np.sqrt(2*np.pi), 0., 0.])
        result.update(centroid_uncertainty_keV=float(gain*np.sqrt(max(0., covariance[1, 1]))),
                      fwhm_uncertainty_keV=float(gain*2.354820045*np.sqrt(max(0., covariance[2, 2]))),
                      net_counts_uncertainty=float(np.sqrt(max(0., area_gradient @ covariance @ area_gradient))),
                      parameter_covariance=covariance.tolist())
    else:
        result['warning'] = 'Fit covariance is singular; statistical errors unavailable'
    if fwhm_kev < 2 * gain:
        result['warning'] = 'Peak spans fewer than 2 native bins; width is sampling-limited'
    elif side > 1 and max(left_y, right_y) > .25 * float(raw.max()):
        result['warning'] = 'High endpoint background; check ROI bounds and peak isolation'
    return result


def analyze_roi(counts, bounds, calibration=(0., DEFAULT_GAIN), smoothing=1, method='endpoint', noise=None):
    """Measure a peak using direct endpoint background or nonlinear Gaussian fit.

    ROI membership uses native-bin centers in [low, high). Gross counts are exact.
    When method is 'endpoint', net area is the signed integral above a linear endpoint
    background, centroid uses positive background-subtracted weights, and FWHM uses
    interpolated crossings on smoothed signal.
    When method is 'gaussian', a Gaussian peak with linear background is fitted
    via Levenberg-Marquardt to native bins, providing net Gaussian area, fit curve
    overlay, and reduced chi-squared.
    """
    if smoothing not in (1, 3, 5, 9):
        raise ValueError('Width smoothing must be 1, 3, 5 or 9 native bins')
    if method not in ('endpoint', 'gaussian'):
        raise ValueError("Method must be 'endpoint' or 'gaussian'")
    x, original = _spectrum(counts, calibration, allow_signed=noise is not None)
    low, high = map(float, bounds)
    if not np.isfinite([low, high]).all() or low >= high:
        raise ValueError('ROI lower bound must be smaller than its upper bound')
    selected = (x >= max(0., low)) & (x < min(4000., high))
    if noise is not None:
        if len(noise.foreground) != len(original):
            raise ValueError('Noise model does not match spectrum length')
        noise = noise.subset(selected)
    x, raw = x[selected], original[selected]
    method_name = 'linear endpoint background; interpolated half maximum' if method == 'endpoint' else 'Gaussian fit (linear background)'
    result = dict(bounds_keV=[low, high], native_bins=len(raw), smoothing_native_bins=smoothing,
                  method=method_name,
                  gross_counts=int(raw.sum(dtype=np.uint64)) if raw.dtype.kind in 'ui' else float(raw.sum()), background_counts=None,
                  net_counts=None, centroid_keV=None, peak_keV=None,
                  fwhm_keV=None, fwhm_low_keV=None, fwhm_high_keV=None,
                  centroid_uncertainty_keV=None, fwhm_uncertainty_keV=None, net_counts_uncertainty=None,
                  resolution_percent=None, status='No counts in ROI', warning=None)
    if len(raw) < 3:
        result['status'] = 'ROI needs at least 3 native bins'
        return result
    if not raw.any():
        return result

    if method == 'gaussian':
        fit = fit_gaussian_roi(x, raw, calibration, (low, high), noise)
        result.update(fit)
        return result

    # Average a small set at each end, leaving the central peak out of the estimate.
    side = min(8, max(1, len(raw) // 8))
    left, right = float(raw[:side].mean()), float(raw[-side:].mean())
    left_x, right_x = x[:side].mean(), x[-side:].mean()
    background = left + (right-left) * (x-left_x) / (right_x-left_x)
    if noise is None:
        background = np.maximum(0., background)
    signal = raw.astype(float) - background
    result['background_counts'] = float(background.sum())
    result['net_counts'] = float(signal.sum())
    fraction = (x-left_x)/(right_x-left_x)
    area_weights = np.ones(len(x))
    area_weights[:side] -= (1-fraction).sum()/side
    area_weights[-side:] -= fraction.sum()/side
    roi_noise = noise or SpectrumNoise(raw.astype(float))
    result['net_counts_uncertainty'] = float(np.sqrt(max(0., roi_noise.covariance_of(area_weights)[0, 0])))
    positive = np.maximum(signal, 0.)
    if positive.sum() <= 0:
        result['status'] = 'No peak above estimated background'
        return result
    measured = _smooth(signal, smoothing)
    peak = int(np.argmax(measured))
    result['peak_keV'] = float(x[peak])
    result['centroid_keV'] = float(np.dot(x, positive) / positive.sum())
    centroid_gradient = (x-result['centroid_keV'])*(signal > 0)/positive.sum()
    corrected = centroid_gradient.copy()
    corrected[:side] -= np.dot(centroid_gradient, 1-fraction)/side
    corrected[-side:] -= np.dot(centroid_gradient, fraction)/side
    result['centroid_uncertainty_keV'] = float(np.sqrt(max(0., roi_noise.covariance_of(corrected)[0, 0])))
    if measured[peak] <= 0:
        result['status'] = 'No peak above estimated background'
        return result
    half = measured[peak] / 2
    left_cross = _crossing(x, measured, peak, half, -1)
    right_cross = _crossing(x, measured, peak, half, 1)
    if peak in (0, len(raw) - 1) or left_cross is None or right_cross is None:
        result['status'] = 'Peak is cut off; widen ROI for FWHM'
        return result
    fwhm = right_cross - left_cross
    result.update(fwhm_keV=fwhm, fwhm_low_keV=left_cross, fwhm_high_keV=right_cross,
                  resolution_percent=100 * fwhm / result['centroid_keV']
                  if result['centroid_keV'] > 0 else None, status='Measured')
    if fwhm < 2 * calibration[1]:
        result['warning'] = 'Peak spans fewer than 2 native bins; width is sampling-limited'
    elif side > 1 and max(left, right) > .25 * float(raw[peak]):
        result['warning'] = 'High endpoint background; check ROI bounds and peak isolation'
    return result


def automatic_roi(counts, calibration=(0., DEFAULT_GAIN), target=None,
                  search_bounds=(0., 4000.), search_radius=80.):
    """Return bounds around the nearest prominent peak, or the strongest visible peak.

    Detection uses five native bins of triangular smoothing. ROI extends past
    both half-maximum crossings to include background, and is clipped at valleys
    toward neighboring prominent peaks. Targeted selection never searches past
    the requested radius (normally determined by the current plot zoom).
    """
    x, raw = _spectrum(counts, calibration)
    low, high = search_bounds
    selected = (x >= max(0., low)) & (x < min(4000., high))
    x, raw = x[selected], raw[selected]
    if len(raw) < 5 or not raw.any():
        raise ValueError('No peak in the visible spectrum')
    y = _smooth(raw, 5)
    search = np.ones(len(x), dtype=bool) if target is None else abs(x - target) <= search_radius
    if not search.any():
        raise ValueError('No peak near the click; zoom in or set ROI bounds manually')
    floor = float(np.percentile(y[search], 20))
    amplitude = float(y[search].max()) - floor
    if amplitude <= 0:
        raise ValueError('No peak above background in the selected area')
    candidates = np.flatnonzero((y >= np.r_[-np.inf, y[:-1]]) &
                               (y > np.r_[y[1:], -np.inf]))
    span = max(16, min(len(x), int(search_radius / calibration[1])))
    threshold = max(.05 * amplitude, 2 * np.sqrt(max(0., floor)))
    prominent = []
    for index in candidates:
        left = float(y[max(0, index-span):index+1].min())
        right = float(y[index:min(len(y), index+span+1)].min())
        if y[index] - max(left, right) > threshold:
            prominent.append(int(index))
    nearby = [index for index in prominent if search[index]]
    if not nearby:
        raise ValueError('No distinct peak nearby; adjust zoom or set ROI bounds manually')
    peak = max(nearby, key=lambda i: y[i]) if target is None else min(
        nearby, key=lambda i: (abs(x[i] - target), -y[i]))
    local = y[max(0, peak-span):min(len(y), peak+span+1)]
    background = float(np.percentile(local, 20))
    half = background + (y[peak] - background) / 2
    left_cross, right_cross = _crossing(x, y, peak, half, -1), _crossing(x, y, peak, half, 1)
    if left_cross is None or right_cross is None:
        raise ValueError('Peak reaches the visible boundary; zoom out or set ROI manually')
    width = right_cross - left_cross
    margin = max(width, 6 * calibration[1])
    roi_low, roi_high = left_cross - margin, right_cross + margin
    left_peaks = [i for i in prominent if x[i] < left_cross]
    right_peaks = [i for i in prominent if x[i] > right_cross]
    if left_peaks:
        neighbor = max(left_peaks)
        valley = neighbor + int(np.argmin(y[neighbor:peak+1]))
        roi_low = max(roi_low, x[valley])
    if right_peaks:
        neighbor = min(right_peaks)
        valley = peak + int(np.argmin(y[peak:neighbor+1]))
        roi_high = min(roi_high, x[valley])
    return max(0., low, x[0] - calibration[1]/2, roi_low), min(
        4000., high, x[-1] + calibration[1]/2, roi_high)
