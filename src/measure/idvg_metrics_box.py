"""Box plots of VT, field-effect mobility, subthreshold swing and VTR per channel length, offline from saved data.

    python -m measure.idvg_metrics_box --samples wd_bobafet_6802 wd_bobafet_6916 [--die 0_0] [--exclude-L 10]
                                       [--out .pptx_figs/wd_bobafet] [--scan]

Per device: the newest IdVg_main file (measure.plot_idvg_all.newest_main_files), cycle 1, forward sweep, Vd = --vd.
With --scan (a scan-only run, no main sweeps): the newest IdVg_initial_scan file instead, forward branch, its drain
voltage taken to be --vd (scan files do not record it), and only devices whose newest row in the die's
summary_*.csv is 'measured'; output names end in _scan and the x-label says "screening scan".
- VT: Vg at Id = 1e-7 A * W/L (the run screen's constant-current definition), log-linear interpolation.
- muFE: linear-regime field-effect mobility, max(gm) * L / (W * Cox * Vd), gm = dId/dVg (central differences);
  Cox = eps0 * k / t_ox, default 10 nm HfO2 with k = 17 (1.51 uF/cm^2).
- SS: the steepest dVg/dlog10(Id), from a line fit over 3 consecutive points, between Id = 10 pA (above the noise
  floor) and the VT current.
- VTR = VT,on - VT,off with measure.tr_extraction.extract_VTR (K. Jana et al., Eqs. 6-9), raw-Id method
  (correct_series_R=False): VT,on from the linear extrapolation of Id at max gm (minus eta_SS*Vd/2), VT,off from the
  log10(Id) extrapolation at min SS to I_D0; points below --id-limit are not SS-minimum candidates.
Writes metrics.csv and one PNG per metric to --out; nothing under data/ is written.
"""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from measure.idvg_pipeline import Vt_at_constant_Id, get_sweeps
from measure.plot_idvg_all import PALETTE, WL_RE, newest_main_files
from measure.tr_extraction import extract_VTR

EPS0 = 8.8541878128e-12   # F/m
VT_ID_PER_WL = 1e-7
SS_ID_MIN = 1e-11
METRICS = {'VT_V': '$V_T$ (V)', 'muFE_cm2Vs': r'$\mu_{FE}$ (cm$^2$/V·s)', 'SS_mVdec': 'SS (mV/dec)',
           'VTR_V': '$V_{TR}$ (V)'}


def vtr_metrics(vg: np.ndarray, Id: np.ndarray, vd: float, id_limit: float) -> dict:
    """VT,on, VT,off and VTR (V) from extract_VTR, raw-Id method; NaN when the curve has no usable region.

    extract_VTR returns numbers even for a curve that never turns on, so a curve with no point between id_limit and
    extract_VTR's subthreshold ceiling (5 % of max Id) gets NaN instead."""
    Id = np.abs(Id)
    on = off = vtr = np.nan
    if np.any((Id >= id_limit) & (Id <= 0.05 * np.nanmax(Id))):
        try:
            on, off, vtr = extract_VTR(vg, Id, vd, ID_limit=id_limit, correct_series_R=False)
        except (ValueError, IndexError, np.linalg.LinAlgError):
            pass
    return {'VTon_V': float(on), 'VToff_V': float(off), 'VTR_V': float(vtr)}


def device_metrics(vg: np.ndarray, Id: np.ndarray, W: float, L: float, vd: float, cox: float) -> dict:
    """VT (V), muFE (cm^2/Vs) and SS (mV/dec) of one forward Id-Vg branch; NaN where a metric cannot be extracted."""
    Id = np.abs(Id)
    out = {'VT_V': np.nan, 'muFE_cm2Vs': np.nan, 'SS_mVdec': np.nan}
    i_vt = VT_ID_PER_WL * W / L
    if np.nanmax(Id) > i_vt:
        out['VT_V'] = Vt_at_constant_Id(vg, Id, Id_target=i_vt)
    gm = np.gradient(Id, vg)
    out['muFE_cm2Vs'] = float(np.nanmax(gm)) * L / (W * cox * abs(vd)) * 1e4   # m^2/Vs -> cm^2/Vs
    ss = []
    for k in range(len(vg) - 2):
        y, x = Id[k:k + 3], vg[k:k + 3]
        if np.all(y > SS_ID_MIN) and np.all(y < i_vt):
            slope = np.polyfit(x, np.log10(y), 1)[0]   # decades per volt
            if slope > 0:
                ss.append(1e3 / slope)
    if ss:
        out['SS_mVdec'] = float(min(ss))
    return out


