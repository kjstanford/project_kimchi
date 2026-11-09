"""Every device's Id-Vg on one figure, |Id|*L/W, one colour per channel length, offline from saved data.

    python -m measure.plot_idvg_all --sample wd_bobafet_6802 [--die 0_0] [--vd 0.05] [--out .pptx_figs/wd_bobafet]

For each device folder under data/SAMPLE/DIE, takes the newest IdVg_main_*.csv (so a --repeat rerun replaces the
first try; initial scans are not used, so screened-out devices are absent), keeps the rows at Drain_Voltage = --vd
(every cycle, both sweep directions), and draws |Id| * L/W against Vg on a log axis. W and L (um) come from the
file name's _W{w}_L{l} suffix. Lines are translucent so the density shows; the colour encodes L only, whatever
the W. Writes one PNG to --out; nothing under data/ is written.
"""
import argparse
import re
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

WL_RE = re.compile(r'_W(\d+(?:\.\d+)?)_L(\d+(?:\.\d+)?)\.csv$')
# categorical slots 1-4 of the dataviz reference palette (blue, orange, aqua, yellow), assigned by sorted L
PALETTE = ('#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948')


def newest_main_files(die_dir: Path) -> list[tuple[str, Path, float, float]]:
    """(device, newest IdVg_main file, W, L) for each device folder that has a main sweep."""
    out = []
    for dev in sorted(d for d in Path(die_dir).iterdir() if d.is_dir()):
        files = sorted(dev.glob('IdVg_main_*.csv'))   # the name starts with the timestamp, so sorting is by time
        if not files:
            continue
        m = WL_RE.search(files[-1].name)
        if not m:
            raise ValueError(f"no _W.._L.. in file name: {files[-1]}")
        out.append((dev.name, files[-1], float(m[1]), float(m[2])))
    return out


def load_curves(die_dir: Path, vd: float = 0.05) -> pd.DataFrame:
    """Rows at Drain_Voltage = vd from each device's newest main sweep, with device, W, L and Id_LW = |Id|*L/W."""
    parts = []
    for dev, path, W, L in newest_main_files(die_dir):
        d = pd.read_csv(path)
        d = d[np.isclose(d['Drain_Voltage'], vd)].copy()
        if d.empty:
            continue
        d['device'], d['W'], d['L'] = dev, W, L
        d['Id_LW'] = d['Drain_Current'].abs() * L / W
        parts.append(d)
    cols = ['device', 'W', 'L', 'Cycle', 'Drain_Voltage', 'Gate_Voltage', 'Id_LW']
    return pd.concat(parts, ignore_index=True)[cols] if parts else pd.DataFrame(columns=cols)


def colours_by_L(L: pd.Series) -> dict[float, str]:
    """A fixed colour per channel length, in ascending L (more than 8 lengths is an error, not a generated hue)."""
    lengths = sorted(set(L))
    if len(lengths) > len(PALETTE):
        raise ValueError(f"{len(lengths)} channel lengths; the palette has {len(PALETTE)} colours")
    return dict(zip(lengths, PALETTE))


def plot(curves: pd.DataFrame, out: Path, title: str, alpha: float = 0.15) -> Path:
    """Draws one translucent line per device and cycle, coloured by L, and saves the figure to out."""
    if curves.empty:
        raise ValueError("no IdVg_main rows to plot")
    colours = colours_by_L(curves['L'])
    fig, ax = plt.subplots(figsize=(8, 6))
    handles = []
    for L, colour in colours.items():
        sub = curves[curves['L'] == L]
        for _, c in sub.groupby(['device', 'Cycle'], sort=False):
            y = c['Id_LW'].to_numpy()
            ax.semilogy(c['Gate_Voltage'].to_numpy(), np.where(y > 0, y, np.nan), color=colour, alpha=alpha,
                        linewidth=1)
        handles.append(Line2D([], [], color=colour, linewidth=2,
                              label=f"L = {L:g} µm ({sub['device'].nunique()} devices)"))
    ax.set_xlabel('$V_G$ (V)')
    ax.set_ylabel('$|I_D| \\cdot L/W$ (A)')
    ax.set_title(title)
    ax.grid(True, which='major', color='0.85', linewidth=0.6)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    ax.legend(handles=handles, frameon=False, loc='lower right')
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=300)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sample', required=True, help='folder under --data-dir, e.g. wd_bobafet_6802')
    ap.add_argument('--die', default='0_0')
    ap.add_argument('--data-dir', default='data')
    ap.add_argument('--vd', type=float, default=0.05, help='drain voltage of the curves to draw (V)')
    ap.add_argument('--alpha', type=float, default=0.15, help='line opacity, 0-1')
    ap.add_argument('--out', default='.pptx_figs', help='output folder (never under data/)')
    a = ap.parse_args(argv)
    if not 0 < a.alpha <= 1:
        ap.error('--alpha must be in (0, 1]')
    die_dir = Path(a.data_dir) / a.sample / a.die
    if not die_dir.is_dir():
        ap.error(f'no such folder: {die_dir}')
    out_dir = Path(a.out)
    if any(out_dir.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    curves = load_curves(die_dir, a.vd)
    n = curves['device'].nunique()
    title = f"{a.sample} {a.die}: newest main sweep per device, $V_D$ = {a.vd:g} V, {n} devices"
    path = plot(curves, out_dir / f"{a.sample}_{a.die}_IdVg_main_all_Vd{a.vd:g}.png", title, a.alpha)
    print(f"{n} devices -> {path}")
    return path


if __name__ == '__main__':
    main()
