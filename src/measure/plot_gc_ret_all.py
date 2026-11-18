"""Every 4200A gain-cell retention run on one figure: read current against retention time, offline from saved data.

    python -m measure.plot_gc_ret_all [--data-dir data_k4200] [--sample wd_bobafet_6916] [--die 0_0]
                                      [--out .pptx_figs/wd_bobafet]

Left panel: the PMU runs (measure.gc_pmu_ret, GC_PMU_ret_*.csv), I_read_A per read. Right panel: the SMU runs
(measure.gc_sweep retention, GC_retention_*.csv, Phase 'retention'), read current = -RBL_Current, as gc_pmu_ret.
|I_read| against retention time on log axes (t = 0 is off a log axis and is left out). Colour = channel length
from the file name, solid = stored 1, dashed = stored 0. Writes one PNG; nothing under the data folders is written.
"""
import argparse
import re
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D

from measure.plot_idvg_all import PALETTE

WL_RE = re.compile(r'_W(\d+(?:\.\d+)?)_L(\d+(?:\.\d+)?)\.csv$')
STATE_STYLE = {1: '-', 0: '--'}


def collect(die_dir: Path) -> pd.DataFrame:
    """Long table: kind (PMU/SMU), device, W, L, file, State, t_s, I_A."""
    parts = []
    for kind, pattern in (('PMU', 'GC_PMU_ret_2*.csv'), ('SMU', 'GC_retention_*.csv')):
        for f in sorted(Path(die_dir).glob(f'*/{pattern}')):
            m = WL_RE.search(f.name)
            d = pd.read_csv(f)
            if kind == 'PMU':
                d = pd.DataFrame({'State': d['State'], 't_s': d['t_ret_s'], 'I_A': d['I_read_A']})
            else:
                d = d[d['Phase'] == 'retention']
                d = pd.DataFrame({'State': d['State'], 't_s': d['Time_s'], 'I_A': -d['RBL_Current']})
            d['kind'], d['device'], d['file'] = kind, f.parent.name, f.name
            d['W'], d['L'] = float(m[1]), float(m[2])
            parts.append(d)
    return pd.concat(parts, ignore_index=True)


def plot(df: pd.DataFrame, out: Path, title: str, font_scale: float = 2.0, alpha: float = 0.5) -> Path:
    lengths = sorted(df['L'].unique())
    colours = dict(zip(sorted(set(lengths) | {1.0, 2.0, 4.0, 10.0}), PALETTE))
    kinds = [k for k in ('PMU', 'SMU') if k in set(df['kind'])]
    with plt.rc_context({'font.size': 10 * font_scale}):
        fig, axes = plt.subplots(1, len(kinds), figsize=(16, 9), sharey=True, squeeze=False)
        for ax, kind in zip(axes[0], kinds):
            sub = df[(df['kind'] == kind) & (df['t_s'] > 0)]
            for (_, state), c in sub.groupby(['file', 'State'], sort=False):
                ax.plot(c['t_s'], c['I_A'].abs(), color=colours[c['L'].iloc[0]], linestyle=STATE_STYLE[state],
                        linewidth=2, alpha=alpha, marker='o' if kind == 'PMU' else None, markersize=4)
            ax.set_xscale('log')
            ax.set_yscale('log')
            ax.set_xlabel('retention time (s)')
            n = sub['file'].nunique()
            ax.set_title(f"{kind} read ({n} runs)")
            ax.grid(True, which='major', color='0.85', linewidth=0.6)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)
        axes[0][0].set_ylabel(r'$|I_{read}|$ (A)')
        handles = [Line2D([], [], color=colours[L], linewidth=3, label=f'L = {L:g} µm') for L in lengths]
        handles += [Line2D([], [], color='0.3', linestyle=STATE_STYLE[s], linewidth=2, label=f'stored {s}')
                    for s in (1, 0)]
        fig.suptitle(title)
        fig.subplots_adjust(left=0.1, right=0.98, top=0.86, bottom=0.25, wspace=0.05)
        fig.legend(handles=handles, loc='lower center', ncol=len(handles), frameon=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=300)
        plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--data-dir', default='data_k4200')
    ap.add_argument('--sample', default='wd_bobafet_6916')
    ap.add_argument('--die', default='0_0')
    ap.add_argument('--font-scale', type=float, default=2.0)
    ap.add_argument('--out', default='.pptx_figs')
    a = ap.parse_args(argv)
    out = Path(a.out)
    if any(out.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    df = collect(Path(a.data_dir) / a.sample / a.die)
    print(df.groupby(['kind', 'L'])['file'].nunique().to_string())
    path = plot(df, out / f'{a.sample}_{a.die}_GC_retention_all.png', f'{a.sample} {a.die}: gain-cell retention, 4200A',
                a.font_scale)
    print(path)
    return path


if __name__ == '__main__':
    main()
