"""V_TR, V_T,on and V_T,off extraction from a linear-mode Id-Vg transfer curve, plus the derivative-based V_TR
variants (gm/Id trough vs gm peak, and 50 % crossings of gm/Id and gm).

Copied from the owner's new_TR_extraction_funs.py (kept outside the repo in .pptx_figs/), computation only: the
plotting helpers and the .xls / Agilent CSV loaders are left out, and print-to-log is not used. The method follows
K. Jana et al., "Advancing the Supply Voltage Scalability of Oxide Semiconductor Transistors through Transition
Region Engineering", Sec. V, Eqs. 6-9.

--------------------------------------------------------------------------
Physics summary
--------------------------------------------------------------------------
V_T,on  : extracted from the ABOVE-THRESHOLD (linear) regime by linearly
          extrapolating I_D-V_GS at the point of maximum transconductance
          (g_m) back to I_D = 0                                    (Eq. 9)

V_T,off : extracted from the SUBTHRESHOLD (exponential) regime by linearly
          extrapolating log10(I_D) vs V_GS at the point of steepest slope
          (minimum subthreshold swing SS_min) back to a reference current
          I_D0 = beta*eta_SS*phi_T^2*(1-exp(-V_DS/phi_T))          (Eqs. 7-8)

V_TR = V_T,on - V_T,off                                              (Eq. 6)

--------------------------------------------------------------------------
Series resistance (R_S, R_D)
--------------------------------------------------------------------------
Series resistance mainly distorts the ABOVE-THRESHOLD part of the curve
(subthreshold current is too small to drop appreciable voltage across
R_S/R_D), so V_T,off is essentially unaffected, while V_T,on (and hence
V_TR) can be substantially biased if R_S/R_D is ignored -- e.g. the
example in the slides shows extracted V_TR going from ~0.59 V (no R) down
to a spurious ~0.17 V once R_S/R_D is added and NOT corrected for.

Two correction strategies are implemented here:

1. Direct correction (requires known R_S, R_D):
        I_D' = I_D / (1 - I_D*(R_S+R_D)/V_DS)
   I_D' is used in place of I_D for the above-threshold (V_T,on) extraction.

2. Y-function method (does NOT require knowing R_S, R_D):
        Y = I_D / sqrt(g_m)
   Y is, to first order, independent of R_S/R_D, so it can be linearly
   extrapolated to Y = 0 to obtain V_T,on directly from RAW data. The
   extrapolation point is chosen where dY/dV_GS is maximal (where Y itself
   is most linear) -- NOT where g_m is maximal, since those are generally
   different points once R_S/R_D is present.

R_S+R_D can itself be *estimated* from the data once beta is known, via:
        R_S+R_D ~= V_DS/I_D,on - V_DS/(Y*beta)

--------------------------------------------------------------------------
Noise handling
--------------------------------------------------------------------------
Real transfer-curve data is noisy, and naive differencing (np.diff /
np.gradient) amplifies that noise badly -- especially for a second
derivative. To keep this robust:

  * All derivatives are computed with a Savitzky-Golay filter (which
    smooths and differentiates simultaneously) on a uniformly-resampled
    grid, rather than point-to-point finite differences.
  * All threshold-voltage extrapolations are done as a LOCAL LINEAR
    REGRESSION over a small window of points around the extremum of
    interest (not just "one slope value + one data point"), which further
    suppresses point-to-point noise while still tracking the local
    curvature correctly.

"""
import warnings

import numpy as np
from scipy.signal import savgol_filter
from scipy.interpolate import interp1d

# ----------------------------------------------------------------------------
# Physical constants
# ----------------------------------------------------------------------------
KB = 1.380649e-23      # J/K
Q = 1.602176634e-19    # C


def thermal_voltage(T=300.0):
    """Thermal voltage phi_T = kB*T/q, in volts."""
    return KB * T / Q


# ----------------------------------------------------------------------------
# Noise-robust resampling & differentiation helpers
# ----------------------------------------------------------------------------
def _odd(n):
    n = int(n)
    return n if n % 2 == 1 else n + 1


def _is_uniform(x, rtol=1e-3):
    """Check whether x is (to within rtol) evenly spaced."""
    d = np.diff(x)
    return np.std(d) <= rtol * np.mean(np.abs(d))


def _uniform_resample(x, y, n=None):
    """
    Put (x, y) onto a uniform grid for Savitzky-Golay filtering.

    IMPORTANT for noisy data: if x is already (near-)uniformly spaced, we
    do NOT interpolate at all -- we use the data as-is. Interpolating noisy
    data (especially with cubic splines, and especially onto a FINER grid
    than the original) creates spurious ringing/overshoot between points
    that can be larger than the genuine noise, which then contaminates the
    derivative. We only resample (with plain LINEAR interpolation, at the
    ORIGINAL point density -- never oversampled) if the input grid is
    genuinely non-uniform, since Savitzky-Golay itself requires uniform
    spacing.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    mask = np.concatenate(([True], np.diff(x) > 0))  # drop duplicate x
    x, y = x[mask], y[mask]

    if _is_uniform(x) and (n is None or n == len(x)):
        return x, y

    n = n or len(x)
    xu = np.linspace(x[0], x[-1], n)
    f = interp1d(x, y, kind="linear")
    yu = f(xu)
    return xu, yu


def smooth(y, window_length=11, polyorder=3):
    """Savitzky-Golay smoothing with automatic window-size clamping."""
    y = np.asarray(y, dtype=float)
    wl = _odd(min(window_length, len(y) - 1)) if len(y) > 3 else len(y)
    wl = max(wl, 3)
    po = min(polyorder, wl - 1)
    return savgol_filter(y, wl, po)


def smooth_derivative(x, y, order=1, window_length=11, polyorder=3,
                       n_resample=None):
    """
    Noise-robust derivative of y wrt x, of the given `order`, computed by:
      1) resampling (x, y) onto a uniform grid (cubic interpolation),
      2) applying a Savitzky-Golay filter configured to directly output the
         requested derivative order on that uniform grid,
      3) interpolating the result back onto the original x locations.

    This is much less noise-sensitive than np.gradient / repeated np.diff,
    particularly for order=2 (needed e.g. for dGm/dVGS-type extractions).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xu, yu = _uniform_resample(x, y, n=n_resample)  # n_resample=None -> native resolution
    dx = xu[1] - xu[0]

    wl = _odd(min(window_length, len(xu) - 1))
    po = min(polyorder, wl - 1)
    po = max(po, order)  # savgol requires polyorder >= deriv order

    dyu = savgol_filter(yu, wl, po, deriv=order, delta=dx)
    f = interp1d(xu, dyu, kind="linear", fill_value="extrapolate")
    return f(x)