def newest_scan_files(die_dir: Path) -> list[tuple[str, Path, float, float]]:
    """(device, newest IdVg_initial_scan file, W, L) for each device folder that has one: the screening double
    sweep, a single drain voltage (the config's Vd_scan) and no Drain_Voltage or Cycle column."""
    out = []
    for dev in sorted(d for d in Path(die_dir).iterdir() if d.is_dir()):
        files = sorted(dev.glob('IdVg_initial_scan_*.csv'))   # timestamp first in the name: sorted by time
        if not files:
            continue
        m = WL_RE.search(files[-1].name)
        if not m:
            raise ValueError(f"no _W.._L.. in file name: {files[-1]}")
        out.append((dev.name, files[-1], float(m[1]), float(m[2])))
    return out


def measured_devices(die_dir: Path) -> set[str]:
    """Devices whose newest row across the die's summary_*.csv files has status 'measured'."""
    parts = [pd.read_csv(p, usecols=['time', 'key', 'status']) for p in sorted(Path(die_dir).glob('summary_*.csv'))]
    if not parts:
        raise ValueError(f'no summary_*.csv in {die_dir}')
    latest = pd.concat(parts).sort_values('time', kind='stable').drop_duplicates('key', keep='last')
    return set(latest.loc[latest['status'] == 'measured', 'key'])


def collect(data_dir: Path, samples: list[str], die: str, vd: float, cox: float, exclude_L: list[float],
            id_limit: float = 1e-11, scan: bool = False) -> pd.DataFrame:
    """One row per device of each sample's die: sample, device, W, L, file, VT_V, muFE_cm2Vs, SS_mVdec, VTon_V,
    VToff_V, VTR_V. Devices with L in exclude_L, or with fewer than 6 points at Vd = vd in cycle 1, are left out.
    scan=True reads each device's newest initial scan instead of its main sweep, taking its drain voltage to be vd,
    and keeps only the devices whose newest row in the die's summary_*.csv is 'measured' (a screened-out device's
    scan is a short, an open or a breakdown, whose extrapolated VT means nothing)."""
    rows = []
    for s in samples:
        die_dir = Path(data_dir) / s / die
        files = (newest_scan_files if scan else newest_main_files)(die_dir)
        keep = measured_devices(die_dir) if scan else None
        for dev, path, W, L in files:
            if L in exclude_L or (keep is not None and dev not in keep):
                continue
            d = pd.read_csv(path)
            if not scan:
                d = d[np.isclose(d['Drain_Voltage'], vd) & (d['Cycle'] == d['Cycle'].min())]
            if len(d) < 6:
                continue
            f = {k: v['f'] for k, v in (('vg', get_sweeps(d['Gate_Voltage'].to_numpy())),
                                        ('id', get_sweeps(d['Drain_Current'].to_numpy())))}
            rows.append({'sample': s, 'device': dev, 'W': W, 'L': L, 'file': path.name,
                         **device_metrics(f['vg'], f['id'], W, L, vd, cox),
                         **vtr_metrics(f['vg'], f['id'], vd, id_limit)})
    return pd.DataFrame(rows)


