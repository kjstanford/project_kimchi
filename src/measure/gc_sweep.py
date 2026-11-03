"""2T gain cell, WBL sweep on the 4200A (KXCI): the read FET's transfer curve, as Clarius gc_wbl_sweep.

The write word line WWL holds the write FET on, so the storage node (read gate) follows the swept write bit
line WBL; the read FET sits between RWL (constant, the drain side) and RBL (constant, read here). All four
SMUs source a voltage and measure V and I (Clarius: Limited Auto 1 uA, 0.1 A compliance, Normal speed,
0.01 s sweep delay; IT/DT/HT come from the config's 'k4200' block). The WBL list is a double sweep with the
stop point measured twice, like Clarius (and fet_idvg_k4200.gate_list).

Config block 'gc_sweep' (defaults in DEFAULTS) and smu_map {'wwl', 'wbl', 'rbl', 'rwl'} -> 'smuN'.
Output per cell: GC_WBL_sweep_<stamp>_<descriptor>.csv/.png with WBL_Voltage (programmed), Direction (f/b),
and <T>_Voltage_meas, <T>_Current, <T>_Status for T = WWL, WBL, RBL, RWL (status bits as fet_idvg).
Read current = |RBL_Current|; Vt = V_WBL at read current = vt_current_A (log interpolation, NaN if the branch
never crosses it), forward and backward.

WWL sweep (experiment gc_wwl, block 'gc_wwl', as keithley4200_utils/gc_helpers.wwl_sweep): per state, two
conditioning biases with the outputs left on (WWL high then WWL low, WBL at the opposite data level, RWL/RBL
low, conditioning_s each), then a WWL double sweep low -> high -> low with WBL at the state's data level
(state 1: wbl_high, state 0: wbl_low), RWL high, RBL low. State 1 starts from a stored low and is written high
as WWL opens the write FET (read current rises); state 0 starts high and is discharged (read current falls).
Output per cell: GC_WWL_sweep_<stamp>_<descriptor>.csv/.png, rows for every state and phase (cond_boost,
cond_hold, sweep) with WWL_Voltage (programmed) and the same per-line columns.

Retention (experiment gc_retention, block 'gc_retention', as gc_helpers.gc_retention_test): per state, the same
two conditioning biases but with WBL at the level being written (state 1: wbl_high, state 0: wbl_low), then a
KXCI sampling test with WWL low, WBL at the opposite level (worst case), RWL high, RBL low: nr readings
interval_adj_s apart. Time_s follows that schedule (DO timestamps were unreliable on this firmware); a timing
flag is raised if the sampling took clearly longer. Output: GC_retention_<stamp>_<descriptor>.csv/.png.
"""
import os
import time

import numpy as np
import pandas as pd

from measure.fet_idvg import ST_COMP, ST_OSC, ST_OVERFLOW, ST_INVALID
from measure.fet_idvg_k4200 import _column, gate_list

TERMS = ('WWL', 'WBL', 'RBL', 'RWL')
FILE_PREFIX = 'GC_WBL_sweep_'
DEFAULTS = dict(wwl_V=2.5, rwl_V=1.5, rbl_V=0.0, wbl_start=0.0, wbl_stop=1.5, wbl_step=0.05,
                compliance=0.1, Irange=1e-6, vt_current_A=1e-7, v_max=3.0)
SUMMARY = ('Vt_V', 'Vt_bwd_V', 'hyst_V', 'on_off', 'Ileak_max_A')
WWL_PREFIX = 'GC_WWL_sweep_'
WWL_DEFAULTS = dict(wwl_high=2.5, wwl_low=-0.5, wwl_step=0.1, wbl_high=1.5, wbl_low=0.0, rwl_high=1.5,
                    rwl_low=0.0, rbl_V=0.0, states=[1, 0], conditioning_s=2.0, compliance=0.1, Irange=1e-6,
                    vt_current_A=1e-7, v_max=3.0)