# ----------------------------------------------------------------------------
# Derived transistor quantities
# ----------------------------------------------------------------------------
def compute_gm(VGS, ID, window_length=11, polyorder=3, n_resample=None):
    """Transconductance g_m = dI_D/dV_GS (noise-robust)."""
    return smooth_derivative(VGS, ID, order=1, window_length=window_length,
                              polyorder=polyorder, n_resample=n_resample)


def compute_d2ID_dVGS2(VGS, ID, window_length=11, polyorder=4, n_resample=None):
    """
    Second derivative d^2I_D/dV_GS^2 (noise-robust), useful for alternative
    extraction schemes such as V_T,G'm or V_T,(Gm/ID)' in Table I of the
    manuscript. Computed directly via savgol(deriv=2) rather than by
    differentiating g_m a second time, to avoid compounding smoothing error.
    """
    return smooth_derivative(VGS, ID, order=2, window_length=window_length,
                              polyorder=polyorder, n_resample=n_resample)


def compute_SS(VGS, ID, gm=None, window_length=11, polyorder=3,
               n_resample=None, eps=1e-30):
    """
    Subthreshold swing SS = dV_GS / d(log10 I_D) = ln(10) * I_D / g_m   [V/dec]
    """
    ID = np.asarray(ID, dtype=float)
    if gm is None:
        gm = compute_gm(VGS, ID, window_length, polyorder, n_resample)
    gm_safe = np.where(np.abs(gm) < eps, eps, gm)
    return np.log(10.0) * ID / gm_safe


def compute_Y_function(ID, gm, eps=1e-30):
    """
    Y-function: Y = I_D / sqrt(g_m).

    To first order, Y = sqrt(beta*VDS)*(VGS - VTON - eta_SS/2*VDS) is
    INDEPENDENT of series resistance R_S/R_D, so it lets you extract V_T,on
    even when R_S/R_D are unknown.
    """
    ID = np.asarray(ID, dtype=float)
    gm = np.asarray(gm, dtype=float)
    gm_pos = np.clip(gm, eps, None)
    return ID / np.sqrt(gm_pos)


def correct_ID_series_R(ID, VDS, Rtot):
    """
    Remove the effect of series resistance R_tot = R_S + R_D from measured
    I_D (valid in the above-threshold/linear regime):

        I_D' = I_D / (1 - I_D * R_tot / V_DS)

    Points where the correction would blow up or invert (I_D*R_tot >= V_DS)
    are masked to NaN.
    """
    ID = np.asarray(ID, dtype=float)
    denom = 1.0 - ID * Rtot / VDS
    safe_denom = np.where(denom > 1e-6, denom, np.nan)
    return ID / safe_denom


# ----------------------------------------------------------------------------
# Local linear-regression extrapolation (robust to point-to-point noise)
# ----------------------------------------------------------------------------
def _local_linear_fit(x, y, idx, npts=5):
    """Least-squares line through `npts` points centered on idx."""
    n = len(x)
    half = npts // 2
    lo = max(0, idx - half)
    hi = min(n, lo + npts)
    lo = max(0, hi - npts)
    xs, ys = np.asarray(x[lo:hi]), np.asarray(y[lo:hi])
    finite = np.isfinite(xs) & np.isfinite(ys)
    xs, ys = xs[finite], ys[finite]
    if len(xs) < 2:
        return np.nan, np.nan
    A = np.vstack([xs, np.ones_like(xs)]).T
    slope, intercept = np.linalg.lstsq(A, ys, rcond=None)[0]
    return slope, intercept


def extrapolate_to_value(x, y, idx, target_y, npts=5):
    """
    Fit a local line to (x, y) around index `idx`, and return the x at which
    that line equals `target_y` (target_y=0 for V_T,on; target_y=log10(ID0)
    for V_T,off).
    """
    slope, intercept = _local_linear_fit(np.asarray(x), np.asarray(y), idx, npts)
    if not np.isfinite(slope) or slope == 0:
        return np.nan
    return (target_y - intercept) / slope


