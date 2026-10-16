"""Three-terminal FET Id-Vg on the B1500A (pymeasure AgilentB1500 API).

Adapted from S200_B1500A_4200A_py_setup/b1500A_utils/FET_three_terminal.py. Changes:
- number of points uses round() (int() could drop the last point)
- FMT21 channel letter is mapped through each SMU's slot number (smu.channel)
  instead of assuming WGFMUs in slots 1-2
- check_errors() is called after the sweep and raises on instrument error
- outputs are zeroed (DZ) after every sweep
- compliance is a parameter (default 5 mA)
- FMT21 status (EEE) is kept: <Terminal>_Status columns; overflow/invalid points -> NaN
"""
import numpy as np
import pandas as pd


# FMT21 status bits for SMU measurement data (programming guide, Table 1-8)
ST_OVERFLOW, ST_OSC, ST_OTHER_COMP, ST_COMP, ST_INVALID, ST_EOD = 1, 2, 4, 8, 64, 128


def _channel_letter(smu):
    return chr(ord('A') + smu.channel - 1)


def _status(item):
    """EEE of an FMT21 element as int (EOD bit removed). Source-output data carries W/E -> 0."""
    s = item[:-15].strip()
    return int(s) & ~ST_EOD if s.isdigit() else 0


def parse_fmt21(raw, terminals):
    """FMT21 string -> one row per sweep step with <Terminal>_Current/_Voltage and, for measured
    currents, <Terminal>_Status. Overflow/invalid current values become NaN."""
    rows = []
    for item in raw.strip().split(','):
        if len(item) < 5:
            continue
        st, typ = _status(item), 'Current' if item[-14] == 'I' else 'Voltage'
        val = float(item[-13:])
        if typ == 'Current' and st & (ST_OVERFLOW | ST_INVALID):
            val = np.nan
        rows.append({'Value': val, 'Channel': item[-15], 'Type': typ, 'Status': st})
    df = pd.DataFrame(rows)
    items_per_step = len(df[['Channel', 'Type']].drop_duplicates())
    df['Group_ID'] = df.index // items_per_step
    df['Col'] = df['Channel'].map(terminals) + '_' + df['Type']
    if df['Col'].isna().any():
        raise ValueError(f"Unexpected channel letters in data: {sorted(set(df['Channel']) - set(terminals))}")
    data = df.pivot(index='Group_ID', columns='Col', values='Value').reset_index(drop=True)
    cur = df[df['Type'] == 'Current']
    st = cur.pivot(index='Group_ID', columns='Col', values='Status').reset_index(drop=True)
    st.columns = [c.replace('_Current', '_Status') for c in st.columns]
    data = pd.concat([data, st.astype(int)], axis=1)
    data.columns.name = None
    return data


def status_flags(data):
    """Summary of flagged points, e.g. 'gate compliance x12; drain overflow x3' ('' if clean)."""
    out = []
    for term in ('Gate', 'Drain', 'Source'):
        col = f'{term}_Status'
        if col not in data:
            continue
        st = data[col].fillna(0).astype(int)
        for bit, name in ((ST_COMP, 'compliance'), (ST_OSC, 'oscillation'),
                          (ST_OVERFLOW | ST_INVALID, 'overflow/invalid')):
            n = int(((st & bit) != 0).sum())
            if n:
                out.append(f"{term.lower()} {name} x{n}")
    return '; '.join(out)


def IdVg_single_Vd(b1500, smu_gate, smu_drain, smu_source, gate_start=0, gate_stop=1, gate_step=0.05,
                   drain_voltage=0.05, Irange='1 nA limited auto ranging', Vrange='2 V limited auto ranging',
                   compliance=0.005):
    """Single LINEAR_DOUBLE gate sweep at fixed Vd. Returns Gate/Drain/Source _Current/_Voltage columns."""
    num_points = int(round((gate_stop - gate_start) / gate_step)) + 1

    b1500.data_format(21, mode=1)
    b1500.meas_mode('STAIRCASE_SWEEP', smu_gate, smu_drain, smu_source)
    for smu in (smu_gate, smu_drain, smu_source):
        smu.enable()
        smu.adc_type = 'HRADC'
        smu.meas_range_current = Irange
        smu.meas_op_mode = 'COMPLIANCE_SIDE'

    b1500.adc_setup('HRADC', 'AUTO', 6)
    b1500.sweep_timing(hold=0, delay=0, step_delay=0)
    b1500.sweep_auto_abort(False, post='STOP')

    smu_gate.staircase_sweep_source('VOLTAGE', 'LINEAR_DOUBLE', Vrange, gate_start, gate_stop, num_points,
                                    comp=compliance)
    smu_drain.force('VOLTAGE', Vrange, drain_voltage, comp=compliance)
    smu_source.force('VOLTAGE', Vrange, 0, comp=compliance)

    try:
        b1500.check_errors()
        b1500.clear_buffer()
        b1500.clear_timer()
        b1500.send_trigger()
        b1500.check_idle()
        b1500.check_errors()
        raw_data_str = b1500.read()
    finally:
        b1500.force_gnd()

    terminals = {_channel_letter(smu_gate): 'Gate', _channel_letter(smu_drain): 'Drain',
                 _channel_letter(smu_source): 'Source'}
    return parse_fmt21(raw_data_str, terminals)


def IdVg_multi_Vd(b1500, smu_gate, smu_drain, smu_source, gate_start, gate_stop, gate_step, drain_list,
                  Irange='1 nA limited auto ranging', Vrange='2 V limited auto ranging', compliance=0.005,
                  single_fn=None):
    """single_fn: the single-Vd sweep to repeat (default IdVg_single_Vd; the 4200A passes its own)."""
    single_fn = single_fn or IdVg_single_Vd
    all_data = []
    for Vd in np.array(drain_list):
        print(f"Measuring at Drain Voltage: {Vd} V")
        data = single_fn(b1500, smu_gate, smu_drain, smu_source, gate_start, gate_stop, gate_step, Vd,
                         Irange, Vrange, compliance)
        data['Drain_Voltage'] = Vd
        all_data.append(data)
    return pd.concat(all_data, ignore_index=True)


def IdVg_multi_Vd_multi_cycle(b1500, smu_gate, smu_drain, smu_source, gate_start, gate_stop, gate_step,
                              drain_list, num_cycles=1, Irange='1 nA limited auto ranging',
                              Vrange='2 V limited auto ranging', compliance=0.005, single_fn=None):
    all_data = []
    for cycle in range(num_cycles):
        print(f"Starting measurement cycle {cycle + 1} of {num_cycles}")
        data = IdVg_multi_Vd(b1500, smu_gate, smu_drain, smu_source, gate_start, gate_stop, gate_step,
                             drain_list, Irange, Vrange, compliance, single_fn=single_fn)
        data['Cycle'] = cycle + 1
        all_data.append(data)
    return pd.concat(all_data, ignore_index=True)
