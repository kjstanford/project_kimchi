"""Vt roll-off and contact resistance across the JK anneal series (jimin SDC noTLM layout), offline from saved data.

    python -m measure.anneal_series [--out .ignore/plots/anneal_series] [--vov 1.0 1.5]

Reads every die summary of the samples in SAMPLES (newest row per device wins, so --repeat remeasures replace the
first try) and, for each measured device, the cycle-1 forward branch of its IdVg_main file (Vd = 50 mV):
- Vt: Vg at Id = 10 nA * W/L (the run screen's definition, re-extracted from the main sweep)
- R*W = Vd / Id * W at each gate overdrive Vg - Vt in --vov (only when Vt + Vov is inside the sweep)
Per sample, die and contact length Lc, the per-L medians of R*W are fitted with R*W = Rsh * L + 2 Rc*W (TLM over
channel length); Rc*W is half the intercept. Writes devices.csv, rc_fits.csv and the figures to --out; nothing
under data/ is written.
"""
import argparse
import glob
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from measure.idvg_pipeline import Vt_at_constant_Id, get_sweeps

# data folder -> plot label, in anneal order (folder names predate the sample names)
SAMPLES = {'unknown': 'no anneal', 'JK_annealed_300C': '300 °C', 'JK_annealed_350C': '350 °C',
           'unknown_2': '400 °C'}
DIE_ALIAS = {('unknown', 'unknown'): 'bottom_right'}   # data/unknown/unknown/SUBCHIP.txt
DIES = ['top_left', 'top_right', 'bottom_right', 'bottom_left']
VT_ID_PER_WL = 1e-8
MIN_PER_L = 3            # devices needed for a per-L median in the TLM fit
ZOOM_L_UM = 0.3          # x limit of the short-L TLM panels
# no anneal = muted gray, then the anneal temperatures as an ordinal blue ramp (dataviz reference palette)
COLORS = {'no anneal': '#898781', '300 °C': '#86b6ef', '350 °C': '#2a78d6', '400 °C': '#104281'}
MARKERS = {'no anneal': 'o', '300 °C': 's', '350 °C': '^', '400 °C': 'D'}
DIE_COLORS = dict(zip(DIES, ('#2a78d6', '#eb6834', '#1baf7a', '#eda100')))   # categorical slots 1-4
INK, MUTED, GRID = '#0b0b0b', '#52514e', '#e1e0d9'


def load_summaries(data_dir):
    rows = []
    for folder in SAMPLES:
        for f in glob.glob(os.path.join(data_dir, folder, '*', 'summary_*.csv')):
            if os.path.getsize(f) == 0:
                continue
            d = pd.read_csv(f)
            if d.empty:
                continue
            d['folder'] = folder
            rows.append(d)
    s = pd.concat(rows, ignore_index=True)
    s['die'] = [DIE_ALIAS.get((f, d), d) for f, d in zip(s.folder, s.die_idx)]
    s = s.sort_values('time').drop_duplicates(['folder', 'die', 'Lc_nm', 'key'], keep='last')
    return s[s.status == 'measured'].copy()


def device_params(path, W, L, vov_list):
    d = pd.read_csv(path)
    c1 = d[d.Cycle == d.Cycle.min()]
    Vg = get_sweeps(c1.Gate_Voltage.to_numpy())['f']
    Id = np.abs(get_sweeps(c1.Drain_Current.to_numpy())['f'])
    Vd = float(c1.Drain_Voltage.iloc[0])
    out = {'Vt_main_V': np.nan, 'Id_max_main_A': float(np.nanmax(Id))}
    try:
        out['Vt_main_V'] = Vt_at_constant_Id(Vg, Id, Id_target=VT_ID_PER_WL * W / L)
    except ValueError:
        pass
    for vov in vov_list:
        Vg_at = out['Vt_main_V'] + vov
        ok = np.isfinite(Vg_at) and Vg.min() <= Vg_at <= Vg.max()
        I = float(np.interp(Vg_at, Vg, Id)) if ok else np.nan
        out[f'RW_kohm_um_vov{vov:g}'] = abs(Vd) / I * W / 1e3 if ok and I > 0 else np.nan
    return out