def rolling_ols_slope(x, y, window):
    """
    OLS slope of y vs x in a sliding window of `window` points centered at
    every index. The window genuinely SHRINKS near the array edges (rather
    than re-anchoring to a full-size window shifted inward) so that each
    reported slope reflects the actual local neighborhood of that index --
    important here because the argmax of this array is used to LOCATE a
    point, not just to estimate a single value, so a location bias at the
    edges would corrupt the result.

    This is used instead of a pointwise numerical derivative (e.g.
    Savitzky-Golay deriv=1) whenever `y` is ITSELF already a derived,
    noise-amplified quantity -- most notably the Y-function, Y = I_D/sqrt(g_m),
    which already involved one differentiation (g_m = dI_D/dV_GS). Computing
    a further pointwise derivative of Y (i.e. dY/dV_GS via a filter) compounds
    that noise amplification, effectively behaving like a second derivative
    of I_D. A windowed regression instead only ever performs ONE level of
    differencing -- the same principle already used for the final V_T
    extrapolations (`_local_linear_fit`) -- just swept across every
    candidate index to find where the local slope is largest.

    Use a wider `window` than you would for a plain first derivative to
    compensate for Y's already-elevated noise floor.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    window = max(3, min(int(window), n))
    half = window // 2
    slopes = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        xs, ys = x[lo:hi], y[lo:hi]
        finite = np.isfinite(xs) & np.isfinite(ys)
        xs, ys = xs[finite], ys[finite]
        if len(xs) < 2:
            continue
        xm = xs.mean()
        dx = xs - xm
        denom = np.dot(dx, dx)
        if denom <= 0:
            continue
        slopes[i] = np.dot(dx, ys - ys.mean()) / denom
    return slopes


# ----------------------------------------------------------------------------
# beta, eta_SS, ID0
# ----------------------------------------------------------------------------
def extract_beta(VDS, deriv, correct_series_R=False):
    """
    beta = mu*Ceff*(W/L), from g_m = beta*V_DS (above-threshold, R-free)
    => beta = g_m,max / V_DS.

    NOTE: if series resistance is significant, gm_max should come from
    R_S/R_D-corrected current (or you should rely on the Y-function path),
    otherwise beta will be systematically underestimated.
    """
    if not correct_series_R:
        return deriv / VDS
    else:
        return deriv**2 / VDS


def extract_eta_SS(SS_min, T=300.0):
    """Subthreshold non-ideality factor eta_SS from SS_min [V/dec]."""
    phiT = thermal_voltage(T)
    return SS_min / (np.log(10.0) * phiT)


def compute_ID0(beta, eta_SS, VDS, T=300.0):
    """
    Off-current pre-factor (MIT Virtual Source model), Eq. (8):
        I_D0 = beta * eta_SS * phi_T^2 * (1 - exp(-V_DS/phi_T))
    """
    phiT = thermal_voltage(T)
    return beta * eta_SS * phiT ** 2 * (1.0 - np.exp(-VDS / phiT))


# ----------------------------------------------------------------------------
# Series resistance (R_S+R_D) estimation from data (optional diagnostic)
# ----------------------------------------------------------------------------
def estimate_series_R(VGS, ID, VDS, beta, gm=None, idx=None,
                       window_length=11, polyorder=3, n_resample=None):
    """
    Estimate R_S+R_D at a chosen above-threshold operating point (default:
    the point of max g_m) using the Y-function relation (R-independent by
    construction, so no prior knowledge of R_S/R_D is needed):

        R_S+R_D ~= V_DS/I_D,on - V_DS/(Y*beta)
    """
    ID = np.asarray(ID, dtype=float)
    if gm is None:
        gm = compute_gm(VGS, ID, window_length, polyorder, n_resample)
    Y = compute_Y_function(ID, gm)
    if idx is None:
        idx = int(np.nanargmax(gm))
    ID_on, Y_on = ID[idx], Y[idx]
    return VDS / ID_on - VDS / (Y_on * beta)


# ----------------------------------------------------------------------------
# Region auto-detection
# ----------------------------------------------------------------------------
def _default_masks(ID, on_frac=0.5, off_frac=0.05):
    """
    Simple auto-detection of subthreshold vs. above-threshold index ranges,
    based on the fraction of max(I_D). Works for a typical monotonic
    transfer curve; pass explicit masks for anything more specific (e.g. a
    strict multi-decade exponential-only subthreshold window).
    """
    ID = np.asarray(ID, dtype=float)
    ID_max = np.nanmax(ID)
    above_mask = ID >= on_frac * ID_max
    sub_mask = (ID > 0) & (ID <= off_frac * ID_max)
    return sub_mask, above_mask


# ----------------------------------------------------------------------------
# Main extraction routine
# ----------------------------------------------------------------------------
def _exclude_edges(mask, n, margin):
    """Zero out `margin` points at each end of a boolean mask (avoids
    Savitzky-Golay boundary artifacts being mistaken for a true extremum)."""
    mask = np.array(mask, dtype=bool, copy=True)
    margin = max(0, min(margin, n // 2 - 1))
    if margin > 0:
        mask[:margin] = False
        mask[-margin:] = False
    return mask


def _largest_true_run(mask):
    """Keep only the longest contiguous run of True in a boolean mask,
    clearing everything else.

    An `ID >= ID_limit` mask is meant to isolate the device's "on" region,
    but noise can make an isolated point or two deep in the off-state
    (measurement noise floor) transiently exceed ID_limit as well, leaving a
    small disconnected island alongside the real turn-on run. Feeding that
    straight to a Savitzky-Golay filter (which assumes an evenly-spaced,
    contiguous grid) makes it bridge across the gap between the island and
    the real run, corrupting the derivative near both. A real Id-Vg sweep
    only has one genuine "on" region, so keeping just the longest run
    removes such islands without needing a separate noise-detection step.
    """
    mask = np.asarray(mask, dtype=bool)
    if not np.any(mask):
        return mask
    padded = np.concatenate(([0], mask.view(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    starts, ends = edges[0::2], edges[1::2]
    best = np.argmax(ends - starts)
    out = np.zeros_like(mask)
    out[starts[best]:ends[best]] = True
    return out


def extract_VTR(VGS, ID, VDS, VOV_limit=None, ID_limit=None, T=300.0,
                 correct_series_R=False, RS=None, RD=None,
                 sub_mask=None, above_mask=None,
                 on_frac=0.5, off_frac=0.05,
                 window_length=11, polyorder=3, n_resample=None,
                 npts_fit=5, npts_fit_Y=None, edge_margin=None, y_slope_window=None,
                 return_details=False):
    """
    Extract V_T,on, V_T,off and V_TR = V_T,on - V_T,off from a linear-mode
    (low V_DS, ideally V_DS ~ a few phi_T) I_D-V_GS transfer curve.

    Parameters
    ----------
    VGS, ID : array_like
        Gate-voltage sweep and corresponding measured drain current
        (monotonically increasing VGS; ID > 0). Any consistent current
        unit works (A, A/um, uA, ...) since beta/ID0 carry the same unit.
    VDS : float
        Drain bias used for the measurement (V); should be small.
    VOV_limit : float
        Maximum allowed V_OV = V_GS - V_T,off for the above-threshold region.
    T : float
        Temperature in Kelvin (for phi_T).
    correct_series_R : bool
        False (default): use the RAW measured I_D for both V_T,on and
            V_T,off (standard method -- V_T,on/V_TR may be biased if
            R_S+R_D is not negligible).
        True: correct the V_T,on extraction for series resistance:
            * If RS and RD are BOTH given (not None) -> apply the direct
              correction I_D' = I_D / (1 - I_D*(RS+RD)/VDS) and extrapolate
              I_D' instead of I_D ("Modified I'_DON" method).
            * If RS and/or RD are NOT given -> automatically fall back to
              the RS/RD-INDEPENDENT Y-function method
              (Y = I_D/sqrt(g_m)) which needs no resistance value at all.
        V_T,off is left unaffected in all cases (the subthreshold current
        is too small for R_S/R_D to matter).
    sub_mask, above_mask : boolean array_like, optional
        Explicit index masks for the subthreshold / above-threshold
        regions. Auto-detected from `on_frac`/`off_frac` if not given.
    on_frac, off_frac : float
        Fractions of max(ID) used for auto-detecting the above-threshold
        (>= on_frac*max) and subthreshold (<= off_frac*max) regions.
    window_length, polyorder : int
        Savitzky-Golay smoothing parameters for all derivatives (g_m, SS).
        Increase window_length for noisier data.
    n_resample : int, optional
        Only relevant if VGS is NOT uniformly spaced. In that case this many
        points are used for the linear-interpolation resample onto a uniform
        grid (default: native resolution, i.e. len(VGS); avoid oversampling,
        which amplifies noise). If VGS is already uniform (typical for a
        voltage sweep), no resampling/interpolation is performed at all.
    npts_fit : int
        Number of points used in each local linear-regression extrapolation.
        Increase for noisier data (trades noise rejection vs. extrapolation
        window nonlinearity).
    npts_fit_Y : int, optional
        Number of points used in the local linear-regression fit for the Y-function.
        If None, defaults to int(0.5 / VGS_step) (i.e. ~0.5 V window, in points).
    edge_margin : int, optional
        Number of points excluded from each end of the sweep when searching
        for the SS-minimum / g_m-maximum locations, to avoid mistaking a
        Savitzky-Golay boundary artifact for the true extremum. Default:
        max(window_length, npts_fit).
    y_slope_window : int, optional
        Window size (in points) for the rolling-OLS-slope scan used to find
        the most-linear region of the Y-function (only used when
        correct_series_R=True and RS/RD are not given). Y = I_D/sqrt(g_m) is
        already a derived, noise-amplified quantity (g_m being a first
        derivative), so locating its point of maximum slope needs a WIDER
        window than a plain first derivative would -- filtering Y a second
        time (e.g. via Savitzky-Golay deriv=1) compounds noise the way a
        second derivative of I_D would. Default: 3x window_length (odd-ized).
    return_details : bool
        If True, also returns a dict of intermediate quantities (gm, SS, Y,
        beta, eta_SS, ID0, region masks, indices, estimated R_tot, method
        used, etc.) useful for diagnostics / plotting.

    Returns
    -------
    VTON, VTOFF, VTR : float
    details : dict   (only if return_details=True)
    """
    VGS = np.asarray(VGS, dtype=float)
    ID = np.asarray(ID, dtype=float)
    ID = np.abs(ID)
    n = len(VGS)
    margin = edge_margin if edge_margin is not None else max(window_length, npts_fit)

    # --- region masks -----------------------------------------------------
    auto_sub, auto_above = _default_masks(ID, on_frac=on_frac, off_frac=off_frac)
    sub_mask = auto_sub if sub_mask is None else np.asarray(sub_mask, dtype=bool)
    above_mask = auto_above if above_mask is None else np.asarray(above_mask, dtype=bool)

    # exclude sweep-edge points from extremum search only (edge points can
    # still be used inside the local linear-regression fit windows)
    sub_mask_search = _exclude_edges(sub_mask, n, margin)
    above_mask_search = _exclude_edges(above_mask, n, margin)
    if not np.any(sub_mask_search):
        sub_mask_search = sub_mask
    if not np.any(above_mask_search):
        above_mask_search = above_mask

    # --- smoothed derivatives on RAW data (needed for SS regardless of
    #     correction choice, and for the Y-function / R_tot diagnostic) ----
    gm_raw = compute_gm(VGS, ID, window_length, polyorder, n_resample)
    SS = compute_SS(VGS, ID, gm=np.gradient(ID, VGS))
    Y = compute_Y_function(ID, gm_raw)

    # =======================================================================
    # V_T,off (subthreshold; always from raw ID, unaffected by RS/RD)
    # =======================================================================
    # Guard against points where the noisy g_m is spuriously <= 0 (typically
    # very-low-current, near-noise-floor artifacts): SS = ln(10)*ID/gm is
    # only physically meaningful for gm > 0, so such points must never be
    # selected as the "SS minimum" -- they are excluded from the search
    # (but NOT from the eventual local-linear-fit window, which uses
    # whatever real neighboring points fall in range).
    valid_gm = gm_raw > 0
    if not ID_limit is None:
        valid_gm &= (ID >= ID_limit)
    SS_sub = np.where(sub_mask_search & valid_gm, SS, np.inf)
    if not np.any(np.isfinite(SS_sub)):
        SS_sub = np.where(sub_mask & valid_gm, SS, np.inf)  # relax edge margin if needed
    idx_off = int(np.nanargmin(SS_sub))
    SS_min = SS[idx_off]

    gm_above_raw = np.where(above_mask, gm_raw, -np.inf)
    gm_above_raw_search = np.where(above_mask_search, gm_raw, -np.inf)

    yw = y_slope_window
    if yw is None:
        above_idx = np.where(above_mask_search)[0]
        span = (above_idx.max() - above_idx.min() + 1) if len(above_idx) > 0 else window_length
        # Wide enough to average over a good fraction of the
        # above-threshold region -- Y's noise floor is elevated (it
        # already involved one differentiation via g_m), so a narrow
        # window here re-introduces the very noise sensitivity we're
        # trying to avoid.
        yw = _odd(max(3 * window_length, span // 3, npts_fit))
    Y_slope_scan = rolling_ols_slope(VGS, Y, window=yw)
    y_margin = max(margin, yw // 2)
    above_mask_for_Y = _exclude_edges(above_mask, n, y_margin)
    if not np.any(above_mask_for_Y):
        above_mask_for_Y = above_mask_search
    Y_slope_above_search = np.where(above_mask_for_Y, Y_slope_scan, -np.inf)

    if not correct_series_R:
        beta_est = extract_beta(VDS, np.nanmax(gm_above_raw_search), correct_series_R)
    else:
        beta_est = extract_beta(VDS, np.nanmax(Y_slope_above_search), correct_series_R)
    
    eta_SS = extract_eta_SS(SS_min, T)
    ID0 = compute_ID0(beta_est, eta_SS, VDS, T)

    logID = np.log10(np.clip(ID, 1e-300, None))
    off_slope, off_intercept = _local_linear_fit(VGS, logID, idx_off, npts=npts_fit)
    VTOFF = extrapolate_to_value(VGS, logID, idx_off, np.log10(ID0), npts=npts_fit)
    VGS_limit = VTOFF + VOV_limit if VOV_limit is not None else np.inf
    above_mask_search = above_mask_search & (VGS <= VGS_limit)

    # =======================================================================
    # V_T,on (above-threshold; method depends on correct_series_R)
    # =======================================================================
    Rtot_used, method = None, "raw_ID"
    on_array, on_ylabel = ID, "I_D"

    if not correct_series_R:
        gm_above_raw_search = np.where(above_mask_search & np.isfinite(ID), gm_raw, -np.inf)
        idx_on = int(np.nanargmax(gm_above_raw_search))
        VTON = extrapolate_to_value(VGS, ID, idx_on, 0.0, npts=npts_fit)
        on_array, on_ylabel = ID, "I_D (raw)"

    elif RS is not None and RD is not None:
        Rtot_used = RS + RD
        method = "ID_prime_correction"
        # Lightly smooth ID *before* applying the correction: I_D' is a
        # nonlinear function of I_D (division by a near-unity, ID-dependent
        # factor), so applying it point-by-point to noisy raw data and then
        # differentiating amplifies noise more than smoothing first does.
        ID_smoothed = smooth(ID, window_length=window_length, polyorder=polyorder)
        IDp = correct_ID_series_R(ID_smoothed, VDS, Rtot_used)
        gm_p = compute_gm(VGS, IDp, window_length, polyorder, n_resample)
        gm_p_above_search = np.where(above_mask_search & np.isfinite(IDp), gm_p, -np.inf)
        idx_on = int(np.nanargmax(gm_p_above_search))
        VTON = extrapolate_to_value(VGS, IDp, idx_on, 0.0, npts=npts_fit)
        # recompute beta from the corrected data (more accurate once R is removed)
        beta_est = extract_beta(VDS, np.nanmax(gm_p_above_search))
        on_array, on_ylabel = IDp, "I_D' (R-corrected)"

    else:
        method = "Y_function"
        # The correct point for the Y-based extrapolation is where Y ITSELF
        # is most linear in VGS, i.e. where its local slope peaks -- NOT
        # where dI_D/dVGS (g_m) peaks. Y already divides out most of the g_m
        # curvature, so its own point of maximum slope is generally at a
        # different VGS than g_m's peak, and using g_m's peak here biases
        # the extrapolation point away from where Y is actually straight.
        #
        # IMPORTANT (noise): Y = I_D/sqrt(g_m) is already a derived quantity
        # that involved one differentiation (g_m), so it carries amplified
        # noise relative to raw I_D. Computing a pointwise numerical
        # derivative of Y (e.g. via a Savitzky-Golay filter) would compound
        # that noise the way a second derivative of I_D does. Instead, we
        # scan a WIDE sliding-window OLS slope across Y directly -- this
        # performs only ONE level of differencing (like the final V_T
        # extrapolation fits), just repeated at every candidate index, and
        # the wider window suppresses Y's already-elevated noise floor.
        # yw = y_slope_window
        # if yw is None:
        #     above_idx = np.where(above_mask_search)[0]
        #     span = (above_idx.max() - above_idx.min() + 1) if len(above_idx) > 0 else window_length
        #     # Wide enough to average over a good fraction of the
        #     # above-threshold region -- Y's noise floor is elevated (it
        #     # already involved one differentiation via g_m), so a narrow
        #     # window here re-introduces the very noise sensitivity we're
        #     # trying to avoid.
        #     yw = _odd(max(3 * window_length, span // 3, npts_fit))
        # Y_slope_scan = rolling_ols_slope(VGS, Y, window=yw)
        # Near the array edges, rolling_ols_slope's window necessarily
        # shrinks (fewer supporting points -> higher variance), so those
        # points must not be allowed to win the argmax just by chance; only
        # consider candidates with a fully-supported window.
        y_margin = max(margin, yw // 2)
        above_mask_for_Y = _exclude_edges(above_mask, n, y_margin) & (VGS <= VGS_limit)
        if not np.any(above_mask_for_Y):
            above_mask_for_Y = above_mask_search
        Y_slope_above_search = np.where(above_mask_for_Y, Y_slope_scan, -np.inf)
        idx_on = int(np.nanargmax(Y_slope_above_search))
        if npts_fit_Y is None:
            npts_fit_Y = max(npts_fit, int(0.5 / np.nanmean(np.diff(VGS))))
        npts_fit = npts_fit_Y
        VTON = extrapolate_to_value(VGS, Y, idx_on, 0.0, npts=npts_fit)
        # Refine beta from the Y-line's own local slope at that point:
        # Y ~ sqrt(beta*VDS)*(VGS - VTON - eta_SS*VDS/2)  =>  beta = slope^2/VDS.
        y_slope, _ = _local_linear_fit(VGS, Y, idx_on, npts=npts_fit)
        if np.isfinite(y_slope) and y_slope > 0:
            beta_est = y_slope ** 2 / VDS
        # opportunistic diagnostic: estimate Rtot even though it wasn't needed
        Rtot_used = estimate_series_R(VGS, ID, VDS, beta_est, gm=gm_raw, idx=idx_on)
        on_array, on_ylabel = Y, "Y = I_D/sqrt(g_m)"
        if idx_on >= n - 1 - y_margin - 1:
            warnings.warn(
                "Y-function: the point of maximum slope sits at the edge of "
                "the measured VGS range. This likely means the true "
                "most-linear region extends beyond your sweep (mobility "
                "degradation onset not yet reached) -- treat V_T,on/V_TR "
                "from this method with extra caution for this dataset.",
                stacklevel=2)

    on_slope, on_intercept = _local_linear_fit(VGS, on_array, idx_on, npts=npts_fit)
    VTON -= eta_SS * VDS / 2.0
    VTR = VTON - VTOFF

    if not return_details:
        return VTON, VTOFF, VTR

    details = dict(
        method=method, idx_on=idx_on, idx_off=idx_off,
        gm=gm_raw, SS=SS, Y=Y, SS_min=SS_min, beta=beta_est,
        eta_SS=eta_SS, ID0=ID0, Rtot_used=Rtot_used,
        sub_mask=sub_mask, above_mask=above_mask,
        off_fit=(off_slope, off_intercept), on_fit=(on_slope, on_intercept),
        on_array=on_array, on_ylabel=on_ylabel,
    )
    return VTON, VTOFF, VTR, details


def get_sweep(VGS_full, ID_full, direction='forward'):
    """
    Get the forward or backward sweep from the VGS and ID arrays.

    Parameters
    ----------
    VGS_full : array_like
        Gate voltage array.
    ID_full : array_like
        Drain current array.
    direction : str
        'forward' for increasing VGS, 'backward' for decreasing VGS.

    Returns
    -------
    VGS : ndarray
        Gate voltage sweep.
    ID : ndarray
        Drain current sweep.
    """
    VGS_full = np.asarray(VGS_full, dtype=float)
    ID_full = np.asarray(ID_full, dtype=float)

    if direction == 'forward':
        dVGS_full = np.diff(VGS_full)
        turn_idx = np.where(dVGS_full <= 0)[0]
        end_idx = int(turn_idx[0]) + 1 if len(turn_idx) > 0 else len(VGS_full)
        VGS, ID = VGS_full[:end_idx], ID_full[:end_idx]
    elif direction == 'backward':
        dVGS_full = np.diff(VGS_full)
        turn_idx = np.where(dVGS_full < 0)[0]
        start_idx = int(turn_idx[0]) if len(turn_idx) > 0 else len(VGS_full) - 1
        VGS, ID = VGS_full[start_idx:], ID_full[start_idx:]
    else:
        raise ValueError("direction must be 'forward' or 'backward'")

    sort_idx = np.argsort(VGS)
    VGS, ID = VGS[sort_idx], ID[sort_idx]
    return VGS, ID


# ----------------------------------------------------------------------------
# gm/Id and gm (or gm/Cgg) based V_TR extractions
# ----------------------------------------------------------------------------
def compute_gmid_gmcgg(VGS, ID, Vg_cv=None, Cgg_cv=None, ID_limit=2e-12,
                        window_length=5, polyorder=3):
    """gm, gm/Id = d ln(Id)/dVg, and a "rising" signal for the twin-axis
    speed-vs-efficiency comparison: gm/Cgg if a CV sweep (Vg_cv, Cgg_cv) is
    supplied, else plain gm. Cgg data is often unavailable (most Id-Vg-only
    measurement setups don't have a paired CV sweep), so gm alone is the
    default -- it shares gm/Cgg's rise-then-plateau shape, just without the
    capacitance normalization.

    Points below ID_limit (raw Id at the measurement noise floor -- same
    convention as extract_VTR's ID_limit) are dropped from VGS/ID BEFORE any
    smoothing/differentiation is done, not masked afterward: at the noise
    floor, log(Id) swings wildly (Id can even flip sign before abs()), and if
    those points are left in the Savitzky-Golay window, the filter's boundary
    response contaminates the derivative at points just *inside* the valid
    region too -- masking to NaN only after the fact doesn't undo that. This
    mirrors extract_VTR's own ID_limit handling (there it guards which points
    the SS-minimum search can land on; here the equivalent is keeping the
    noise floor out of the derivative computation itself).

    Vg_cv, Cgg_cv, if given, are a separate (Vg, |Cgg|) CV sweep, interpolated
    onto the (already-trimmed) Id-Vg sweep's VGS grid -- the two measurements
    need not share a Vg grid.

    Returns (gm, gm_over_id, rising, rising_kind, valid), where rising_kind
    is "gm_over_cgg" or "gm" depending on whether CV data was supplied, and
    valid is a boolean mask (over the ORIGINAL VGS) marking the kept points;
    gm/gm_over_id/rising are NaN outside it.
    """
    VGS = np.asarray(VGS, dtype=float)
    ID = np.abs(np.asarray(ID, dtype=float))
    # Largest contiguous run only: an isolated noise spike deep in the
    # off-state can transiently clear ID_limit too, leaving a small
    # disconnected island next to the real turn-on run (see
    # _largest_true_run's docstring) -- keep just the genuine "on" region.
    valid = _largest_true_run(ID >= ID_limit)
    VGS_v, ID_v = VGS[valid], ID[valid]

    gm_v = compute_gm(VGS_v, ID_v, window_length=window_length, polyorder=polyorder)
    gm_over_id_v = smooth_derivative(VGS_v, np.log(np.clip(ID_v, 1e-300, None)), order=1,
                                      window_length=window_length, polyorder=polyorder)

    if Vg_cv is not None and Cgg_cv is not None:
        Cgg_interp = np.interp(VGS_v, Vg_cv, Cgg_cv)
        rising_v = gm_v / Cgg_interp
        rising_kind = "gm_over_cgg"
    else:
        rising_v = gm_v.copy()
        rising_kind = "gm"

    gm = np.full(VGS.shape, np.nan)
    gm_over_id = np.full(VGS.shape, np.nan)
    rising = np.full(VGS.shape, np.nan)
    gm[valid] = gm_v
    gm_over_id[valid] = gm_over_id_v
    rising[valid] = rising_v
    return gm, gm_over_id, rising, rising_kind, valid


# Measured-data analog of derivative_extrema() in TR_related_equations.py:
# there, VTR_deriv = Vg*(peak of dfT/dVg) - Vg*(trough of d(gm/Id)/dVg) --
# the Vg-separation between where transport efficiency (gm/Id) is degrading
# fastest and where speed (gm/Cgg, or gm alone if Cgg is unavailable) is
# rising fastest.
#
# gm/Id and gm/Cgg (or gm) are THEMSELVES once-differentiated quantities (gm
# already involved one derivative of Id), so a further pointwise
# Savitzky-Golay derivative here would compound noise the same way a second
# derivative of Id would -- this is exactly the situation rolling_ols_slope
# was written for (see its docstring / the Y-function branch of extract_VTR).
# A windowed OLS slope is used instead of smooth_derivative for this second
# level.
#
def _thresholded_lobe_centroid(x, y, extremum_idx, frac, mask):
    """Weighted centroid of x over the signed lobe of y around `extremum_idx`,
    using only the contiguous run where |y| stays >= frac * |y[extremum_idx]|
    AND `mask` is True.

    Bounding the window by a FRACTION OF THE LOBE'S OWN PEAK HEIGHT (rather
    than a fixed point count, or the whole domain) lets it adapt to how
    broad or narrow the real feature actually is: a sharp, well-isolated
    lobe gets a narrow window; a broad one gets a wide window; either way
    the averaging stays anchored to the actual extremum instead of picking
    up unrelated structure far away.

    Pathological case this is specifically built to handle (see
    OSFET_VTR_derivative.png): if the lobe is cut off before decaying to
    `frac` of its peak on one side -- either by noise flipping the
    threshold crossing early, or (more commonly here) by simply running
    into the edge of `mask`, e.g. the noise-floor-trimmed domain boundary
    sitting close to the trough -- naively using `frac` as-is would give a
    window whose width on that side is dictated by an arbitrary trimming
    boundary rather than by the shape of the lobe (and, incidentally, could
    make the OTHER side's window arbitrarily wider than the constrained
    side purely because of where the trim happened to fall). Instead, the
    fraction actually used is `max(frac, implied fraction at whichever
    side's boundary is hit first)` -- i.e. if a boundary would be hit
    before the lobe decays to `frac` of its peak, the threshold is raised
    (window narrowed) on BOTH sides to match, so the two sides stay on the
    same footing. The window can still end up asymmetric in Volts (a
    genuinely lopsided lobe stays lopsided), but its narrowness is no
    longer at the mercy of an unrelated trimming boundary.

    "Boundary", for this purpose, means EITHER `mask` going False/non-finite
    OR the first point where y crosses to the opposite sign of
    y[extremum_idx] -- i.e. the walk never reaches past a sign change into a
    neighboring, unrelated lobe. Without that sign check, a noisy
    already-once-differentiated signal (e.g. d(gm/Id)/dVg computed via a
    second Savitzky-Golay pass) can oscillate right at the edge of the
    trimmed/masked domain -- a spurious opposite-sign spike a few points
    outside the real lobe would otherwise get treated as "the boundary
    height", forcing `frac` up to ~1.0 and collapsing the window to a
    single point even though the actual lobe was never clipped at all.
    """
    y = np.asarray(y, dtype=float)
    sign = 1.0 if y[extremum_idx] >= 0 else -1.0
    peak = abs(y[extremum_idx])
    n = len(y)

    def _same_side(i):
        return np.isfinite(y[i]) and sign * y[i] > 0

    def _inner_limit(step):
        i = extremum_idx
        while 0 <= i + step < n and mask[i + step] and _same_side(i + step):
            i += step
        return i

    implied_lo = abs(y[_inner_limit(-1)]) / peak if peak > 0 else 1.0
    implied_hi = abs(y[_inner_limit(+1)]) / peak if peak > 0 else 1.0
    eff_frac = min(1.0, max(frac, implied_lo, implied_hi))
    thresh = eff_frac * peak

    lo = extremum_idx
    while (lo - 1 >= 0 and mask[lo - 1] and _same_side(lo - 1)
           and abs(y[lo - 1]) >= thresh):
        lo -= 1
    hi = extremum_idx
    while (hi + 1 < n and mask[hi + 1] and _same_side(hi + 1)
           and abs(y[hi + 1]) >= thresh):
        hi += 1
    hi += 1  # exclusive end, for slicing

    # A single-point window (lo == hi-1 == extremum_idx) has zero width, so
    # its trapezoidal integral is 0/0 -- e.g. when the requested `frac` is
    # tighter than the Vg step size can resolve (coarsely-sampled data where
    # even the immediate neighbor has already dropped below `thresh`).
    # Rather than return NaN, fall back to including one more same-side
    # neighbor on each side where available, just enough to give the
    # centroid a non-degenerate interval to integrate over.
    if hi - lo <= 1:
        if lo - 1 >= 0 and mask[lo - 1] and _same_side(lo - 1):
            lo -= 1
        if hi < n and mask[hi] and _same_side(hi):
            hi += 1

    w = np.clip(sign * y[lo:hi], 0.0, None)
    w = np.nan_to_num(w, nan=0.0)
    xg = x[lo:hi]
    centroid = np.trapezoid(xg * w, xg) / np.trapezoid(w, xg)
    return centroid, lo, hi


# Both trough and peak now use the SAME fraction-of-peak-height windowed
# centroid (`_thresholded_lobe_centroid`), replacing two earlier, different
# treatments:
#   - The rising side used to average over the WHOLE search domain. That
#     works when the lobe's derivative genuinely decays to ~0 on the
#     plateau (as tested for both example devices), but has no way to
#     adapt if it doesn't.
#   - The gm/Id trough used to average over a FIXED POINT COUNT around the
#     argmin, because d(gm/Id)/dVg's negative lobe does NOT decay back to
#     ~0 the way the rising side's positive lobe does (gm/Id keeps falling
#     roughly like 1/Vov well past the sharp trough) -- a full-domain
#     centroid there is dominated by that long tail and lands far from the
#     true trough (tested: it shifted the Si nFET trough from 0.575 V to
#     0.69 V, past the rising centroid, making VTR_deriv go NEGATIVE).
# A fraction-of-peak threshold subsumes both: it naturally narrows around a
# sharp lobe (like the trough) and widens around a broad one (like the
# rising side), and -- the point of this version -- it naturally clips
# itself at the edge of the valid/search domain rather than reaching for
# data that isn't there, which a fixed full-domain or fixed-point-count
# window cannot do.
#
# trough_frac / peak_frac are separate (not one shared lobe_frac) so the two
# sides can be tuned independently -- e.g. gm/Id's negative lobe is often
# expected to tail more than the rising side's positive lobe (gm/Id keeps
# falling ~1/Vov indefinitely, whereas gm can saturate once the device is
# fully above threshold), which would argue for a HIGHER trough_frac (tighter
# window) and a LOWER peak_frac (can afford a looser one since there's
# supposedly less tail to pick up).
#
# CAVEAT, checked against both example devices here before picking defaults:
# that asymmetry is real for the Si nFET (rising-side drift from its argmax
# is smaller than the trough's at every frac tested, e.g. ~0.03 V vs ~0.07 V
# at frac=0.1) but backwards for the OSFET -- its RISING side (gm or
# gm/Cgg) drifts MUCH more than its trough at every frac tested (~0.5 V vs
# ~0.14 V at frac=0.1), because the OSFET's above-threshold current keeps
# curving for ~2 V past the argmax rather than settling into a flat gm the
# way the Si device does. So "gm doesn't tail" is a per-device property, not
# a general rule; a lower peak_frac default would make the OSFET case (the
# one used to motivate the clipped-window fix above) noticeably worse, not
# better. Both default to 0.5 (half-max, FWHM convention, matching the
# previous shared-lobe_frac default) until there's a reason to split them
# for a specific dataset -- pass trough_frac/peak_frac explicitly to tune
# per-device.
def extract_VTR_derivative(VGS, ID, Vg_cv=None, Cgg_cv=None, ID_limit=2e-12,
                            window_length=5, polyorder=3, slope_window=None,
                            edge_margin=None, gmid_edge_margin=None,
                            trough_frac=0.9, peak_frac=0.5):
    VGS = np.asarray(VGS, dtype=float)
    n = len(VGS)
    gm, gm_over_id, rising, rising_kind, valid = compute_gmid_gmcgg(
        VGS, ID, Vg_cv, Cgg_cv, ID_limit, window_length, polyorder)

    sw = slope_window if slope_window is not None else _odd(max(3 * window_length, n // 8))

    # Compute both derivatives ONLY over the trimmed (noise-floor removed)
    # range, same rationale as compute_gmid_gmcgg's own trimming: sizing
    # either derivative in units of consecutive ARRAY entries over the full
    # NaN-padded array would silently let it span across the off-state gap
    # as if those were adjacent, contiguous samples.
    #
    # d(gm/Id)/dVg uses smooth_derivative (Savitzky-Golay on a resampled
    # uniform grid) rather than rolling_ols_slope -- gm/Id's trough is sharp
    # enough that a fixed-length OLS window tends to blunt/shift it, whereas
    # savgol's polynomial fit tracks the sharp curvature better here.
    # d(rising)/dVg (gm or gm/Cgg) keeps rolling_ols_slope: that lobe is
    # broad, so a windowed-OLS slope (built for compounding-noise avoidance
    # on an already-once-differentiated quantity) still applies well there.
    VGS_v = VGS[valid]
    d_gmid_v = smooth_derivative(VGS_v, gm_over_id[valid], order=1,
                                  window_length=window_length, polyorder=polyorder)
    d_rising_v = rolling_ols_slope(VGS_v, rising[valid], window=sw)
    d_gmid = np.full(n, np.nan)
    d_rising = np.full(n, np.nan)
    d_gmid[valid] = d_gmid_v
    d_rising[valid] = d_rising_v

    # Exclude a margin from each end of the TRIMMED run -- not the raw
    # sweep's endpoints (those are off-state and already outside `valid`).
    # The two derivatives get INDEPENDENT margins because they come from
    # different edge-effect sources: `d_rising` uses rolling_ols_slope,
    # whose window genuinely shrinks near the ends, with effects felt out to
    # ~sw/2 points in. `d_gmid` uses smooth_derivative (a single
    # Savitzky-Golay pass), whose edge effects are confined to roughly
    # window_length/2 points -- much smaller than `sw`. Sharing the
    # rolling_ols_slope-sized margin for BOTH (as before) could exclude a
    # genuine, sharp gm/Id trough that happens to sit close to where the
    # noise-floor trim starts, forcing the search onto the decay tail
    # instead of the real feature (seen on device "1_1_3": true trough at
    # Vg=0.70 V was masked out, landing the centroid at 0.92 V instead).
    margin_rising = edge_margin if edge_margin is not None else max(window_length, sw // 2)
    margin_gmid = gmid_edge_margin if gmid_edge_margin is not None else window_length // 2

    def _search_mask(margin):
        mask_v = _exclude_edges(np.ones(len(VGS_v), dtype=bool), len(VGS_v), margin)
        mask = np.zeros(n, dtype=bool)
        mask[valid] = mask_v
        return mask if np.any(mask) else valid

    search_mask_gmid = _search_mask(margin_gmid)
    search_mask_rising = _search_mask(margin_rising)

    idx_trough = int(np.nanargmin(np.where(search_mask_gmid, d_gmid, np.inf)))
    idx_peak = int(np.nanargmax(np.where(search_mask_rising, d_rising, -np.inf)))

    Vg_trough, lo_t, hi_t = _thresholded_lobe_centroid(
        VGS, d_gmid, idx_trough, trough_frac, search_mask_gmid)
    Vg_peak, lo_p, hi_p = _thresholded_lobe_centroid(
        VGS, d_rising, idx_peak, peak_frac, search_mask_rising)

    VTR_deriv = Vg_peak - Vg_trough

    details = dict(gm=gm, gm_over_id=gm_over_id, rising=rising, rising_kind=rising_kind,
                    d_gmid=d_gmid, d_rising=d_rising, slope_window=sw,
                    trough_frac=trough_frac, peak_frac=peak_frac,
                    idx_trough=idx_trough, trough_window=(lo_t, hi_t),
                    idx_peak=idx_peak, peak_window=(lo_p, hi_p),
                    Vg_trough=Vg_trough, Vg_peak=Vg_peak)
    return VTR_deriv, details

def _find_crossing(x, y, ref):
    """First x (ascending) at which y crosses ref, linearly interpolated
    between the two bracketing samples."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    x, y = x[finite], y[finite]
    d = y - ref
    sign_change = np.where(np.diff(np.sign(d)) != 0)[0]
    if len(sign_change) == 0:
        return np.nan
    i = sign_change[0]
    x0, x1 = x[i], x[i + 1]
    d0, d1 = d[i], d[i + 1]
    if d1 == d0:
        return x0
    return x0 + (0 - d0) * (x1 - x0) / (d1 - d0)