WWL_SUMMARY = ('Vt_V', 'Vt_s0_V', 'I0_s1_A', 'I0_s0_A', 'Iend_s1_A', 'Iend_s0_A', 'Ileak_max_A')
RET_PREFIX = 'GC_retention_'
RET_DEFAULTS = dict(wwl_high=2.5, wwl_low=-0.5, wbl_high=1.5, wbl_low=0.0, rwl_high=1.5, rwl_low=0.0, rbl_V=0.0,
                    states=[1, 0], conditioning_s=2.0, retention_s=300.0, interval_s=1.0, compliance=0.1,
                    Irange=1e-6, v_max=3.0)
RET_SUMMARY = ('Iret_s1_start_A', 'Iret_s1_end_A', 'Iret_s0_start_A', 'Iret_s0_end_A', 'ret_ratio_end',
               'Ileak_max_A')


def params(block):
    unknown = set(block) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"gc_sweep: unknown settings {sorted(unknown)}")
    return {**DEFAULTS, **block}


def check_config(cfg):
    """Raise on a bad setting before anything connects or moves."""
    p = params(cfg.get('gc_sweep', {}))
    ok = p['wbl_step'] > 0 and p['wbl_stop'] > p['wbl_start']
    pts = gate_list(p['wbl_start'], p['wbl_stop'], p['wbl_step']) if ok else []
    if not ok or len(pts) > 4096:
        raise ValueError(f"gc_sweep: WBL {p['wbl_start']} -> {p['wbl_stop']} V in {p['wbl_step']} V steps "
                         f"(needs start < stop, step > 0, <= 4096 points, got {len(pts)})")
    for k in ('wwl_V', 'rwl_V', 'rbl_V', 'wbl_start', 'wbl_stop'):
        if abs(p[k]) > p['v_max']:
            raise ValueError(f"gc_sweep: |{k}| = {abs(p[k])} V exceeds v_max = {p['v_max']} V")
    if not 1e-7 <= p['Irange'] <= 0.1:
        raise ValueError(f"gc_sweep: Irange {p['Irange']} A outside 1e-7..0.1 A (no preamps: 100 nA lowest)")
    if not 0 < p['compliance'] <= 0.1:
        raise ValueError(f"gc_sweep: compliance {p['compliance']} A outside (0, 0.1] A")
    return p