def resolve(path, data_dir):
    p = os.path.normpath(str(path).replace('\\', '/'))
    if os.path.exists(p):
        return p
    i = p.replace('\\', '/').find('data/')
    return os.path.normpath(os.path.join(data_dir, p.replace('\\', '/')[i + 5:])) if i >= 0 else p


def l_medians(g, col):
    """Per-L median of col, only for L with at least MIN_PER_L devices."""
    gb = g.groupby('L_um')[col]
    m = gb.median()
    return m[gb.count() >= MIN_PER_L]


def tlm_fit(g, col):
    """Line through the per-L medians, weighted by 1/R (relative residuals): the spread grows with R, and an
    unweighted fit lets the few long-L points set the intercept."""
    m = l_medians(g, col)
    if len(m) < 4:
        return None
    (slope, icpt), cov = np.polyfit(m.index.to_numpy(), m.to_numpy(), 1, w=1 / m.to_numpy(), cov=True)
    return {'RcW_kohm_um': icpt / 2, 'RcW_err': np.sqrt(cov[1, 1]) / 2, 'Rsh_kohm_sq': slope,
            'n_L': len(m), 'n_dev': int(g[col].notna().sum())}


def style(ax):
    ax.grid(True, which='major', color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for sp in ('top', 'right'):
        ax.spines[sp].set_visible(False)
    for sp in ('left', 'bottom'):
        ax.spines[sp].set_color('#c3c2b7')
    ax.tick_params(colors=MUTED, labelcolor=MUTED)


def log_L_axis(ax, Ls):
    ax.set_xscale('log')
    ax.set_xticks(Ls)
    ax.set_xticklabels([f'{v:g}' for v in Ls], fontsize=8)
    ax.minorticks_off()
    ax.set_xlabel('Channel length L (µm)', color=INK)


def plot_rolloff(dev, out):
    Ls = sorted(dev.L_um.unique())
    fig, ax = plt.subplots(figsize=(7.5, 5))
    for lab in SAMPLES.values():
        g = dev[(dev.label == lab) & (dev.Lc_nm == 500)].groupby('L_um').Vt_main_V
        if g.ngroups == 0:
            continue
        med, q1, q3, n = g.median(), g.quantile(0.25), g.quantile(0.75), g.size()
        ax.fill_between(med.index, q1, q3, color=COLORS[lab], alpha=0.15, lw=0)
        ax.plot(med.index, med, color=COLORS[lab], lw=2, marker=MARKERS[lab], ms=7, mec='#fcfcfb', mew=1.5,
                label=f'{lab}  ({n.sum()} devices)')
    log_L_axis(ax, Ls)
    ax.set_ylabel('Vt at 10 nA·W/L, Vd = 50 mV (V)', color=INK)
    ax.set_title('Vt roll-off, Lc = 500 nm, all dies (median, band = IQR)', color=INK, fontsize=11, loc='left')
    style(ax)
    ax.legend(frameon=False, fontsize=9, title='Anneal', title_fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(out, 'vt_rolloff.png'), dpi=150)
    plt.close(fig)


def plot_rolloff_by_die(dev, out):
    Ls = sorted(dev.L_um.unique())
    labs = [l for l in SAMPLES.values() if (dev.label == l).any()]
    fig, axs = plt.subplots(1, len(labs), figsize=(4 * len(labs), 4.2), sharey=True)
    for ax, lab in zip(np.atleast_1d(axs), labs):
        for die in DIES:
            g = dev[(dev.label == lab) & (dev.die == die) & (dev.Lc_nm == 500)].groupby('L_um').Vt_main_V
            if g.ngroups == 0:
                continue
            med = g.median()
            ax.plot(med.index, med, color=DIE_COLORS[die], lw=2, marker='o', ms=5, mec='#fcfcfb', mew=1,
                    label=f'{die.replace("_", " ")} ({g.size().sum()})')
        log_L_axis(ax, [v for v in Ls if v in (0.04, 0.1, 0.2, 0.5, 1, 2)])
        ax.set_title(lab, color=INK, fontsize=11, loc='left')
        style(ax)
        ax.legend(frameon=False, fontsize=8)
    np.atleast_1d(axs)[0].set_ylabel('Vt at 10 nA·W/L (V), median per L', color=INK)
    fig.suptitle('Vt roll-off by die, Lc = 500 nm', color=INK, fontsize=12, x=0.01, ha='left')
    fig.tight_layout()
    fig.savefig(os.path.join(out, 'vt_rolloff_by_die.png'), dpi=150)
    plt.close(fig)


def plot_tlm(dev, fits, vov, out):
    col = f'RW_kohm_um_vov{vov:g}'
    labs = [l for l in SAMPLES.values() if (dev.label == l).any()]
    n = len(labs)
    fig = plt.figure(figsize=(4 * (n + 1), 8))
    gs = fig.add_gridspec(2, n + 1)
    top = [fig.add_subplot(gs[0, i]) for i in range(n)]
    bot = [fig.add_subplot(gs[1, i]) for i in range(n)]
    for row, axs in (('full', top), ('zoom', bot)):
        ymax = 0
        for ax, lab in zip(axs, labs):
            for die in DIES:
                g = dev[(dev.label == lab) & (dev.die == die) & (dev.Lc_nm == 500)]
                m = l_medians(g, col).dropna()
                if m.empty:
                    continue
                if row == 'zoom':
                    g, m = g[g.L_um <= ZOOM_L_UM], m[m.index <= ZOOM_L_UM]
                ax.scatter(g.L_um, g[col], s=8, color=DIE_COLORS[die], alpha=0.25, lw=0)
                ax.plot(m.index, m, 'o', color=DIE_COLORS[die], ms=6, mec='#fcfcfb', mew=1, zorder=3)
                f = fits[(fits.label == lab) & (fits.die == die) & (fits.Lc_nm == 500) & (fits.vov == vov)]
                lbl = die.replace('_', ' ')
                if len(f):
                    f = f.iloc[0]
                    x = np.array([0, ZOOM_L_UM if row == 'zoom' else m.index.max()])
                    ax.plot(x, f.Rsh_kohm_sq * x + 2 * f.RcW_kohm_um, color=DIE_COLORS[die], lw=1.5)
                    lbl += f': Rc·W {f.RcW_kohm_um:.2g} ± {f.RcW_err:.1g}'
                ax.plot([], [], 'o-', color=DIE_COLORS[die], label=lbl)
                if len(m):
                    ymax = max(ymax, m.max())
            style(ax)
            ax.set_xlabel('Channel length L (µm)', color=INK)
            if row == 'full':
                ax.set_title(lab, color=INK, fontsize=11, loc='left')
                ax.legend(frameon=False, fontsize=7.5, loc='upper left', title='Rc·W in kΩ·µm', title_fontsize=7.5)
        for ax in axs:
            ax.set_xlim(0, ZOOM_L_UM * 1.03 if row == 'zoom' else None)
            ax.set_ylim(0, ymax * 1.15)
    top[0].set_ylabel(f'R·W at Vg − Vt = {vov:g} V (kΩ·µm)', color=INK)
    bot[0].set_ylabel(f'R·W, L ≤ {ZOOM_L_UM:g} µm (kΩ·µm)', color=INK)

    # Rc*W per sample: one point per die (crosses: the other contact lengths)
    ax = fig.add_subplot(gs[:, n])
    f = fits[fits.vov == vov]
    xs = {lab: i for i, lab in enumerate(labs)}
    for (lab, Lc), g in f.groupby(['label', 'Lc_nm']):
        x = xs[lab] + {500: 0.0, 100: 0.2, 80: 0.3, 60: 0.4}.get(Lc, 0) + np.linspace(-0.07, 0.07, len(g))
        ax.errorbar(x, g.RcW_kohm_um, yerr=g.RcW_err, fmt='none', ecolor=MUTED, elinewidth=0.8)
        if Lc == 500:
            ax.scatter(x, g.RcW_kohm_um, s=45, color=COLORS[lab], marker=MARKERS[lab], edgecolors='#fcfcfb',
                       lw=1, zorder=3)
        else:
            ax.scatter(x, g.RcW_kohm_um, s=40, color=COLORS[lab], marker='x', lw=1.5, zorder=3)
            ax.annotate(f'Lc {Lc}', (x.max(), g.RcW_kohm_um.max()), xytext=(4, {60: 2, 80: 4, 100: -9}.get(Lc, 2)), textcoords='offset points',
                        fontsize=7, color=MUTED)
    ax.set_xticks(range(n))
    ax.set_xticklabels(labs, fontsize=9)
    ax.set_xlim(-0.5, n - 0.3)
    ax.axhline(0, color='#c3c2b7', lw=0.8)
    ax.set_ylabel('Rc·W (kΩ·µm), one point per die', color=INK)
    ax.set_title('Rc·W from the TLM intercept\n(markers: Lc 500 nm, ×: shorter Lc)', color=INK, fontsize=10,
                 loc='left')
    style(ax)
    fig.suptitle(f'TLM over channel length at Vg − Vt = {vov:g} V, Vd = 50 mV, Lc = 500 nm. Dots: per-L medians '
                 f'(≥ {MIN_PER_L} devices), faint: devices, lines: 1/R-weighted fit', color=INK, fontsize=12,
                 x=0.01, ha='left')
    fig.tight_layout()
    fig.savefig(os.path.join(out, f'tlm_rc_vov{vov:g}.png'), dpi=150)
    plt.close(fig)


def plot_picked(dev, fits, picks, vov, out, L_max=0.5):
    """One die per sample: dVt = Vt - Vt(L = L_max) vs L, and the short-L TLM with that die's Rc*W."""
    col = f'RW_kohm_um_vov{vov:g}'
    fig, (a1, a2, a3) = plt.subplots(1, 3, figsize=(15, 4.8), gridspec_kw={'width_ratios': [1, 1, 0.42]})
    ymax, rc = 0, []
    for folder, die in picks:
        lab = SAMPLES[folder]
        g = dev[(dev.folder == folder) & (dev.die == die) & (dev.Lc_nm == 500) & (dev.L_um <= L_max + 1e-9)]
        if g.empty:
            print(f'no devices for {folder} {die}')
            continue
        name = f'{lab}, {die.replace("_", " ")}'
        c, mk = COLORS[lab], MARKERS[lab]

        ref = g[np.isclose(g.L_um, L_max)].Vt_main_V.median()
        dVt = g.Vt_main_V - ref
        q = dVt.groupby(g.L_um).quantile([0.25, 0.5, 0.75]).unstack()
        a1.scatter(g.L_um, dVt, s=10, color=c, alpha=0.3, lw=0)
        a1.errorbar(q.index, q[0.5], yerr=[q[0.5] - q[0.25], q[0.75] - q[0.5]], color=c, lw=2, marker=mk, ms=7,
                    mec='#fcfcfb', mew=1.5, elinewidth=1, capsize=0,
                    label=f'{name}  (Vt at {L_max:g} µm = {ref:+.2f} V, {len(g)} devices)')

        m = l_medians(g, col).dropna()
        q = g.groupby('L_um')[col].quantile([0.25, 0.75]).unstack().loc[m.index]
        a2.scatter(g.L_um, g[col], s=10, color=c, alpha=0.3, lw=0)
        ymax = max(ymax, q[0.75].max())
        a2.errorbar(m.index, m, yerr=[m - q[0.25], q[0.75] - m], fmt=mk, color=c, ms=7, mec='#fcfcfb', mew=1.5,
                    elinewidth=1, capsize=0, zorder=3)
        f = fits[(fits.folder == folder) & (fits.die == die) & (fits.Lc_nm == 500) & (fits.vov == vov)]
        lbl = name
        if len(f):
            f = f.iloc[0]
            x = np.array([0, L_max])
            a2.plot(x, f.Rsh_kohm_sq * x + 2 * f.RcW_kohm_um, color=c, lw=1.5)
            lbl += f':  Rc·W = {f.RcW_kohm_um:.2f} ± {f.RcW_err:.2f} kΩ·µm'
            rc.append((lab, die, f.RcW_kohm_um, f.RcW_err, c, mk))
        a2.plot([], [], marker=mk, color=c, lw=1.5, label=lbl)

    Ls = sorted(v for v in dev.L_um.unique() if v <= L_max + 1e-9)
    log_L_axis(a1, Ls)
    a1.axhline(0, color='#c3c2b7', lw=0.8)
    a1.set_ylabel(f'ΔVt = Vt − Vt(L = {L_max:g} µm) (V)', color=INK)
    a1.set_title('Vt roll-off (median, bars = IQR, faint: devices)', color=INK, fontsize=11, loc='left')
    a1.legend(frameon=False, fontsize=8, loc='lower right')
    style(a1)

    a2.set_xlim(0, L_max * 1.03)
    a2.set_ylim(0, ymax * 1.15)
    a2.set_xlabel('Channel length L (µm)', color=INK)
    a2.set_ylabel(f'R·W at Vg − Vt = {vov:g} V (kΩ·µm)', color=INK)
    a2.set_title('TLM: Rc·W = intercept / 2 (fit over all L up to 2 µm)', color=INK, fontsize=11, loc='left')
    a2.legend(frameon=False, fontsize=8, loc='upper left')
    style(a2)
    for i, (lab, die, v, e, c, mk) in enumerate(rc):
        a3.errorbar(i, v, yerr=e, fmt=mk, color=c, ms=9, mec='#fcfcfb', mew=1.5, ecolor=MUTED, elinewidth=1)
        a3.annotate(f'{v:.2f}', (i, v), xytext=(8, 0), textcoords='offset points', va='center', fontsize=9,
                    color=INK)
    a3.set_xticks(range(len(rc)))
    a3.set_xticklabels([lab + '\n' + die.replace('_', ' ') for lab, die, *_ in rc], fontsize=8)
    a3.set_xlim(-0.5, len(rc) - 0.3)
    a3.set_ylim(0, None)
    a3.set_ylabel(f'Rc·W at Vg − Vt = {vov:g} V (kΩ·µm)', color=INK)
    a3.set_title('Rc·W (± fit s.e.)', color=INK, fontsize=11, loc='left')
    style(a3)
    fig.suptitle(f'Lc = 500 nm, Vd = 50 mV, Vt at 10 nA·W/L', color=MUTED, fontsize=9, x=0.01, ha='left')
    fig.tight_layout()
    path = os.path.join(out, f'picked_dvt_rc_vov{vov:g}.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', default='data')
    ap.add_argument('--out', default=os.path.join('.ignore', 'plots', 'anneal_series'))
    ap.add_argument('--vov', type=float, nargs='+', default=[1.0, 1.5])
    ap.add_argument('--pick', nargs='+', metavar='FOLDER:DIE',
                    default=['JK_annealed_300C:top_right', 'JK_annealed_350C:bottom_left', 'unknown_2:bottom_left'],
                    help='one die per sample for the dVt / Rc figure')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    s = load_summaries(a.data)
    recs = []
    for r in s.itertuples():
        p = resolve(r.main_file, a.data)
        if not os.path.exists(p):
            print(f'missing main file: {p}')
            continue
        recs.append({'folder': r.folder, 'label': SAMPLES[r.folder], 'die': r.die, 'Lc_nm': r.Lc_nm, 'key': r.key,
                     'L_um': r.L_um, 'W_um': r.W_um, 'Vt_scan_V': r.Vt_V,
                     **device_params(p, r.W_um, r.L_um, a.vov)})
    dev = pd.DataFrame(recs)
    dev.to_csv(os.path.join(a.out, 'devices.csv'), index=False)

    fits = []
    for vov in a.vov:
        for (folder, lab, die, Lc), g in dev.groupby(['folder', 'label', 'die', 'Lc_nm']):
            f = tlm_fit(g, f'RW_kohm_um_vov{vov:g}')
            if f:
                fits.append({'folder': folder, 'label': lab, 'die': die, 'Lc_nm': Lc, 'vov': vov, **f})
    fits = pd.DataFrame(fits)
    fits.to_csv(os.path.join(a.out, 'rc_fits.csv'), index=False)
    print(dev.groupby(['label', 'die', 'Lc_nm']).size().to_string())
    print(fits.round(3).to_string())

    plot_rolloff(dev, a.out)
    plot_rolloff_by_die(dev, a.out)
    for vov in a.vov:
        plot_tlm(dev, fits, vov, a.out)
        plot_picked(dev, fits, [p.split(':', 1) for p in a.pick], vov, a.out)
    print(f'figures in {a.out}')


if __name__ == '__main__':
    main()