def extract_VTR_gm_gmid(VGS, ID, Vg_cv=None, Cgg_cv=None, ID_limit=2e-12,
                        window_length=5, polyorder=3, slope_window=None,
                        edge_margin=None, gmid_edge_margin=None,
                        ref_frac=0.5):
    VGS = np.asarray(VGS, dtype=float)
    n = len(VGS)
    gm, gm_over_id, rising, rising_kind, valid = compute_gmid_gmcgg(
        VGS, ID, Vg_cv, Cgg_cv, ID_limit, window_length, polyorder)

    sw = slope_window if slope_window is not None else _odd(max(3 * window_length, n // 8))

    VGS_v = VGS[valid]
    gm_norm = gm / np.nanmax(gm)
    rising_norm = rising / np.nanmax(rising)
    gm_over_id_norm = gm_over_id / np.nanmax(gm_over_id)

    VT_gm = _find_crossing(VGS_v, gm_norm[valid], ref_frac)
    VT_rising = _find_crossing(VGS_v, rising_norm[valid], ref_frac)
    VT_gm_over_id = _find_crossing(VGS_v, gm_over_id_norm[valid], ref_frac)
    VTR_gm_gmid = VT_rising - VT_gm_over_id

    details = dict(gm=gm, gm_over_id=gm_over_id,
                    gm_norm=gm_norm, rising_norm=rising_norm, gm_over_id_norm=gm_over_id_norm,
                    rising=rising, rising_kind=rising_kind, valid=valid,
                    VT_gm=VT_gm, VT_rising=VT_rising, VT_gm_over_id=VT_gm_over_id)
    return VTR_gm_gmid, details


# ----------------------------------------------------------------------------
# gm/Id + gm 50% crossing, for a SATURATION-region (high V_DS) Id-Vg curve
# ----------------------------------------------------------------------------
# Same 50%-of-max crossing method as extract_VTR_gm_gmid (gm/Id = d ln(Id)/dVg
# paired with plain gm = dId/dVg), just applied to a SATURATION (high V_DS)
# Id-Vg sweep instead of a linear-region one -- kept as its own function
# (rather than just calling extract_VTR_gm_gmid on saturation data) so the
# saturation use case is explicit and separately testable.
def compute_gmid_gmsat(VGS, ID, ID_limit=2e-12, window_length=5, polyorder=3):
    """gm/Id = d ln(Id)/dVg and gm = dId/dVg for a SATURATION (high V_DS)
    Id-Vg sweep.

    Same ID_limit/noise-floor trimming convention as compute_gmid_gmcgg
    (points below ID_limit are dropped before smoothing/differentiation, not
    masked afterward, to keep the noise floor out of the Savitzky-Golay
    window entirely).

    Returns (gm_over_id, gm, valid), each NaN outside `valid`.
    """
    VGS = np.asarray(VGS, dtype=float)
    ID = np.abs(np.asarray(ID, dtype=float))
    valid = _largest_true_run(ID >= ID_limit)
    VGS_v, ID_v = VGS[valid], ID[valid]

    gm_over_id_v = smooth_derivative(VGS_v, np.log(np.clip(ID_v, 1e-300, None)), order=1,
                                      window_length=window_length, polyorder=polyorder)
    gm_v = compute_gm(VGS_v, ID_v, window_length=window_length, polyorder=polyorder)

    gm_over_id = np.full(VGS.shape, np.nan)
    gm = np.full(VGS.shape, np.nan)
    gm_over_id[valid] = gm_over_id_v
    gm[valid] = gm_v
    return gm_over_id, gm, valid


def extract_VTR_gm_gmid_sat(VGS, ID, ID_limit=2e-12, window_length=5, polyorder=3,
                             ref_frac=0.5):
    """
    V_TR from the Vg-separation between the 50%-of-max crossings of gm/Id
    and gm = dId/dVg, for a SATURATION-region (high V_DS) Id-Vg transfer
    curve -- the saturation counterpart of extract_VTR_gm_gmid (same method,
    same signals, just applied to a saturation sweep).

    Returns (VTR_gm_gmid_sat, details), details holding gm_over_id, gm,
    their normalized versions, valid, and the two crossing voltages
    (VT_gm_over_id, VT_gm) -- mirrors extract_VTR_gm_gmid's details dict.
    """
    VGS = np.asarray(VGS, dtype=float)
    gm_over_id, gm, valid = compute_gmid_gmsat(
        VGS, ID, ID_limit=ID_limit, window_length=window_length, polyorder=polyorder)

    VGS_v = VGS[valid]
    gm_norm = gm / np.nanmax(gm)
    gm_over_id_norm = gm_over_id / np.nanmax(gm_over_id)

    VT_gm = _find_crossing(VGS_v, gm_norm[valid], ref_frac)
    VT_gm_over_id = _find_crossing(VGS_v, gm_over_id_norm[valid], ref_frac)
    VTR_gm_gmid_sat = VT_gm - VT_gm_over_id

    details = dict(gm_over_id=gm_over_id, gm=gm,
                    gm_norm=gm_norm, gm_over_id_norm=gm_over_id_norm,
                    valid=valid, VT_gm=VT_gm, VT_gm_over_id=VT_gm_over_id)
    return VTR_gm_gmid_sat, details
