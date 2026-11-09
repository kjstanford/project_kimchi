"""Box plots of VT, field-effect mobility and subthreshold swing per channel length, offline from saved data.

    python -m measure.idvg_metrics_box --samples wd_bobafet_6802 wd_bobafet_6916 [--die 0_0] [--exclude-L 10]
                                       [--out .pptx_figs/wd_bobafet]

Per device: the newest IdVg_main file (measure.plot_idvg_all.newest_main_files), cycle 1, forward sweep, Vd = --vd.
- VT: Vg at Id = 1e-7 A * W/L (the run screen's constant-current definition), log-linear interpolation.
- muFE: linear-regime field-effect mobility, max(gm) * L / (W * Cox * Vd), gm = dId/dVg (central differences);
  Cox = eps0 * k / t_ox, default 10 nm HfO2 with k = 17 (1.51 uF/cm^2).
- SS: the steepest dVg/dlog10(Id), from a line fit over 3 consecutive points, between Id = 10 pA (above the noise
  floor) and the VT current.
- VTR = VT,on - VT,off with --tr-funs's extract_VTR (K. Jana et al., Eqs. 6-9), raw-Id method
  (correct_series_R=False): VT,on from the linear extrapolation of Id at max gm (minus eta_SS*Vd/2), VT,off from the
  log10(Id) extrapolation at min SS to I_D0; points below --id-limit are not SS-minimum candidates.
Writes metrics.csv and one PNG per metric to --out; nothing under data/ is written.
"""
import argparse
import importlib.util
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from measure.idvg_pipeline import Vt_at_constant_Id, get_sweeps
from measure.plot_idvg_all import PALETTE, newest_main_files

EPS0 = 8.8541878128e-12   # F/m
VT_ID_PER_WL = 1e-7
SS_ID_MIN = 1e-11
METRICS = {'VT_V': '$V_T$ (V)', 'muFE_cm2Vs': r'$\mu_{FE}$ (cm$^2$/V·s)', 'SS_mVdec': 'SS (mV/dec)',
           'VTR_V': '$V_{TR}$ (V)'}


def load_tr_funs(path: Path):
    """Imports the V_TR extraction module from a file path (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location('tr_funs', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vtr_metrics(tr, vg: np.ndarray, Id: np.ndarray, vd: float, id_limit: float) -> dict:
    """VT,on, VT,off and VTR (V) from tr.extract_VTR, raw-Id method; NaN when the extraction fails."""
    try:
        on, off, vtr = tr.extract_VTR(vg, np.abs(Id), vd, ID_limit=id_limit, correct_series_R=False)
    except (ValueError, IndexError, np.linalg.LinAlgError):
        on = off = vtr = np.nan
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


def collect(data_dir: Path, samples: list[str], die: str, vd: float, cox: float, exclude_L: list[float],
            tr=None, id_limit: float = 1e-11) -> pd.DataFrame:
    rows = []
    for s in samples:
        for dev, path, W, L in newest_main_files(Path(data_dir) / s / die):
            if L in exclude_L:
                continue
            d = pd.read_csv(path)
            d = d[np.isclose(d['Drain_Voltage'], vd) & (d['Cycle'] == d['Cycle'].min())]
            if len(d) < 6:
                continue
            f = {k: v['f'] for k, v in (('vg', get_sweeps(d['Gate_Voltage'].to_numpy())),
                                        ('id', get_sweeps(d['Drain_Current'].to_numpy())))}
            rows.append({'sample': s, 'device': dev, 'W': W, 'L': L, 'file': path.name,
                         **device_metrics(f['vg'], f['id'], W, L, vd, cox),
                         **(vtr_metrics(tr, f['vg'], f['id'], vd, id_limit) if tr else {})})
    return pd.DataFrame(rows)


def box(df: pd.DataFrame, metric: str, out: Path, font_scale: float = 2.0) -> Path:
    """Box (median, quartiles, 1.5 IQR whiskers) plus jittered points, grouped by sample, coloured by L."""
    lengths = sorted(df['L'].unique())
    colours = dict(zip(sorted(set(df['L']) | {1.0, 2.0, 4.0, 10.0}), PALETTE))
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
        ax.set_xticks([i * (n + 1) + (n - 1) / 2 for i in range(len(samples))],
                      [s.replace('wd_bobafet_', '') for s in samples])
        ax.set_ylabel(METRICS[metric])
        ax.grid(True, axis='y', color='0.85', linewidth=0.6)
        for side in ('top', 'right'):
            ax.spines[side].set_visible(False)
        ax.legend(handles=[Patch(facecolor=colours[L], alpha=0.6, label=f'L = {L:g} µm') for L in lengths],
                  frameon=False, loc='best')
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
    ap.add_argument('--tr-funs', default='.pptx_figs/new_TR_extraction_funs.py',
                    help='file with extract_VTR; VTR is skipped if it is missing')
    ap.add_argument('--id-limit', type=float, default=1e-11, help='lowest Id (A) for the SS-minimum search of VT,off')
    a = ap.parse_args(argv)
    out = Path(a.out)
    if any(out.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    cox = EPS0 * a.k / (a.tox_nm * 1e-9)
    print(f"Cox = {cox * 1e2:.3g} uF/cm^2 ({a.tox_nm:g} nm, k = {a.k:g})")
    tr = load_tr_funs(Path(a.tr_funs)) if Path(a.tr_funs).is_file() else None
    if tr is None:
        print(f"no {a.tr_funs}: VTR skipped")
    df = collect(Path(a.data_dir), a.samples, a.die, a.vd, cox, a.exclude_L, tr, a.id_limit)
    out.mkdir(parents=True, exist_ok=True)
    tag = '_'.join(s.replace('wd_bobafet_', '') for s in a.samples)
    df.to_csv(out / f'metrics_{tag}_{a.die}_Vd{a.vd:g}.csv', index=False)
    metrics = [m for m in METRICS if m in df]
    cols = metrics + [c for c in ('VTon_V', 'VToff_V') if c in df]
    print(df.groupby(['sample', 'L'])[cols].agg(['median', 'count']).round(3).to_string())
    for m in metrics:
        print(box(df, m, out / f'box_{m}_{tag}_{a.die}_Vd{a.vd:g}.png', a.font_scale))


if __name__ == '__main__':
    main()