def wbl_sweep(k, chans, p):
    """One WBL double sweep; chans = {'wwl': n, 'wbl': n, 'rbl': n, 'rwl': n}. Outputs off afterwards."""
    v = gate_list(p['wbl_start'], p['wbl_stop'], p['wbl_step'])
    names = {chans[t.lower()]: (f'V{t}', f'I{t}') for t in TERMS}
    consts = {chans['wwl']: p['wwl_V'], chans['rwl']: p['rwl_V'], chans['rbl']: p['rbl_V']}
    try:
        raw = k.smu_sweep(chans['wbl'], v, consts, names, p['compliance'], p['Irange'])
    finally:
        k.outputs_off()
    n = len(v)
    data = {'WBL_Voltage': v, 'Direction': ['f'] * (n // 2) + ['b'] * (n - n // 2)}
    for t in TERMS:
        data[f'{t}_Voltage_meas'] = _column(raw[f'V{t}'], n, f'V{t}')[0]
    for t in TERMS:
        data[f'{t}_Current'], data[f'{t}_Status'] = _column(raw[f'I{t}'], n, f'I{t}')
    return pd.DataFrame(data)


def status_flags(data):
    """'rbl compliance x3; wwl overflow/invalid x1' ('' if clean)."""
    out = []
    for t in TERMS:
        st = data[f'{t}_Status'].fillna(0).astype(int)
        for bit, name in ((ST_COMP, 'compliance'), (ST_OSC, 'oscillation'),
                          (ST_OVERFLOW | ST_INVALID, 'overflow/invalid')):
            c = int(((st & bit) != 0).sum())
            if c:
                out.append(f"{t.lower()} {name} x{c}")
    return '; '.join(out)


def v_at_current(v, i, target):
    """V where |i| crosses target (log-linear, first crossing along the branch); NaN if it never does."""
    v, a = np.asarray(v, float), np.abs(np.asarray(i, float))
    ok = np.isfinite(a) & (a > 0)
    v, la, lt = v[ok], np.log10(a[ok]), np.log10(target)
    for j in range(len(la) - 1):
        if (la[j] - lt) * (la[j + 1] - lt) <= 0 and la[j] != la[j + 1]:
            return float(v[j] + (lt - la[j]) * (v[j + 1] - v[j]) / (la[j + 1] - la[j]))
    return float('nan')


def analyse(data, p):
    f, b = data[data['Direction'] == 'f'], data[data['Direction'] == 'b']
    vt_f = v_at_current(f['WBL_Voltage'], f['RBL_Current'], p['vt_current_A'])
    vt_b = v_at_current(b['WBL_Voltage'][::-1], b['RBL_Current'][::-1], p['vt_current_A'])  # low -> high V
    ir = np.abs(f['RBL_Current'].to_numpy())
    lo, hi = np.nanmin(ir), np.nanmax(ir)
    leak = np.nanmax(np.abs(data[['WWL_Current', 'WBL_Current']].to_numpy()))
    return {'Vt_V': vt_f, 'Vt_bwd_V': vt_b, 'hyst_V': vt_b - vt_f, 'on_off': float(hi / lo) if lo > 0 else np.inf,
            'Ileak_max_A': float(leak), 'Iread_max_A': float(hi)}


def plot(data, p, title):
    import matplotlib.pyplot as plt
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    colors = dict(RBL='tab:blue', RWL='tab:orange', WBL='tab:green', WWL='tab:red')
    for t in TERMS:
        for d, ls in (('f', '-'), ('b', '--')):
            s = data[data['Direction'] == d]
            ax.semilogy(s['WBL_Voltage'], np.abs(s[f'{t}_Current']), ls, color=colors[t],
                        label=f'|I {t}|' if d == 'f' else None)
    for d, ls in (('f', '-'), ('b', '--')):
        s = data[data['Direction'] == d]
        ax2.plot(s['WBL_Voltage'], np.abs(s['RBL_Current']) * 1e6, ls, color=colors['RBL'],
                 label='forward' if d == 'f' else 'backward')
    ax.axhline(p['vt_current_A'], color='0.6', lw=0.8)
    ax.set_xlabel('V WBL (V)'); ax.set_ylabel('|I| (A)'); ax.grid(True, which='both', alpha=0.3); ax.legend()
    ax2.set_xlabel('V WBL (V)'); ax2.set_ylabel('|I RBL| (uA)'); ax2.grid(True, alpha=0.3); ax2.legend()
    fig.suptitle(f"{title}\nWWL {p['wwl_V']:g} V, RWL {p['rwl_V']:g} V, RBL {p['rbl_V']:g} V; "
                 f"WBL {p['wbl_start']:g} -> {p['wbl_stop']:g} V, {p['wbl_step']:g} V steps, dual")
    fig.tight_layout()
    return fig


def measure_cell(k, chans, cfg, save_dir, descriptor, save=True, show=False, log=print):
    """WBL sweep on the cell under the probes; returns the run_map summary fields."""
    p = check_config(cfg)
    data = wbl_sweep(k, chans, p)
    res = analyse(data, p)
    flags = status_flags(data)
    path = None
    if save:
        import matplotlib.pyplot as plt
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{FILE_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}_{descriptor}.csv")
        data.to_csv(path, index=False)
        fig = plot(data, p, descriptor)
        fig.savefig(path.replace('.csv', '.png'))
        if show:
            plt.show(block=False); plt.pause(1)
        plt.close(fig)
        log(f"Data saved to: {path}")
    log(f"Vt @ |I RBL| = {p['vt_current_A']:g} A: fwd {res['Vt_V']:.3f} V, bwd {res['Vt_bwd_V']:.3f} V "
        f"(hyst {res['hyst_V'] * 1e3:.0f} mV) | max |I RBL| {res['Iread_max_A']:.3e} | on/off {res['on_off']:.3g} | "
        f"max |I WWL|,|I WBL| {res['Ileak_max_A']:.3e}" + (f" | flags: {flags}" if flags else ''))
    return {'status': 'measured', 'reason': '', 'flags': flags, 'main_file': path,
            **{key: res[key] for key in SUMMARY}}


# --- WWL sweep -------------------------------------------------------------------------------------------

def check_wwl_config(cfg):
    """Raise on a bad 'gc_wwl' setting before anything connects or moves."""
    block = cfg.get('gc_wwl', {})
    unknown = set(block) - set(WWL_DEFAULTS)
    if unknown:
        raise ValueError(f"gc_wwl: unknown settings {sorted(unknown)}")
    p = {**WWL_DEFAULTS, **block}
    ok = p['wwl_step'] > 0 and p['wwl_high'] > p['wwl_low']
    if not ok or len(gate_list(p['wwl_low'], p['wwl_high'], p['wwl_step'])) > 4096:
        raise ValueError(f"gc_wwl: WWL {p['wwl_low']} -> {p['wwl_high']} V in {p['wwl_step']} V steps "
                         "(needs low < high, step > 0, <= 4096 points)")
    for key in ('wwl_high', 'wwl_low', 'wbl_high', 'wbl_low', 'rwl_high', 'rwl_low', 'rbl_V'):
        if abs(p[key]) > p['v_max']:
            raise ValueError(f"gc_wwl: |{key}| = {abs(p[key])} V exceeds v_max = {p['v_max']} V")
    if not p['states'] or not set(p['states']) <= {0, 1} or len(set(p['states'])) != len(p['states']):
        raise ValueError(f"gc_wwl: states must be distinct values from (0, 1), got {p['states']}")
    if not 0 <= p['conditioning_s'] <= 655.3:
        raise ValueError(f"gc_wwl: conditioning_s {p['conditioning_s']} outside 0..655.3 s")
    if not 1e-7 <= p['Irange'] <= 0.1:
        raise ValueError(f"gc_wwl: Irange {p['Irange']} A outside 1e-7..0.1 A (no preamps: 100 nA lowest)")
    if not 0 < p['compliance'] <= 0.1:
        raise ValueError(f"gc_wwl: compliance {p['compliance']} A outside (0, 0.1] A")
    return p


def _frame(raw, n, extra):
    data = dict(extra)
    for t in TERMS:
        data[f'{t}_Voltage_meas'] = _column(raw[f'V{t}'], n, f'V{t}')[0]
    for t in TERMS:
        data[f'{t}_Current'], data[f'{t}_Status'] = _column(raw[f'I{t}'], n, f'I{t}')
    return pd.DataFrame(data)


def wwl_sweep(k, chans, p, state):
    """Conditioning (2 biases, outputs left on) + WWL double sweep for one state; outputs off afterwards."""
    names = {chans[t.lower()]: (f'V{t}', f'I{t}') for t in TERMS}
    data_v, init_v = (p['wbl_high'], p['wbl_low']) if state else (p['wbl_low'], p['wbl_high'])
    v = gate_list(p['wwl_low'], p['wwl_high'], p['wwl_step'])
    parts = []
    try:
        for phase, wwl in (('cond_boost', p['wwl_high']), ('cond_hold', p['wwl_low'])):
            bias = {chans['wwl']: wwl, chans['wbl']: init_v, chans['rwl']: p['rwl_low'], chans['rbl']: p['rbl_V']}
            raw = k.smu_bias(bias, names, p['conditioning_s'], p['compliance'], p['Irange'])
            parts.append(_frame(raw, 1, {'State': [state], 'Phase': [phase], 'Direction': [''],
                                         'WWL_Voltage': [wwl]}))
        consts = {chans['wbl']: data_v, chans['rwl']: p['rwl_high'], chans['rbl']: p['rbl_V']}
        raw = k.smu_sweep(chans['wwl'], v, consts, names, p['compliance'], p['Irange'])
        n = len(v)
        parts.append(_frame(raw, n, {'State': [state] * n, 'Phase': ['sweep'] * n,
                                     'Direction': ['f'] * (n // 2) + ['b'] * (n - n // 2), 'WWL_Voltage': v}))
    finally:
        k.outputs_off()
    return pd.concat(parts, ignore_index=True)


def analyse_wwl(data, p):
    """Forward branch: V_WWL where the read current crosses vt_current_A, rising in state 1 (Vt_V: the write
    FET opens and writes WBL high; searched up to the current peak) and falling in state 0 (Vt_s0_V: the node is
    discharged; searched from the peak on, since a stored high below vt_current_A first couples up through it
    as WWL rises). The backward branch keeps what was written (the write FET closes on the new level), so no
    backward crossing is taken; instead |I RBL| at the first (I0) and last (Iend) sweep point, both at WWL low,
    read the stored state before and after the sweep."""
    sw = data[data['Phase'] == 'sweep']
    out = {key: np.nan for key in WWL_SUMMARY}
    for state in sorted(set(sw['State'])):
        s = sw[sw['State'] == state]
        f = s[s['Direction'] == 'f']
        v, i = f['WWL_Voltage'].to_numpy(), np.abs(f['RBL_Current'].to_numpy())
        if np.isfinite(i).any():
            pk = int(np.nanargmax(i))
            seg = slice(0, pk + 1) if state == 1 else slice(pk, None)
            out['Vt_V' if state == 1 else 'Vt_s0_V'] = v_at_current(v[seg], i[seg], p['vt_current_A'])
        out[f'I0_s{state}_A'] = float(abs(s['RBL_Current'].iloc[0]))
        out[f'Iend_s{state}_A'] = float(abs(s['RBL_Current'].iloc[-1]))
    out['Ileak_max_A'] = float(np.nanmax(np.abs(sw[['WWL_Current', 'WBL_Current']].to_numpy())))
    return out


def plot_wwl(data, p, title):
    import matplotlib.pyplot as plt
    states = sorted(set(data['State']), reverse=True)
    fig, axes = plt.subplots(1, len(states), figsize=(6 * len(states), 5), squeeze=False)
    colors = dict(RBL='tab:blue', RWL='tab:orange', WBL='tab:green', WWL='tab:red')
    for ax, state in zip(axes[0], states):
        sw = data[(data['State'] == state) & (data['Phase'] == 'sweep')]
        for t in TERMS:
            for d, ls in (('f', '-'), ('b', '--')):
                s = sw[sw['Direction'] == d]
                ax.semilogy(s['WWL_Voltage'], np.abs(s[f'{t}_Current']), ls, color=colors[t],
                            label=f'|I {t}|' if d == 'f' else None)
        ax.axhline(p['vt_current_A'], color='0.6', lw=0.8)
        data_v, init_v = (p['wbl_high'], p['wbl_low']) if state else (p['wbl_low'], p['wbl_high'])
        ax.set_title(f"state {state}: stored WBL {init_v:g} V, swept with WBL {data_v:g} V")
        ax.set_xlabel('V WWL (V)'); ax.set_ylabel('|I| (A)'); ax.grid(True, which='both', alpha=0.3); ax.legend()
    fig.suptitle(f"{title}\nWWL {p['wwl_low']:g} -> {p['wwl_high']:g} V, {p['wwl_step']:g} V steps, dual; "
                 f"RWL {p['rwl_high']:g} V (conditioning {p['rwl_low']:g} V), RBL {p['rbl_V']:g} V; "
                 f"conditioning {p['conditioning_s']:g} s at WWL {p['wwl_high']:g} then {p['wwl_low']:g} V")
    fig.tight_layout()
    return fig


def measure_cell_wwl(k, chans, cfg, save_dir, descriptor, save=True, show=False, log=print):
    """WWL sweep in every configured state (default 1 then 0) on the cell under the probes; one file."""
    p = check_wwl_config(cfg)
    data = pd.concat([wwl_sweep(k, chans, p, s) for s in p['states']], ignore_index=True)
    res = analyse_wwl(data, p)
    flags = status_flags(data)
    path = None
    if save:
        import matplotlib.pyplot as plt
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{WWL_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}_{descriptor}.csv")
        data.to_csv(path, index=False)
        fig = plot_wwl(data, p, descriptor)
        fig.savefig(path.replace('.csv', '.png'))
        if show:
            plt.show(block=False); plt.pause(1)
        plt.close(fig)
        log(f"Data saved to: {path}")
    log(f"WWL @ |I RBL| = {p['vt_current_A']:g} A: state 1 (rising) {res['Vt_V']:.3f} V, state 0 (falling) "
        f"{res['Vt_s0_V']:.3f} V | |I RBL| at WWL {p['wwl_low']:g} V before -> after: state 1 "
        f"{res['I0_s1_A']:.2e} -> {res['Iend_s1_A']:.2e}, state 0 {res['I0_s0_A']:.2e} -> {res['Iend_s0_A']:.2e} "
        f"| max |I WWL|,|I WBL| {res['Ileak_max_A']:.3e}"
        + (f" | flags: {flags}" if flags else ''))
    return {'status': 'measured', 'reason': '', 'flags': flags, 'main_file': path,
            **{key: res[key] for key in WWL_SUMMARY}}


# --- retention (gc_helpers.gc_retention_test) ------------------------------------------------------------

def check_retention_config(cfg):
    """Raise on a bad 'gc_retention' setting before anything connects or moves. Returns the settings plus
    the schedule: nr readings, interval_adj_s (retention_s / (nr - 1), as the reference)."""
    block = cfg.get('gc_retention', {})
    unknown = set(block) - set(RET_DEFAULTS)
    if unknown:
        raise ValueError(f"gc_retention: unknown settings {sorted(unknown)}")
    p = {**RET_DEFAULTS, **block}
    for key in ('wwl_high', 'wwl_low', 'wbl_high', 'wbl_low', 'rwl_high', 'rwl_low', 'rbl_V'):
        if abs(p[key]) > p['v_max']:
            raise ValueError(f"gc_retention: |{key}| = {abs(p[key])} V exceeds v_max = {p['v_max']} V")
    if not p['states'] or not set(p['states']) <= {0, 1} or len(set(p['states'])) != len(p['states']):
        raise ValueError(f"gc_retention: states must be distinct values from (0, 1), got {p['states']}")
    if not 0 <= p['conditioning_s'] <= 655.3:
        raise ValueError(f"gc_retention: conditioning_s {p['conditioning_s']} outside 0..655.3 s")
    if not p['retention_s'] >= 0.01 or not 0.1 <= p['interval_s'] <= 10:
        raise ValueError(f"gc_retention: retention_s {p['retention_s']} (>= 0.01) / interval_s {p['interval_s']} "
                         "(0.1..10 s: below 0.1 s the 8 readings of a sample may not fit, so the schedule time "
                         "axis would be wrong)")
    count = max(1, round(p['retention_s'] / p['interval_s']))
    p['interval_adj_s'], p['nr'] = p['retention_s'] / count, count + 1
    if not 0.1 <= p['interval_adj_s'] <= 10 or p['nr'] > 4096:
        raise ValueError(f"gc_retention: {p['retention_s']} s at {p['interval_s']} s needs {p['nr']} readings "
                         f"{p['interval_adj_s']:.3g} s apart (KXCI: <= 4096 readings, here 0.1..10 s apart)")
    if not 1e-7 <= p['Irange'] <= 0.1:
        raise ValueError(f"gc_retention: Irange {p['Irange']} A outside 1e-7..0.1 A (no preamps: 100 nA lowest)")
    if not 0 < p['compliance'] <= 0.1:
        raise ValueError(f"gc_retention: compliance {p['compliance']} A outside (0, 0.1] A")
    return p


def retention(k, chans, p, state, log=print):
    """Program one state (2 biases, outputs left on), then sample all lines while it is retained with WBL at
    the opposite level and the read path on (RWL high). Outputs off afterwards."""
    names = {chans[t.lower()]: (f'V{t}', f'I{t}') for t in TERMS}
    prog_v, ret_v = (p['wbl_high'], p['wbl_low']) if state else (p['wbl_low'], p['wbl_high'])
    parts, timing = [], ''
    try:
        for phase, wwl in (('cond_boost', p['wwl_high']), ('cond_hold', p['wwl_low'])):
            bias = {chans['wwl']: wwl, chans['wbl']: prog_v, chans['rwl']: p['rwl_low'], chans['rbl']: p['rbl_V']}
            raw = k.smu_bias(bias, names, p['conditioning_s'], p['compliance'], p['Irange'])
            parts.append(_frame(raw, 1, {'State': [state], 'Phase': [phase], 'Time_s': [np.nan]}))
        consts = {chans['wwl']: p['wwl_low'], chans['wbl']: ret_v, chans['rwl']: p['rwl_high'],
                  chans['rbl']: p['rbl_V']}
        raw = k.smu_sample(consts, names, p['nr'], p['interval_adj_s'], p['compliance'], p['Irange'])
        sched = (p['nr'] - 1) * p['interval_adj_s']
        took = getattr(k, 'last_test_s', None)
        if took is not None and took > 1.2 * sched + 5:
            timing = f"state {state} timing: sampling took {took:.0f} s for a {sched:g} s schedule"
            log(f"WARNING {timing}: the readings were further apart than interval_s, Time_s is too short")
        n = p['nr']
        parts.append(_frame(raw, n, {'State': [state] * n, 'Phase': ['retention'] * n,
                                     'Time_s': np.round(np.arange(n) * p['interval_adj_s'], 6)}))
    finally:
        k.outputs_off()
    return pd.concat(parts, ignore_index=True), timing


def analyse_retention(data):
    out = {key: np.nan for key in RET_SUMMARY}
    r = data[data['Phase'] == 'retention']
    for state in sorted(set(r['State'])):
        i = np.abs(r.loc[r['State'] == state, 'RBL_Current'].to_numpy())
        out[f'Iret_s{state}_start_A'], out[f'Iret_s{state}_end_A'] = float(i[0]), float(i[-1])
    if out['Iret_s0_end_A'] > 0:
        out['ret_ratio_end'] = out['Iret_s1_end_A'] / out['Iret_s0_end_A']
    out['Ileak_max_A'] = float(np.nanmax(np.abs(r[['WWL_Current', 'WBL_Current']].to_numpy())))
    return out


def plot_retention(data, p, title):
    import matplotlib.pyplot as plt
    r = data[data['Phase'] == 'retention']
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    for state, color in ((1, 'tab:blue'), (0, 'tab:orange')):
        s = r[r['State'] == state]
        if s.empty:
            continue
        ax.semilogy(s['Time_s'], np.abs(s['RBL_Current']), color=color,
                    label=f"state {state} (stored {'high' if state else 'low'})")
        for t, ls in (('WWL', '-'), ('WBL', '--')):
            ax2.semilogy(s['Time_s'], np.abs(s[f'{t}_Current']), ls, color=color, label=f'|I {t}| state {state}')
    ax.set_xlabel('time (s)'); ax.set_ylabel('|I RBL| (A)'); ax.grid(True, which='both', alpha=0.3); ax.legend()
    ax2.set_xlabel('time (s)'); ax2.set_ylabel('|I| (A)'); ax2.grid(True, which='both', alpha=0.3); ax2.legend()
    fig.suptitle(f"{title}\nwrite WWL {p['wwl_high']:g} V then hold {p['wwl_low']:g} V ({p['conditioning_s']:g} s "
                 f"each, WBL = data, RWL {p['rwl_low']:g} V); retain WBL = opposite, RWL {p['rwl_high']:g} V, "
                 f"RBL {p['rbl_V']:g} V; {p['nr']} readings {p['interval_adj_s']:g} s apart")
    fig.tight_layout()
    return fig


def measure_cell_retention(k, chans, cfg, save_dir, descriptor, save=True, show=False, log=print):
    """Retention in every configured state (default 1 then 0) on the cell under the probes; one file."""
    p = check_retention_config(cfg)
    parts, timing = [], []
    for s in p['states']:
        log(f"retention state {s}: {p['nr']} readings {p['interval_adj_s']:g} s apart "
            f"(~{p['retention_s'] + 2 * p['conditioning_s']:.0f} s)")
        d, t = retention(k, chans, p, s, log)
        parts.append(d)
        if t:
            timing.append(t)
    data = pd.concat(parts, ignore_index=True)
    res = analyse_retention(data)
    flags = '; '.join(f for f in [status_flags(data)] + timing if f)
    path = None
    if save:
        import matplotlib.pyplot as plt
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, f"{RET_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}_{descriptor}.csv")
        data.to_csv(path, index=False)
        fig = plot_retention(data, p, descriptor)
        fig.savefig(path.replace('.csv', '.png'))
        if show:
            plt.show(block=False); plt.pause(1)
        plt.close(fig)
        log(f"Data saved to: {path}")
    log(f"retention |I RBL| start -> end: state 1 {res['Iret_s1_start_A']:.2e} -> {res['Iret_s1_end_A']:.2e}, "
        f"state 0 {res['Iret_s0_start_A']:.2e} -> {res['Iret_s0_end_A']:.2e} | ratio at end "
        f"{res['ret_ratio_end']:.3g} | max |I WWL|,|I WBL| {res['Ileak_max_A']:.3e}"
        + (f" | flags: {flags}" if flags else ''))
    return {'status': 'measured', 'reason': '', 'flags': flags, 'main_file': path,
            **{key: res[key] for key in RET_SUMMARY}}


def main(argv=None):
    """Offline only: recompute the summary from saved GC_WWL_sweep_*.csv or GC_retention_*.csv files (no
    instrument). Prints a table; --out writes it as CSV (the data files themselves are never changed)."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--reanalyse-wwl', nargs='+', metavar='CSV')
    g.add_argument('--reanalyse-retention', nargs='+', metavar='CSV')
    ap.add_argument('--vt-current-A', type=float, default=WWL_DEFAULTS['vt_current_A'])
    ap.add_argument('--out', help='write the table to this CSV')
    a = ap.parse_args(argv)
    rows = []
    for f in a.reanalyse_wwl or a.reanalyse_retention:
        data = pd.read_csv(f)
        res = (analyse_wwl(data, {'vt_current_A': a.vt_current_A}) if a.reanalyse_wwl
               else analyse_retention(data))
        rows.append({'file': os.path.basename(f), **res, 'flags': status_flags(data)})
    table = pd.DataFrame(rows)
    with pd.option_context('display.width', 250, 'display.max_columns', 20):
        print(table.to_string(index=False, float_format=lambda x: f'{x:.3g}'))
    if a.out:
        table.to_csv(a.out, index=False)
        print(f"written: {a.out}")
    return table


if __name__ == '__main__':
    main()
