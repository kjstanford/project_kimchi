"""Every PMU stress-staircase run's dVt on one figure: stress and 0 V recovery, offline from saved data.

    python -m measure.plot_stress_all [--data-dir data_k4200_stress] [--sample wd_bobafet_6916] [--die 0_0]
                                      [--out .pptx_figs/wd_bobafet]

For each PMU_stair_meta_*.json with a Vt table, reads that run's own PMU_stair_vt_*.csv (the run's Vt definition,
vt_id_per_wl * W/L with the vt_id_min_a floor); if only re-analyses are left, the one at that same current. dVt
(source side, relative to the run's own reference staircase) is drawn against cumulative stress time (left panel)
and recovery time at 0 V (right panel), both log scales and one shared dVt axis.
Colour = channel length, line style = stress mode (DC, AC by period). Runs on a device that was stressed before
(a device with several runs, or a WARNING_*already_stressed file) are drawn grey: they are not fresh-device data.
Writes one PNG; nothing under the data folders is written.
"""
import argparse
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

from measure.plot_idvg_all import PALETTE

STYLES = ('-', '--', ':', '-.')
GREY = '#9a9a9a'


def mode_label(p: dict) -> str:
    if p.get('stress_mode', 'dc') != 'ac':
        return f"DC {p['vstress']:g} V"
    per = p['ac_period_s']
    per_s = f"{per * 1e6:g} µs" if per < 1e-3 else f"{per * 1e3:g} ms"
    return f"AC {p['vstress']:g} V, {per_s} period"


def vt_file(meta: Path, p: dict, W: float, L: float) -> Path | None:
    stamp_desc = meta.name.removeprefix('PMU_stair_meta_').removesuffix('.json')
    plain = meta.with_name(f'PMU_stair_vt_{stamp_desc}.csv')
    if plain.is_file():
        return plain
    target = max(p.get('vt_id_per_wl', 1e-8) * W / L, p.get('vt_id_min_a', 0.0))
    for f in meta.parent.glob(f'PMU_stair_vt_{stamp_desc}_reanalysis_Iref*.csv'):
        m = re.search(r'_Iref([0-9.e+-]+)\.csv$', f.name)
        if m and np.isclose(float(m[1]), target, rtol=0.02):
            return f
    return None


def collect(die_dir: Path) -> list[dict]:
    """One dict per run with data: device, W, L, mode, started, repeat flag, and its Vt table."""
    runs = []
    for meta in sorted(Path(die_dir).glob('*/PMU_stair_meta_*.json')):
        m = json.loads(meta.read_text())
        p, W, L = m['params'], m['W_um'], m['L_um']
        f = vt_file(meta, p, W, L)
        if f is None:
            continue
        runs.append({'device': meta.parent.name, 'W': W, 'L': L, 'mode': mode_label(p), 'started': m.get('started'),
                     'vt': pd.read_csv(f), 'file': f.name})
    per_dev = pd.Series([r['device'] for r in runs]).value_counts()
    for r in runs:
        warned = any((Path(die_dir) / r['device']).glob('WARNING_*already_stressed*'))
        r['repeat'] = bool(warned or per_dev[r['device']] > 1)
    return runs


def plot(runs: list[dict], out: Path, title: str, font_scale: float = 2.0, alpha: float = 0.8) -> Path:
    if not runs:
        raise ValueError('no stress runs with a Vt table')
    lengths = sorted({r['L'] for r in runs})
    colours = dict(zip(sorted(set(lengths) | {1.0, 2.0, 4.0, 10.0}), PALETTE))
    modes = list(dict.fromkeys(r['mode'] for r in sorted(runs, key=lambda r: r['mode'] != 'DC 3 V')))
    styles = dict(zip(modes, STYLES))
    with plt.rc_context({'font.size': 10 * font_scale}):
        fig, (axs, axr) = plt.subplots(1, 2, figsize=(16, 8), sharey=True, gridspec_kw={'wspace': 0.04})
        for r in sorted(runs, key=lambda r: not r['repeat']):   # grey first, fresh devices on top
            v = r['vt']
            c, lw, a = (GREY, 1.5, 0.6) if r['repeat'] else (colours[r['L']], 2.5, alpha)
            s = v[(v['phase'] == 'read') & (v['t_stress_s'] > 0)]
            rc = v[v['phase'] == 'recovery']
            kw = dict(color=c, linestyle=styles[r['mode']], linewidth=lw, alpha=a, marker='o', markersize=4)
            axs.plot(s['t_stress_s'], s['dVt_source_V'] * 1e3, **kw)
            axr.plot(rc['t_rec_s'], rc['dVt_source_V'] * 1e3, **kw)
        for ax, lab in ((axs, 'cumulative stress time (s)'), (axr, 'recovery time at 0 V (s)')):
            ax.set_xscale('log')
            ax.set_xlabel(lab)
            ax.axhline(0, color='0.5', linewidth=1)
            ax.grid(True, which='major', color='0.85', linewidth=0.6)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)
        axs.set_ylabel(r'$\Delta V_T$ (mV)')
        handles = []
        for L in lengths:
            for mode in modes:
                n = sum(1 for r in runs if not r['repeat'] and r['L'] == L and r['mode'] == mode)
                if n:
                    handles.append(Line2D([], [], color=colours[L], linestyle=styles[mode], linewidth=2.5,
                                          label=f'L = {L:g} µm, {mode} ({n})'))
        n_rep = sum(r['repeat'] for r in runs)
        if n_rep:
            handles.append(Line2D([], [], color=GREY, linewidth=1.5,
                                  label=f'pre-stressed devices ({n_rep} runs)'))
        fig.suptitle(title)
        rows = (len(handles) + 1) // 2
        fig.set_size_inches(16, 8 + 0.55 * rows)
        fig.subplots_adjust(left=0.1, right=0.98, top=0.9, bottom=0.14 + 0.065 * rows, wspace=0.04)
        fig.legend(handles=handles, loc='lower center', ncol=2, frameon=False, bbox_to_anchor=(0.5, 0.0))
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=300)
        plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--data-dir', default='data_k4200_stress')
    ap.add_argument('--sample', default='wd_bobafet_6916')
    ap.add_argument('--die', default='0_0')
    ap.add_argument('--font-scale', type=float, default=2.0)
    ap.add_argument('--include-repeats', action='store_true', help='also draw runs on pre-stressed devices, grey')
    ap.add_argument('--out', default='.pptx_figs')
    a = ap.parse_args(argv)
    out = Path(a.out)
    if any(out.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    runs = collect(Path(a.data_dir) / a.sample / a.die)
    for r in runs:
        print(f"{r['device']} W{r['W']:g}/L{r['L']:g} {r['mode']:<24} {'REPEAT' if r['repeat'] else 'fresh '} "
              f"{r['started']}  {r['file']}")
    if not a.include_repeats:
        runs = [r for r in runs if not r['repeat']]
    path = plot(runs, out / f'{a.sample}_{a.die}_stress_dVt_all.png', f'{a.sample} {a.die}: PMU stress, dVt',
                a.font_scale)
    print(path)
    return path


if __name__ == '__main__':
    main()
