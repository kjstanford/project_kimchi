"""Three-terminal FET Id-Vg on the Keithley 4200A (KXCI list sweep), same interface and output as
measure.fet_idvg so idvg_pipeline screens and saves it unchanged.

- the double sweep is a gate voltage list start..stop then stop..start (stop measured twice, like the
  B1500 LINEAR_DOUBLE sweep), so get_sweeps() halves it the same way
- smu_* are 4200A SMU numbers (1..n); Irange is the lowest limited-autorange current range in A (RG);
  Vrange is not used (the 4200A picks the source range itself)
- columns: Gate/Drain/Source _Current and _Status, Gate_Voltage (programmed, like the B1500 source data)
  and Gate_Voltage_meas (the 4200A reading). The DO status letter maps onto the B1500 FMT21 bits:
  C -> compliance, T -> other channel in compliance, X -> oscillation, V -> overflow (value NaN)
- outputs are turned off after every sweep, also on errors
"""
import numpy as np
import pandas as pd

from measure import fet_idvg
from measure.fet_idvg import ST_COMP, ST_OSC, ST_OTHER_COMP, ST_OVERFLOW

STATUS_BITS = {'N': 0, 'L': 0, 'C': ST_COMP, 'T': ST_OTHER_COMP, 'X': ST_OSC, 'V': ST_OVERFLOW}


def gate_list(start, stop, step):
    n = int(round((stop - start) / step)) + 1
    fwd = [round(start + (stop - start) * i / (n - 1), 6) for i in range(n)] if n > 1 else [start]
    return fwd + fwd[::-1]


def _column(readings, n, name):
    if len(readings) != n:
        raise ValueError(f"4200A returned {len(readings)} {name} readings, expected {n}")
    st = np.array([STATUS_BITS.get(c, 0) for c, _ in readings], dtype=int)
    val = np.array([v for _, v in readings], dtype=float)
    val[(st & ST_OVERFLOW) != 0] = np.nan
    val[np.abs(val) >= 1e37] = np.nan          # 9.000E+37 = not-a-measurement marker
    return val, st


def IdVg_single_Vd(k4200, smu_gate, smu_drain, smu_source, gate_start=0, gate_stop=1, gate_step=0.05,
                   drain_voltage=0.05, Irange=1e-9, Vrange=None, compliance=0.005):
    vg = gate_list(gate_start, gate_stop, gate_step)
    try:
        raw = k4200.list_sweep(smu_gate, smu_drain, smu_source, vg, drain_voltage, compliance, Irange)
    finally:
        k4200.outputs_off()
    data = {'Gate_Voltage': vg}
    data['Gate_Voltage_meas'] = _column(raw['VG'], len(vg), 'VG')[0]
    for term, name in (('Gate', 'IG'), ('Drain', 'ID'), ('Source', 'ISRC')):
        data[f'{term}_Current'], data[f'{term}_Status'] = _column(raw[name], len(vg), name)
    cols = ['Drain_Current', 'Gate_Current', 'Gate_Voltage', 'Source_Current', 'Drain_Status', 'Gate_Status',
            'Source_Status', 'Gate_Voltage_meas']
    return pd.DataFrame(data)[cols]


def IdVg_multi_Vd_multi_cycle(k4200, smu_gate, smu_drain, smu_source, gate_start, gate_stop, gate_step,
                              drain_list, num_cycles=1, Irange=1e-9, Vrange=None, compliance=0.005):
    return fet_idvg.IdVg_multi_Vd_multi_cycle(k4200, smu_gate, smu_drain, smu_source, gate_start, gate_stop,
                                              gate_step, drain_list, num_cycles, Irange, Vrange, compliance,
                                              single_fn=IdVg_single_Vd)