def box(df: pd.DataFrame, metric: str, out: Path, font_scale: float = 2.0, note: str = '') -> Path:
    """Box (median, quartiles, 1.5 IQR whiskers) plus jittered points, grouped by sample, coloured by L.

    Up to len(PALETTE) lengths: categorical hues and a legend. More: one sequential ramp over log L, and each box
    labelled with its L on the x-axis (a legend that long would cover the data). note, when given, is added to the
    x-label in brackets (e.g. 'screening scan')."""
    suffix = f' ({note})' if note else ''
    lengths = sorted(df['L'].unique())
    keys = sorted(set(df['L']) | {1.0, 2.0, 4.0, 10.0})
    if len(keys) <= len(PALETTE):
        colours = dict(zip(keys, PALETTE))
    else:   # more lengths than categorical hues: L is a magnitude, so one sequential ramp over log L
        logs = np.log10(lengths)
        span = (logs.max() - logs.min()) or 1.0
        colours = {L: plt.cm.viridis(0.9 * (lg - logs.min()) / span) for L, lg in zip(lengths, logs)}
    samples = list(dict.fromkeys(df['sample']))
    rng = np.random.default_rng(0)
    with plt.rc_context({'font.size': 10 * font_scale}):
        fig, ax = plt.subplots(figsize=(10, 7))
        n = len(lengths)
        for i, s in enumerate(samples):
            for j, L in enumerate(lengths):
                v = df.loc[(df['sample'] == s) & (df['L'] == L), metric].dropna().to_numpy()
                if not len(v):
                    continue
                x = i * (n + 1) + j
                c = colours[L]
                ax.boxplot(v, positions=[x], widths=0.7, showfliers=False, patch_artist=True,
                           boxprops=dict(facecolor=c, alpha=0.35, edgecolor=c, linewidth=2),
                           medianprops=dict(color='black', linewidth=2), whiskerprops=dict(color=c, linewidth=2),
                           capprops=dict(color=c, linewidth=2))
                ax.scatter(x + rng.uniform(-0.25, 0.25, len(v)), v, s=6, color=c, alpha=0.35, linewidths=0)
        names = [s.replace('wd_bobafet_', '') for s in samples]
        if len(keys) <= len(PALETTE):   # few lengths: a legend
            ax.set_xticks([i * (n + 1) + (n - 1) / 2 for i in range(len(samples))], names)
            ax.legend(handles=[Patch(facecolor=colours[L], alpha=0.6, label=f'L = {L:g} µm') for L in lengths],
                      frameon=False, loc='best')
            if note:
                ax.set_xlabel(suffix.strip())
        else:   # many lengths: a legend would cover the data, so each box is labelled with its L
            ax.set_xticks([i * (n + 1) + j for i in range(len(samples)) for j in range(n)],
                          [f'{L:g}' for _ in samples for L in lengths], rotation=60)
            ax.set_xlabel(f"L (µm)   {' | '.join(names)}{suffix}")
        ax.set_ylabel(METRICS[metric])
        ax.grid(True, axis='y', color='0.85', linewidth=0.6)
        for side in ('top', 'right'):
            ax.spines[side].set_visible(False)
        fig.tight_layout()
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=300)
        plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--samples', nargs='+', required=True)
    ap.add_argument('--die', default='0_0')
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--vd', type=float, default=0.05)
    ap.add_argument('--exclude-L', type=float, nargs='*', default=[10.0])
    ap.add_argument('--tox-nm', type=float, default=10.0)
    ap.add_argument('--k', type=float, default=17.0)
    ap.add_argument('--font-scale', type=float, default=2.0)
    ap.add_argument('--out', default='.pptx_figs')
    ap.add_argument('--id-limit', type=float, default=1e-11, help='lowest Id (A) for the SS-minimum search of VT,off')
    ap.add_argument('--scan', action='store_true',
                    help='read the initial screening scans (IdVg_initial_scan_*), for scan-only runs; Vd = --vd')
    a = ap.parse_args(argv)
    out = Path(a.out)
    if any(out.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    cox = EPS0 * a.k / (a.tox_nm * 1e-9)
    print(f"Cox = {cox * 1e2:.3g} uF/cm^2 ({a.tox_nm:g} nm, k = {a.k:g})")
    df = collect(Path(a.data_dir), a.samples, a.die, a.vd, cox, a.exclude_L, a.id_limit, a.scan)
    if df.empty:
        kind = 'measured screening scans' if a.scan else 'main sweeps'
        ap.error(f'no {kind} at Vd = {a.vd:g} V under {a.data_dir}/{{{",".join(a.samples)}}}/{a.die}')
    out.mkdir(parents=True, exist_ok=True)
    tag = '_'.join(s.replace('wd_bobafet_', '') for s in a.samples) + ('_scan' if a.scan else '')
    df.to_csv(out / f'metrics_{tag}_{a.die}_Vd{a.vd:g}.csv', index=False)
    metrics = [m for m in METRICS if m in df]
    cols = metrics + [c for c in ('VTon_V', 'VToff_V') if c in df]
    print(df.groupby(['sample', 'L'])[cols].agg(['median', 'count']).round(3).to_string())
    for m in metrics:
        print(box(df, m, out / f'box_{m}_{tag}_{a.die}_Vd{a.vd:g}.png', a.font_scale,
                  'screening scan' if a.scan else ''))


if __name__ == '__main__':
    main()
