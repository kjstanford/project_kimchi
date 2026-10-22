"""Adaptive Id-Vg sequence: initial scan -> screening/Vt -> main multi-Vd, multi-cycle sweep.

Adapted from S200_B1500A_4200A_py_setup/main_utils/main_funs.py
(measure_initial_scan_v2, measure_main_IdVg, measure_all_v2) and
data_processing_utils/IdVg_param_extract.py. Screening thresholds are unchanged
but configurable; the adjusted gate start is clamped to start_limit; results are
returned instead of printed only.

`idvg` is the sweep backend: a module with IdVg_single_Vd / IdVg_multi_Vd_multi_cycle
(measure.fet_idvg for the B1500A, the default; measure.fet_idvg_k4200 for the 4200A).
Both return the same columns and status bits, so screening is the same for either tool.
"""
import os
import time
from math import floor

import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import interp1d

from measure import fet_idvg
from measure.fet_idvg import ST_COMP, status_flags


def get_sweeps(X):
    """Split a double sweep into forward ('f') and backward ('b') halves."""
    n = len(X)
    return {'f': X[:n // 2], 'b': X[n // 2:]}


def Vt_at_constant_Id(Vg_data, Id_data, Id_target=1e-9):
    """Vg at a constant drain current via log-linear interpolation (extrapolates)."""
    Id_abs = np.abs(Id_data)
    mask = (Id_abs > 0) & np.isfinite(Id_abs) & np.isfinite(Vg_data)
    order = np.argsort(Id_abs[mask])
    f = interp1d(np.log10(Id_abs[mask][order]), Vg_data[mask][order], kind='linear', fill_value='extrapolate')
    return float(f(np.log10(Id_target)))


def _save(data, fig, save_dir, test_name, descriptor):
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{test_name}_{time.strftime('%Y%m%d_%H%M%S')}_{descriptor}.csv")
    data.to_csv(path, index=False)
    fig.savefig(path.replace('.csv', '.png'))
    print(f"Data saved to: {path}")
    return path


def _finish_plot(fig, show):
    if show:
        plt.show(block=False)
        plt.pause(1)


def initial_scan(b1500, smus, cfg, W, L, save_dir, descriptor, save=True, show=False, log=print, idvg=fet_idvg):
    """Returns dict(skip, reason, gate_start, Vt, on_off, flags, file)."""
    gate, drain, source = smus
    step = cfg['scan_step']
    Vd = cfg['Vd_scan']
    data = idvg.IdVg_single_Vd(b1500, gate, drain, source, cfg['default_start'], cfg['default_stop'], step, Vd,
                          cfg['Irange'], cfg['Vrange'], cfg['compliance'])

    fig = plt.figure(figsize=(8, 6))
    for col, lab in (('Drain_Current', 'Drain Current'), ('Gate_Current', 'Gate Current'),
                     ('Source_Current', 'Source Current')):
        plt.semilogy(data['Gate_Voltage'].to_numpy(), np.abs(data[col].to_numpy()), label=lab)
    plt.xlabel('Gate Voltage (V)'); plt.ylabel('Current (A)')
    plt.title(f'Initial Id-Vg Scan at Vd = {Vd} V  [{descriptor}]'); plt.legend(); plt.grid(True)
    _finish_plot(fig, show)
    path = _save(data, fig, save_dir, 'IdVg_initial_scan', descriptor) if save else None
    plt.close(fig)

    s = cfg['screen']
    Vg = data['Gate_Voltage'].to_numpy()
    Id = np.abs(data['Drain_Current'].to_numpy())
    Ig = np.abs(data['Gate_Current'].to_numpy())
    Vg_f, Id_f = get_sweeps(Vg)['f'], get_sweeps(Id)['f']
    Id_lo, Id_hi, Ig_hi = np.nanmin(Id), np.nanmax(Id), np.nanmax(Ig)
    out = {'skip': False, 'reason': '', 'gate_start': cfg['default_start'], 'Vt': np.nan,
           'on_off': float(Id_hi / Id_lo) if Id_lo > 0 else np.inf, 'flags': status_flags(data), 'file': path}
    if out['flags']:
        log(f"Status flags: {out['flags']}")

    gate_comp = (data['Gate_Status'].astype(int) & ST_COMP) != 0 if 'Gate_Status' in data else np.zeros(len(data), bool)
    if gate_comp.any():
        out.update(skip=True, reason=f"gate compliance (Ig hit {Ig_hi:.3g} A) - possible breakdown")
        log(f"max Id {Id_hi:.3e} | max Ig {Ig_hi:.3e}")
        return out
    f_lo, f_hi = np.nanmin(Id_f), np.nanmax(Id_f)
    if f_lo > s.get('short_Id_min', 1e-5) and f_hi / f_lo < s.get('short_on_off_max', 2.0):
        Id_mid = float(np.nanmedian(Id_f))
        out.update(skip=True, reason=f"S/D short: Id flat ~{Id_mid * 1e3:.3g} mA "
                                     f"(~{abs(Vd) / Id_mid:.3g} ohm at Vd={Vd:g} V)")
        log(f"max Id {Id_hi:.3e} | max Ig {Ig_hi:.3e} | on/off {out['on_off']:.3g}")
        return out

    try:
        out['Vt'] = Vt_at_constant_Id(Vg_f, Id_f, Id_target=s['Vt_Id_per_WL'] * W / L)
    except ValueError:
        pass
    log(f"Vt @ Id = {s['Vt_Id_per_WL']:g} A * W/L: {out['Vt']} V | max Id {Id_hi:.3e} | "
        f"max Ig {Ig_hi:.3e} | on/off {out['on_off']:.3g}")

    per_WL = s.get('Id_max_min_per_WL')   # scales with W/L when set, else the fixed Id_max_min
    Id_min = per_WL * W / L if per_WL else s['Id_max_min']
    if np.isnan(out['Vt']):
        out.update(skip=True, reason='Vt extraction failed')
    elif not (s['Vt_min'] <= out['Vt'] <= s['Vt_max']):
        out.update(skip=True, reason=f"Vt out of range [{s['Vt_min']}, {s['Vt_max']}] V")
    elif Ig_hi > s['Ig_max']:
        out.update(skip=True, reason=f"gate leakage > {s['Ig_max']:g} A")
    elif Id_hi < Id_min:
        out.update(skip=True, reason=f"max Id < {Id_min:g} A" + (f" ({per_WL:g} A x W/L)" if per_WL else ''))
    elif (Id_lo < 1e-12 or Id_hi > 1e-7) and out['on_off'] < s['on_off_min']:
        out.update(skip=True, reason=f"on/off < {s['on_off_min']:g}")
    if out['skip']:
        return out

    if not cfg.get('adaptive_start', True):   # fixed main-sweep start
        return out
    if Id_lo <= s['Voff_Id']:
        Voff = Vt_at_constant_Id(Vg_f, Id_f, Id_target=s['Voff_Id'])
        log(f"Voff = {Voff:.3f} V at Id = {s['Voff_Id']:g} A")
        out['gate_start'] = max(cfg['start_limit'], round(floor((Voff - 0.5) / step) * step, 6))
    else:
        out['gate_start'] = cfg['start_limit']
    return out


def main_sweep(b1500, smus, cfg, gate_start, save_dir, descriptor, save=True, show=False,
               cycle_colors=('blue', 'red', 'green', 'black', 'orange'), idvg=fet_idvg):
    gate, drain, source = smus
    drain_list = list(cfg['IdVg_drain_list'])
    data = idvg.IdVg_multi_Vd_multi_cycle(b1500, gate, drain, source, gate_start, cfg['default_stop'], cfg['main_step'],
                                     drain_list, cfg['IdVg_cycles'], cfg['Irange'], cfg['Vrange'],
                                     cfg['compliance'])
    styles = dict(zip(drain_list, ('-', '--', ':', '-.')))
    fig = plt.figure(figsize=(8, 6))
    for cycle in data['Cycle'].unique():
        cd = data[data['Cycle'] == cycle]
        color = cycle_colors[(cycle - 1) % len(cycle_colors)]
        for Vd in cd['Drain_Voltage'].unique():
            vd = cd[cd['Drain_Voltage'] == Vd]
            ls = styles.get(Vd, '-')
            for col in ('Drain_Current', 'Gate_Current', 'Source_Current'):
                plt.semilogy(vd['Gate_Voltage'].to_numpy(), np.abs(vd[col].to_numpy()), color=color, linestyle=ls)
    plt.xlabel('Gate Voltage (V)'); plt.ylabel('Current (A)')
    plt.title(f"Id-Vg at Vd = {', '.join(f'{v:g}' for v in drain_list)} V  [{descriptor}]"); plt.grid(True)
    _finish_plot(fig, show)
    path = _save(data, fig, save_dir, 'IdVg_main', descriptor) if save else None
    plt.close(fig)
    return path, status_flags(data)


def measure_device(b1500, smus, cfg, W, L, save_dir, descriptor, save=True, show=False, log=print, idvg=fet_idvg):
    """Initial scan + (if it passes) main sweep. Returns a result dict for the run summary."""
    scan = initial_scan(b1500, smus, cfg, W, L, save_dir, descriptor, save, show, log, idvg=idvg)
    res = {'status': 'screened_out' if scan['skip'] else 'measured', 'reason': scan['reason'],
           'Vt_V': scan['Vt'], 'on_off': scan['on_off'], 'gate_start_V': scan['gate_start'],
           'flags': scan['flags'], 'scan_file': scan['file'], 'main_file': None}
    if scan['skip']:
        log(f"SKIPPING main sweep: {scan['reason']}")
    elif cfg.get('scan_only', False):
        log("scan only (measurement.scan_only): no main sweep")
    else:
        log(f"Main sweep {scan['gate_start']} V -> {cfg['default_stop']} V")
        res['main_file'], main_flags = main_sweep(b1500, smus, cfg, scan['gate_start'], save_dir, descriptor,
                                                  save, show, idvg=idvg)
        if main_flags:
            log(f"Main sweep status flags: {main_flags}")
            res['flags'] = '; '.join(f for f in (f"scan: {scan['flags']}" if scan['flags'] else '',
                                                 f"main: {main_flags}") if f)
    return res
