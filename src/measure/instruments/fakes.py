"""Dummy S200 / B1500 / 4200A for dry runs. No GPIB, no VISA.

FakeVisaProber mimics the pyvisa resource the real VeloxProber wraps; FakeB1500/FakeSMU
mimic the pymeasure AgilentB1500 API used by measure.fet_idvg; FakeK4200Visa mimics the pyvisa
resource the real measure.instruments.k4200.K4200 driver sends KXCI to. All write the command they
stand in for into a shared Bench.log and enforce interlocks:
- no chuck motion (XY or Z) while any SMU output is non-zero
- no XY motion while at contact height
- no measurement trigger unless at contact height
"""
import math
import random
import re


GC_LINES = ('WWL', 'WBL', 'RWL', 'RBL')


class SafetyViolation(AssertionError):
    pass


def fet_point(rng, dev, v, vd, comp):
    """Simple n-FET at gate voltage v, drain voltage vd -> (Id, Ig, gate_in_compliance).
    dev = (W_um, L_um[, fault]) or None (open site); fault in {None, 'short', 'gate_break', 'overflow'}."""
    W, L, fault = (tuple(dev) + (None,))[:3] if dev is not None else (None, None, 'open')
    if fault == 'open':
        Id = rng.gauss(0, 2e-13)
    elif fault == 'short':
        Id = abs(vd) / 40.0 * (1 + rng.gauss(0, 1e-3))
    else:
        nvt, Vt = 1.35 * 0.0259, 0.3
        veff = nvt * math.log1p(math.exp(min((v - Vt) / nvt, 50)))
        Id = (W / L) * 1e-7 * (veff / nvt) * (1 - math.exp(-abs(vd) / 0.0259))
        Id = min(Id, comp) + abs(rng.gauss(0, 2e-13))
    Ig = abs(rng.gauss(0, 5e-14))
    if fault == 'gate_break' and abs(v) > 1.0:
        return Id, comp, True
    return Id, Ig, False


class Bench:
    def __init__(self):
        self.log = []          # list of 'PROBER: ...' / 'B1500: ...' strings in order
        self.bias_live = False
        self.in_contact = False


class FakeVisaProber:
    Z_SEP, Z_CONTACT = -800.0, -750.0

    def __init__(self, bench, start=(-1194.69955, -8756.63996), move_scale=1.0, move_rot_deg=0.0):
        """move_scale / move_rot_deg simulate a systematic chuck error (seen on the S200:
        a commanded (-200, 0) um move read back as (-203.557, +3.198) um)."""
        self.bench = bench
        self.x, self.y = start
        self.scale, self.rot = move_scale, math.radians(move_rot_deg)
        self.z = self.Z_SEP
        self.timeout = 20000

    def _pos(self):
        return f"0: {self.x:.5f} {self.y:.5f} {self.z:.5f}"

    def _do(self, cmd):
        self.bench.log.append(f"PROBER: {cmd}")
        parts = cmd.split()
        name = parts[0]
        if name in ('MoveChuck', 'MoveChuckSeparation', 'MoveChuckContact') and self.bench.bias_live:
            raise SafetyViolation(f"{name} while SMU outputs are live")
        if name == 'MoveChuck':
            if self.bench.in_contact:
                raise SafetyViolation("MoveChuck (XY) while at contact height")
            if parts[3:5] != ['R', 'Y']:
                raise ValueError(f"Fake only supports relative micron moves: {cmd}")
            mx, my = float(parts[1]), float(parts[2])
            c, s = math.cos(self.rot), math.sin(self.rot)
            self.x += self.scale * (c * mx - s * my)
            self.y += self.scale * (s * mx + c * my)
        elif name == 'MoveChuckSeparation':
            self.z, self.bench.in_contact = self.Z_SEP, False
        elif name == 'MoveChuckContact':
            self.z, self.bench.in_contact = self.Z_CONTACT, True
        elif name == '*IDN?':
            return "Cascade Microtech,Velox,FAKE,0.0"
        elif name != 'ReadChuckPosition':
            raise ValueError(f"Unknown Velox command in fake: {cmd}")
        return self._pos()

    def write(self, cmd):
        self._do(cmd)

    def query(self, cmd):
        return self._do(cmd)

    def close(self):
        self.bench.log.append("PROBER: <close>")


class FakeSMU:
    def __init__(self, parent, channel, name):
        self._p, self.channel, self.name = parent, channel, name
        self.adc_type = self.meas_range_current = self.meas_op_mode = None
        self.sweep = None      # (start, stop, n, mode)
        self.voltage = 0.0
        self.comp = None

    def _w(self, s):
        self._p.bench.log.append(f"B1500: {s}")

    def enable(self):
        self._w(f"CN {self.channel}")

    def disable(self):
        self._w(f"CL {self.channel}")

    def force_gnd(self):
        self._w(f"DZ {self.channel}")
        self.voltage = 0.0
        self._p._update_bias()

    def staircase_sweep_source(self, source_type, mode, source_range, start, stop, steps, comp):
        self._w(f"WV {self.channel},{mode},{start},{stop},{steps},{comp}")
        self.sweep = (start, stop, steps, mode)
        self.comp = comp
        self.voltage = start
        self._p._update_bias()

    def force(self, source_type, source_range, output, comp):
        self._w(f"DV {self.channel},{output},{comp}")
        self.voltage = float(output)
        self.comp = comp
        self._p._update_bias()


class FakeB1500:
    """smu1..smu4 sit in slots 3..6 (slots 1-2 are WGFMUs on the real tool)."""

    def __init__(self, bench, device_lookup=lambda: (10.0, 2.0), fail_on_trigger=None, seed=0):
        self.bench = bench
        # -> (W_um, L_um[, fault]) or None for an open site; fault in {None, 'short', 'gate_break', 'overflow'}
        self.device_lookup = device_lookup
        self.fail_on_trigger = fail_on_trigger   # raise OSError on the n-th trigger (1-based)
        self.n_trigger = 0
        self.rng = random.Random(seed)
        self.smus = []
        for i, slot in enumerate((3, 4, 5, 6), start=1):
            smu = FakeSMU(self, slot, f"SMU{i}")
            setattr(self, f"smu{i}", smu)
            self.smus.append(smu)
        self.mm_channels = []
        self._data = ''

    def _w(self, s):
        self.bench.log.append(f"B1500: {s}")

    def _update_bias(self):
        self.bench.bias_live = any(abs(s.voltage) > 0 for s in self.smus)

    # --- pymeasure-like API ---
    def data_format(self, fmt, mode=0):
        self._w(f"FMT {fmt},{mode}")

    def meas_mode(self, mode, *smus):
        self.mm_channels = list(smus)
        self._w(f"MM 2,{','.join(str(s.channel) for s in smus)}")

    def adc_setup(self, adc, mode, n):
        self._w(f"AIT {adc},{mode},{n}")

    def sweep_timing(self, hold, delay, step_delay=0):
        self._w(f"WT {hold},{delay},{step_delay}")

    def sweep_auto_abort(self, abort, post='START'):
        self._w(f"WM {int(bool(abort)) + 1},{post}")

    def check_errors(self):
        self._w("ERRX?")

    def clear_buffer(self):
        self._w("BC")

    def clear_timer(self):
        self._w("TSR")

    def check_idle(self):
        self._w("*OPC?")

    def force_gnd(self):
        self._w("DZ")
        for s in self.smus:
            s.voltage = 0.0
        self._update_bias()

    def send_trigger(self):
        self._w("XE")
        self.n_trigger += 1
        if not self.bench.in_contact:
            raise SafetyViolation("Measurement triggered while chuck is not at contact height")
        if self.fail_on_trigger and self.n_trigger == self.fail_on_trigger:
            raise OSError("Agilent B1500 Error 999: injected fault")
        self._data = self._simulate()

    def read(self):
        return self._data

    # --- simple n-FET model, FMT21 output ("EEE" status + channel + type + value) ---
    def _simulate(self):
        gate = next(s for s in self.mm_channels if s.sweep)
        others = [s for s in self.mm_channels if s is not gate]
        drain = max(others, key=lambda s: abs(s.voltage))
        source = next(s for s in others if s is not drain)
        start, stop, n, mode = gate.sweep
        vg = [start + (stop - start) * i / (n - 1) for i in range(n)]
        if mode == 'LINEAR_DOUBLE':
            vg = vg + vg[::-1]
        dev = self.device_lookup()
        fault = (tuple(dev) + (None,))[2] if dev is not None else 'open'
        comp = gate.comp or 0.1
        letter = lambda s: chr(ord('A') + s.channel - 1)
        toks = []
        for i, v in enumerate(vg):
            Id, Ig, gcomp = fet_point(self.rng, dev, v, drain.voltage, comp)
            st_g, st_d, st_s = (8, 4, 4) if gcomp else (0, 0, 0)
            Is = -(Id + Ig)
            Id_s = f"{Id:+.6E}"
            if fault == 'overflow' and i % 10 == 3:
                Id_s, st_d = '+199.9990E+99', 1      # the B1500 overflow value (13 chars like every field)
            tag = '  E' if i == len(vg) - 1 else '  W'
            toks += [f"{st_g:03d}{letter(gate)}I{Ig:+.6E}", f"{st_d:03d}{letter(drain)}I{Id_s}",
                     f"{st_s:03d}{letter(source)}I{Is:+.6E}", f"{tag}{letter(gate)}V{v:+.6E}"]
        return ','.join(toks)


class FakeK4200Visa:
    """KXCI behind a fake pyvisa resource, strict where the real KXCI is:
    - each command only after its page command (DE/SS/SM/MD/US), numbers <= 12 characters;
      a bad command sets status byte B1 (+RQS) and :ERROR:LAST:GET returns the message
    - a serial poll clears B0/B1/B6; while a test runs the poll returns 16 (busy) `busy_polls` times
    - outputs: at the end of a test a channel goes to 0 V only if 'ST n, 1' was sent, otherwise it keeps
      its last value (bench.bias_live); in user mode 'DVn' turns that output off
    - argument errors (-993) only set the last error, no status bit (as seen on the real 4200A)
    *OPT? lists SMUPA1..3 + SMU4 (preamps on the three test SMUs) unless `opt` says otherwise; RG accepts
    1e-12 A with a preamp, 1e-7 A without.
    :PMU: commands (Segment Arb only), as strict as the KXCI manual: :PMU:INIT first, <= 128 values per
    command with :ADD to append, fixed ranges, >= 3 segments per sequence, 20 ns..1 s on the 10 ns grid,
    seamless start = previous stop (also across the sequence list and loops), <= 2048 segments, <= 512
    sequences, <= 65,536 points and < 1e6 samples; violations give the documented codes (-954/-956/-957/
    -958/-959/-960/-961/-962/-963/-967/-980). RP after any :PMU: command is refused (they cannot be mixed
    [7-1]). EXECUTE needs the RPMs in pulse mode and the chuck at contact; it runs for `pmu_busy_polls`
    :PMU:TEST:STATUS? polls, during which DATA:COUNT? grows. The simulated FET (pmu_roles = gate, drain,
    source channels) gets a hidden PBTI shift dVt = A * t_stress^n * max(0, 1 - r * log10(1 + t_off / tau)),
    stress_model = (A, n, r, tau, threshold): t_stress is the time with the gate flat at >= threshold V
    (2.9 V: above the 2.5 V top of the 0 -> 2.5 V read staircase, below the 3 V stress).
    The point format 'V,I,T,status' is a guess (the SARB layout is undocumented; bring-up step L0).
    While a PMU test runs, any query except :PMU:TEST:STATUS? / DATA:COUNT? / DATA:GET is refused with
    -980 and gets no reply (TimeoutError), as on the real tool."""
    PMU_RANGES = {1e-7: 2e-10, 1e-6: 2e-9, 1e-5: 5e-9, 1e-4: 5e-8, 1e-3: 3e-7, 1e-2: 1.5e-6, 0.2: 5e-5, 0.8: 5e-5}
    SARB_KEYS = {'STARTV': float, 'STOPV': float, 'TIME': float, 'MEAS:TYPE': int, 'MEAS:START': float,
                 'MEAS:STOP': float, 'TRIG': int, 'SSR': int}
    PAGES = {'DE': {'CH', 'VS', 'VM'}, 'SS': {'VL', 'VC', 'HT', 'DT', 'ST', 'RP'},
             'SM': {'DM', 'LI', 'XN', 'YA', 'YB', 'NR', 'IN', 'WT'}, 'MD': {'ME'}, 'US': {'DV'}}
    MODELESS = {'BC', 'DR', 'IT', 'RS', 'RG'}

    def __init__(self, bench, device_lookup=lambda: (10.0, 2.0), fail_on_trigger=None, seed=0,
                 opt='SMUPA1,SMUPA2,SMUPA3,SMU4,,,,', busy_polls=2, pmu_busy_polls=3, pmu_roles=(1, 2, 3),
                 stress_model=(0.02, 0.3, 0.1, 1e-4, 2.9), pmu_gc=None):
        self.bench, self.device_lookup, self.fail_on_trigger = bench, device_lookup, fail_on_trigger
        self.rng, self.opt, self.busy_polls = random.Random(seed), opt, busy_polls
        self.preamp = {int(m.group(3)): bool(m.group(2)) for m in re.finditer(r'(HP)?SMU(PA)?(\d+)', opt)}
        self.n_smu = max(self.preamp, default=0)
        self.page, self.timeout, self.n_trigger = None, 60000, 0
        self.stb, self.err, self.dr, self.dm, self.li = 0, '', False, None, []
        self.nr, self.gc_sn = 1, 0.0      # sampling readings; gain-cell storage node (kept across tests)
        self.ch, self.vl, self.vc, self.standby, self.out = {}, {}, {}, set(), {}
        self.buf, self._busy = {}, 0
        self.pmu_busy_polls, self.pmu_roles, self.stress_model = pmu_busy_polls, tuple(pmu_roles), stress_model
        self.pmu_gc = tuple(pmu_gc) if pmu_gc else None      # (WWL, WBL, RWL, RBL) channels: gain-cell PMU model
        self.rpm_names = set()
        for m in re.finditer(r'PMU(\d)(RPM(1-2|1|2))?', opt):
            if m.group(3):
                self.rpm_names |= {f"PMU{m.group(1)}-{c}" for c in m.group(3).split('-')}
        self.pmu_cards = {int(m.group(1)) for m in re.finditer(r'PMU(\d)', opt)}
        self.rpm, self.pmu, self.pmu_used, self.pmu_run = {}, None, False, None
        self.pmu_out, self.pmu_data, self.pmu_final = set(), {}, {}
        self.pending = []             # stale replies waiting in the output buffer (read() returns them)

    def _log(self, s):
        self.bench.log.append(f"K4200: {s}")

    def _update_bias(self):
        pmu = bool(self.pmu_run and self.pmu_out) or any(abs(self.pmu_final.get(c, 0)) > 0 for c in self.pmu_out)
        self.bench.bias_live = any(abs(v) > 0 for v in self.out.values()) or pmu

    def _error(self, msg, code=-992):
        self.err = f"{msg} ({code})"
        if code != -993:                    # the real 4200A sets no status bit for argument errors
            self.stb |= 2 | 64

    def _done(self):
        self.stb |= 1 | (64 if self.dr else 0)

    def read_stb(self):
        if self._busy:
            self._busy -= 1
            if not self._busy:
                self._done()
            return 16
        s, self.stb = self.stb, self.stb & ~(1 | 2 | 64)
        return s

    def read(self):
        if self.pending:
            return self.pending.pop(0)
        raise TimeoutError("VI_ERROR_TMO: nothing to read")

    def query(self, cmd):
        self._log(cmd)
        if self.pending:              # a stale reply is answered first, shifting every later answer
            self.pending.append(self._query(cmd))
            return self.pending.pop(0)
        return self._query(cmd)

    def _query(self, cmd):
        c = cmd.strip()
        if c == '*IDN?':
            return 'KEITHLEY INSTRUMENTS,KI4200A,FAKE,V0.0'
        if c == '*OPT?':
            return self.opt
        if self.pmu_run and not c.upper().startswith((':PMU:TEST:STATUS?', ':PMU:DATA:COUNT?', ':PMU:DATA:GET')):
            # the real KXCI refuses it with -980 and never replies (seen live 2026-10-15)
            self._error("Command not valid during test execution.", -980)
            raise TimeoutError(f"VI_ERROR_TMO: no reply to {c!r} during a PMU test")
        if c == ':ERROR:LAST:GET':
            return self.err          # '' = no error, as on the real 4200A (V1.13)
        if c == ':ERROR:LAST:CLEAR':        # no reply over GPIB: a query of it times out on the real tool
            raise TimeoutError("VI_ERROR_TMO: :ERROR:LAST:CLEAR sends no reply")
        if c.upper().startswith(':PMU:'):
            return self._pmu_query(c)
        if c.startswith('DO '):
            name = c[3:].strip().strip("'")
            if self._busy:
                self._error("Command not valid during test execution", -980)
                return ''
            if name not in self.buf:
                self._error(f"DO for unknown name {name!r}", -993)
                return ''
            return ','.join(f"{st}{v:+.4E}" for st, v in self.buf[name])
        self._error(f"unknown query {c!r}")
        return ''

    def write(self, cmd):
        self._log(cmd)
        c = cmd.strip()
        if c in self.PAGES:
            self.page = c
            return
        if c == ':ERROR:LAST:CLEAR':
            self.err = ''
            return
        if c.upper().startswith(':PMU:'):
            return self._pmu_write(c)
        head = ''.join(ch for ch in c[:2] if ch.isalpha())
        args = [a.strip() for a in c[len(head):].split(',')] if len(c) > len(head) else []
        if head not in self.MODELESS and head not in self.PAGES.get(self.page, ()):
            return self._error(f"{c!r} not valid on page {self.page}", -989)
        if any(len(a) > 12 for a in args if a and a[0] not in "'P"):
            return self._error(f"number too long in {c!r}", -993)
        try:
            getattr(self, f"_{head}")(args)
        except (ValueError, IndexError, KeyError) as e:
            self._error(f"bad arguments in {c!r}: {e}", -993)

    def _smu(self, ch):
        return 1 <= ch <= self.n_smu

    # --- commands ---
    def _BC(self, a):
        self.buf, self.stb = {}, self.stb & ~1

    def _DR(self, a):
        self.dr = a[0] == '1'

    def _IT(self, a):
        if a[0] not in ('1', '2', '3') and not (a[0] == '4' and len(a) == 4):
            raise ValueError('IT')

    def _RS(self, a):
        if not 3 <= int(a[0]) <= 7:
            raise ValueError('RS')

    def _RG(self, a):
        ch = int(a[0])
        if not self._smu(ch) or not (1e-12 if self.preamp.get(ch) else 1e-7) <= float(a[1]) <= 0.1:
            raise ValueError('RG')

    def _CH(self, a):
        ch = int(a[0])
        if not self._smu(ch):
            raise ValueError('CH channel')
        if len(a) == 1:
            for d in (self.ch, self.vl, self.vc):
                d.pop(ch, None)
        else:
            self.ch[ch] = (a[1].strip("'"), a[2].strip("'"), int(a[3]), int(a[4]))
            (self.vc if self.ch[ch][3] == 1 else self.vl).pop(ch, None)   # a redefined channel drops its old source

    def _VS(self, a):
        pass

    _VM = _VS

    def _VL(self, a):
        ch, master, comp, pts = int(a[0]), int(a[1]), float(a[2]), [float(x) for x in a[3:]]
        if ch not in self.ch or self.ch[ch][3] != 1 or master not in (0, 1) or not 0 < len(pts) <= 4096:
            raise ValueError('VL')
        self.vl[ch] = (comp, pts)

    def _VC(self, a):
        ch = int(a[0])
        if ch not in self.ch or self.ch[ch][3] != 3:
            raise ValueError('VC')
        self.vc[ch] = (float(a[1]), float(a[2]))

    def _HT(self, a):
        float(a[0])

    _DT = _HT

    def _ST(self, a):
        (self.standby.add if a[1] == '1' else self.standby.discard)(int(a[0]))

    def _RP(self, a):
        if self.pmu_used:
            return self._error("two-letter pulse command RP cannot be used with the :PMU: commands", -992)
        if a[1] not in ('0', '1', '2', '3'):
            raise ValueError('RP')

    def _DM(self, a):
        self.dm = int(a[0])

    def _LI(self, a):
        self.li = [x.strip("'") for x in a]

    def _XN(self, a):
        pass

    def _NR(self, a):
        n = int(a[0])
        if not 1 <= n <= 4096:
            raise ValueError('NR')
        self.nr = n

    def _IN(self, a):
        if not 0.01 <= float(a[0]) <= 10:
            raise ValueError('IN')

    def _WT(self, a):
        if not 0 <= float(a[0]) <= 100:
            raise ValueError('WT')

    _YA = _YB = _XN

    def _DV(self, a):
        ch = int(a[0])
        if not self._smu(ch):
            raise ValueError('DV')
        self.out[ch] = float(a[2]) if len(a) >= 3 else 0.0
        self._update_bias()

    def _ME(self, a):
        if a[0] == '4':
            self._busy = 0
            return
        if a[0] != '1':
            raise ValueError('ME')
        self.n_trigger += 1
        if not self.bench.in_contact:
            raise SafetyViolation("Measurement triggered while chuck is not at contact height")
        if self.fail_on_trigger and self.n_trigger == self.fail_on_trigger:
            raise OSError("VI_ERROR_TMO: injected 4200A fault")
        gates = [c for c in self.vl if c in self.ch]
        if {f'I{t}' for t in GC_LINES} <= {c[1] for c in self.ch.values()} and self.dm == 2:
            return self._gain_cell(gates)
        if len(gates) != 1 or len(self.vc) != 2 or self.dm != 2:
            raise ValueError('test not set up: one VL channel, two VC channels and DM2 expected')
        g = gates[0]
        comp, vg = self.vl[g]
        (d, (vd, _)), (s, (vs, _)) = sorted(self.vc.items(), key=lambda kv: -abs(kv[1][0]))
        names = {self.ch[g][0]: 'VG', self.ch[g][1]: 'IG', self.ch[d][1]: 'ID', self.ch[s][1]: 'IS'}
        dev = self.device_lookup()
        fault = (tuple(dev) + (None,))[2] if dev is not None else 'open'
        cols = {'VG': [], 'IG': [], 'ID': [], 'IS': []}
        for i, v in enumerate(vg):
            Id, Ig, gcomp = fet_point(self.rng, dev, v, vd, comp)
            sg, sd = ('C', 'T') if gcomp else ('N', 'N')
            if fault == 'overflow' and i % 10 == 3:
                sd, Id = 'V', 1.05e-1
            cols['VG'].append(('N', v + self.rng.gauss(0, 1e-5)))
            cols['IG'].append((sg, Ig))
            cols['ID'].append((sd, Id))
            cols['IS'].append((sd, -(Id + Ig)))
        self.buf = {n: cols[k] for n, k in names.items() if n in self.li}   # only listed names are measured
        self._end_test({g: vg[-1], d: vd, s: vs})

    def _end_test(self, last):
        for ch in self.ch:
            self.out[ch] = 0.0 if ch in self.standby else last.get(ch, 0.0)
        self._update_bias()
        self._busy = self.busy_polls
        if not self._busy:
            self._done()

    def _gain_cell(self, swept):
        """2T gain cell, recognised by the line names (IWWL, IWBL, IRWL, IRBL): a list sweep (one VL + three VC)
        or a sampling test (all VC, NR readings). The write FET passes WBL to the storage node while
        V_WWL - min(V_WBL, SN) > 1 V, else the node holds (also between tests), leaking 0.1 % per reading toward
        WBL; the read FET (the site's W/L) sits between RWL and RBL with the node as gate: current in at RWL, out
        at RBL; WBL/WWL see noise."""
        by = {c[1][1:]: ch for ch, c in self.ch.items()}          # 'WWL' -> channel
        if len(swept) > 1 or set(self.vc) | set(swept) != set(by.values()):
            raise ValueError('gain cell: one VL or none, every line sourced')
        comp = self.vl[swept[0]][0] if swept else max(c for _, c in self.vc.values())
        n = len(self.vl[swept[0]][1]) if swept else self.nr
        level = lambda ch, i: self.vl[ch][1][i] if ch in swept else self.vc[ch][0]
        dev = self.device_lookup()
        cols = {ch: ([], []) for ch in by.values()}
        for i in range(n):
            v = {t: level(by[t], i) for t in GC_LINES}
            if v['WWL'] - min(v['WBL'], self.gc_sn) > 1.0:
                self.gc_sn = v['WBL']
            else:                                   # write FET off: the node leaks slowly toward WBL
                self.gc_sn += (v['WBL'] - self.gc_sn) * 1e-3
            Ir, _, _ = fet_point(self.rng, dev, self.gc_sn - v['RBL'], v['RWL'] - v['RBL'], comp)
            cur = {'WWL': self.rng.gauss(0, 5e-14), 'WBL': self.rng.gauss(0, 5e-14), 'RWL': Ir, 'RBL': -Ir}
            for t in GC_LINES:
                cols[by[t]][0].append(('N', v[t] + self.rng.gauss(0, 1e-5)))
                cols[by[t]][1].append(('N', cur[t]))
        buf = {}
        for ch, (vs, is_) in cols.items():
            buf[self.ch[ch][0]], buf[self.ch[ch][1]] = vs, is_
        self.buf = {nm: r for nm, r in buf.items() if nm in self.li}
        self._end_test({ch: level(ch, n - 1) for ch in by.values()})

    # --- :PMU: (Segment Arb) ---
    @staticmethod
    def _pmu_split(c):
        head, _, rest = c.strip().partition(' ')
        return head.upper(), ([x.strip() for x in rest.split(',')] if rest.strip() else [])

    def _pch(self, a):
        ch = int(a)
        if ch < 1 or (ch - 1) // 2 + 1 not in self.pmu_cards:
            raise ValueError(f"no pulse channel {ch}")
        return ch

    @staticmethod
    def _pname(ch):
        return f"PMU{(ch - 1) // 2 + 1}-{(ch - 1) % 2 + 1}"

    def _pmu_write(self, c):
        head, a = self._pmu_split(c)
        self.pmu_used = True
        try:
            self._pmu_cmd(head, a)
        except (ValueError, IndexError, KeyError) as e:
            self._error(f"bad arguments in {c!r}: {e}", -993)

    def _pmu_cmd(self, head, a):
        if self.pmu_run and not (head == ':PMU:ABORT' or (head == ':PMU:OUTPUT:STATE' and a[1] == '0')):
            return self._error(f"{head}: command not valid during test execution", -980)
        if head == ':PMU:RPM:CONFIGURE':
            name, mode = a[0].upper(), int(a[1])
            if name not in self.rpm_names or mode not in (0, 1, 2, 3):
                raise ValueError(f"RPM {name} mode {mode}")
            self.rpm[name] = mode
            return
        if head == ':PMU:INIT':
            if a[0] not in ('0', '1'):
                raise ValueError('INIT mode')
            self.pmu = dict(mode=int(a[0]), seq={}, wlist={}, mrange={}, srange={}, load={}, rate=200e6, comp=set())
            self.pmu_data, self.pmu_out, self.pmu_final = {}, set(), {}
            self._update_bias()
            return
        if self.pmu is None:
            return self._error(f"{head} before :PMU:INIT", -991)
        pm = self.pmu
        if head == ':PMU:SOURCE:RANGE':
            v = float(a[1])
            if v not in (10.0, 40.0):
                raise ValueError('source range')
            pm['srange'][self._pch(a[0])] = v
        elif head == ':PMU:MEASURE:RANGE':
            ch, typ = self._pch(a[0]), int(a[1])
            if pm['mode'] == 1 and typ != 2:
                return self._error("Segment Arb supports fixed current ranges only", -967)
            r = float(a[2])
            if r not in self.PMU_RANGES:
                raise ValueError(f"range {r}")
            pm['mrange'][ch] = r
        elif head == ':PMU:LOAD':
            if not 1.0 <= float(a[1]) <= 1e7:
                raise ValueError('load')
            pm['load'][self._pch(a[0])] = float(a[1])
        elif head == ':PMU:SAMPLE:RATE':
            r = float(a[0])
            if r != int(r) or not 1e3 <= r <= 200e6:
                raise ValueError('rate')
            pm['rate'] = r
        elif head in (':PMU:CONNECTION:COMP', ':PMU:MEASURE:CONNECTION:COMP'):
            if int(a[1]) not in (1, 3):
                raise ValueError('compensation type')
            pm['comp'].add(self._pch(a[0]))
        elif head.startswith(':PMU:SARB:SEQ:'):
            key = head[len(':PMU:SARB:SEQ:'):]
            add = key.endswith(':ADD')
            key = key[:-4] if add else key
            conv = self.SARB_KEYS[key]
            ch, seq, vals = self._pch(a[0]), int(a[1]), a[2:]
            if not 1 <= seq <= 512:
                return self._error(f"sequence {seq}: maximum number of sequences exceeded", -960)
            if not 1 <= len(vals) <= 128:
                raise ValueError(f"{len(vals)} values (1..128 per command)")
            vals = [conv(v) for v in vals]
            d = pm['seq'].setdefault(ch, {}).setdefault(seq, {})
            if add:
                d[key] += vals                 # KeyError if the base command was not sent
            else:
                d[key] = vals
        elif head in (':PMU:SARB:WFM:SEQ:LIST', ':PMU:SARB:WFM:SEQ:LIST:ADD'):
            ch, vals = self._pch(a[0]), a[1:]
            if len(vals) % 2 or not 2 <= len(vals) <= 128:
                raise ValueError(f"{len(vals)} list values")
            pairs = [(int(vals[i]), int(float(vals[i + 1]))) for i in range(0, len(vals), 2)]
            for sq, loops in pairs:
                if 'STARTV' not in pm['seq'].get(ch, {}).get(sq, {}):
                    return self._error(f"sequence {sq} unknown on channel {ch}", -961)
                if not 1 <= loops <= 10 ** 12:
                    raise ValueError(f"loops {loops}")
            if head.endswith(':ADD'):
                pm['wlist'][ch] += pairs
            else:
                pm['wlist'][ch] = pairs
        elif head == ':PMU:OUTPUT:STATE':
            ch = self._pch(a[0])
            if a[1] not in ('0', '1'):
                raise ValueError('state')
            (self.pmu_out.add if a[1] == '1' else self.pmu_out.discard)(ch)
            self._update_bias()
        elif head == ':PMU:RETAIN:CONFIG':
            if a[1] not in ('0', '1'):
                raise ValueError('state')
            pm.setdefault('retain', set()).add(self._pch(a[0]))
        elif head == ':PMU:EXECUTE':
            self._pmu_execute()
        elif head == ':PMU:ABORT':
            if self.pmu_run:
                for ch in self.pmu_data:
                    self.pmu_data[ch] = self.pmu_data[ch][:self._pmu_count(ch)]
                self.pmu_run = None
            self.pmu_out.clear()
            self._update_bias()
        else:
            return self._error(f"unknown command {head}", -992)

    def _pmu_check(self, chans):
        """First broken rule as (message, code), or None."""
        pm = self.pmu
        if pm['mode'] != 1:
            return "only Segment Arb is simulated", -965
        if not chans:
            return "no pulse output enabled", -967
        for ch in chans:
            if self._pname(ch) in self.rpm_names and self.rpm.get(self._pname(ch)) != 0:
                return f"RPM {self._pname(ch)} is not in pulse mode", -967
            if ch not in pm['wlist']:
                return f"no sequence list on channel {ch}", -961
            ids = sorted(pm['seq'].get(ch, {}))
            if ids != list(range(1, len(ids) + 1)):      # live 2026-10-15: only id 2 defined -> -961
                return f"Unknown sequence (ids {ids} on channel {ch} are not 1..N)", -961
        ref = None
        for ch in chans:
            seqs, nseg, vr = pm['seq'].get(ch, {}), 0, pm['srange'].get(ch, 10.0)
            for sid, d in seqs.items():
                n = len(d.get('STARTV', []))
                if n < 3:
                    return f"ch {ch} sequence {sid}: {n} segments (at least 3)", -958
                if any(len(d.get(k, [0] * n)) != n for k in self.SARB_KEYS) or 'STOPV' not in d or 'TIME' not in d:
                    return f"ch {ch} sequence {sid}: array lengths differ", -962
                nseg += n
                for i in range(n):
                    t, v0, v1 = d['TIME'][i], d['STARTV'][i], d['STOPV'][i]
                    if not 20e-9 * (1 - 1e-9) <= t <= 1.0 or abs(round(t / 10e-9) * 10e-9 - t) > 1e-6 * 10e-9:
                        return f"ch {ch} sequence {sid} segment {i}: time {t!r}", -962
                    if i and v0 != d['STOPV'][i - 1]:
                        return f"ch {ch} sequence {sid} segment {i}: start != previous stop", -956
                    if v0 != v1 and abs(v1 - v0) / t < 362:
                        return f"ch {ch} sequence {sid} segment {i}: below minimum slew rate", -959
                    if max(abs(v0), abs(v1)) > vr:
                        return f"ch {ch} sequence {sid} segment {i}: |V| > {vr:g} V", -964
                    if d.get('MEAS:TYPE', [0] * n)[i]:
                        ms, me = d['MEAS:START'][i], d['MEAS:STOP'][i]
                        if not 0 <= ms < me <= t * (1 + 1e-12):
                            return f"ch {ch} sequence {sid} segment {i}: measure window outside segment", -957
            if nseg > 2048:
                return f"ch {ch}: {nseg} segments", -954
            prev = None
            for sid, loops in pm['wlist'][ch]:
                d = seqs[sid]
                if prev is not None and d['STARTV'][0] != prev:
                    return f"ch {ch}: sequence {sid} starts at {d['STARTV'][0]} after {prev}", -956
                if loops > 1 and d['STARTV'][0] != d['STOPV'][-1]:
                    return f"ch {ch}: looped sequence {sid} is not seamless", -956
                prev = d['STOPV'][-1]
            win = pts = 0
            for sid, loops in pm['wlist'][ch]:
                d = seqs[sid]
                for mt, ms, me in zip(d.get('MEAS:TYPE', []), d.get('MEAS:START', []), d.get('MEAS:STOP', [])):
                    if mt:
                        win += loops * (me - ms)
                        pts += loops
            if pts > 65536 or win * pm['rate'] >= 1e6:
                return f"ch {ch}: {pts} points / {win * pm['rate']:.3g} samples", -963
            shape = [(sid, loops, tuple(seqs[sid]['TIME'])) for sid, loops in pm['wlist'][ch]]
            if ref is not None and shape != ref:
                return "segment times or sequence lists differ between channels", -967
            ref = shape
        return None

    def _pmu_execute(self):
        chans = sorted(self.pmu_out)
        bad = self._pmu_check(chans)
        if bad:
            return self._error(*bad)
        if not self.bench.in_contact:
            raise SafetyViolation("PMU outputs on while the chuck is not at contact height")
        self.n_trigger += 1
        if self.fail_on_trigger and self.n_trigger == self.fail_on_trigger:
            raise OSError("VI_ERROR_TMO: injected 4200A fault")
        self.pmu_data = self._pmu_simulate(chans)
        self.pmu_final = {ch: self.pmu['seq'][ch][self.pmu['wlist'][ch][-1][0]]['STOPV'][-1] for ch in chans}
        self.pmu_run = dict(left=self.pmu_busy_polls, total=max(1, self.pmu_busy_polls))
        self._update_bias()

    GC_PMU_TAU_S = 100.0            # storage-node leak toward 0 V while the write FET is off

    def _pmu_simulate_gc(self, chans):
        """2T gain cell on 4 PMU channels (pmu_gc = WWL, WBL, RWL, RBL channels): every captured segment
        (MEAS:TYPE 2) is sampled at the capture rate (same <= ~4096-sample rule as below), a spot-mean segment
        (MEAS:TYPE 1) gives one point stamped at its window start; the storage node follows WBL while
        V_WWL - min(V_WBL, SN) > 1 V, else leaks toward 0 V with GC_PMU_TAU_S; read current = fet_point(SN,
        V_RWL - V_RBL), in at RWL, out at RBL; WWL/WBL see noise. Loops of unmeasured sequences are advanced
        in one step per segment."""
        pm = self.pmu
        wwl, wbl, rwl, rbl = self.pmu_gc
        if not set(self.pmu_gc) <= set(chans):
            return {ch: [] for ch in chans}
        dev = self.device_lookup()
        out = {ch: [] for ch in chans}
        W0 = pm['seq'][wwl]
        cap = sum(loops * (me - ms) for sid, loops in pm['wlist'][wwl]
                  for mt_, ms, me in zip(W0[sid].get('MEAS:TYPE', []), W0[sid].get('MEAS:START', []),
                                         W0[sid].get('MEAS:STOP', [])) if mt_ == 2)
        spot = sum(loops * (me - ms) for sid, loops in pm['wlist'][wwl]
                   for mt_, ms, me in zip(W0[sid].get('MEAS:TYPE', []), W0[sid].get('MEAS:START', []),
                                          W0[sid].get('MEAS:STOP', [])) if mt_ == 1)
        n_div = 1
        while (cap or spot) and 200e6 / n_div > min(pm['rate'], 4096 / (cap or spot)):
            n_div += 1
        rate = 200e6 / n_div
        t = 0.0

        def pt(v, i, ch, tc, n_avg=1):
            rng_a = pm['mrange'].get(ch, 1e-2)
            i = max(-1.05 * rng_a, min(1.05 * rng_a, i + self.rng.gauss(0, self.PMU_RANGES[rng_a] / n_avg ** 0.5)))
            return f"{v:.6g},{i:.6g},{tc:.9g},01020021"

        def step(v, dt=0.0):
            if v[0] - min(v[1], self.gc_sn) > 1.0:
                self.gc_sn = v[1]
            elif dt:
                self.gc_sn *= math.exp(-dt / self.GC_PMU_TAU_S)

        for sid, loops in pm['wlist'][wwl]:
            S = [pm['seq'][ch][sid] for ch in self.pmu_gc]
            T, mt = S[0]['TIME'], S[0].get('MEAS:TYPE', [0] * len(S[0]['TIME']))
            if not any(mt):                      # unmeasured (e.g. looped 1 ms holds): one step per segment
                for i, ti in enumerate(T):
                    step([d['STOPV'][i] for d in S], ti * loops)
                t += sum(T) * loops
                continue
            for _ in range(loops):
                for i, ti in enumerate(T):
                    v0 = [d['STARTV'][i] for d in S]
                    v1 = [d['STOPV'][i] for d in S]
                    if mt[i] == 2:
                        ms, me = S[0]['MEAS:START'][i], S[0]['MEAS:STOP'][i]
                        for j in range(max(1, int(round((me - ms) * rate)))):
                            tj = ms + j / rate
                            v = [a + (b - a) * tj / ti for a, b in zip(v0, v1)]
                            step(v)
                            Ir, _, _ = fet_point(self.rng, dev, self.gc_sn - v[3], v[2] - v[3], 1.0)
                            for ch, vv, ii in ((wwl, v[0], 0.0), (wbl, v[1], 0.0), (rwl, v[2], Ir), (rbl, v[3], -Ir)):
                                out[ch].append(pt(vv, ii, ch, t + tj))
                    elif mt[i]:
                        ms, me = S[0]['MEAS:START'][i], S[0]['MEAS:STOP'][i]
                        step(v0)
                        Ir, _, _ = fet_point(self.rng, dev, self.gc_sn - v0[3], v0[2] - v0[3], 1.0)
                        n_avg = max(1, int(round((me - ms) * rate)))
                        for ch, vv, ii in ((wwl, v0[0], 0.0), (wbl, v0[1], 0.0), (rwl, v0[2], Ir), (rbl, v0[3], -Ir)):
                            out[ch].append(pt(vv, ii, ch, t + ms, n_avg))
                    step(v1, ti)
                    t += ti
        return out

    def _pmu_simulate(self, chans):
        if self.pmu_gc:
            return self._pmu_simulate_gc(chans)
        pm, (A, n_exp, r, tau, vth) = self.pmu, self.stress_model
        g, dch, sch = self.pmu_roles
        if not {g, dch, sch} <= set(chans):
            return {ch: [] for ch in chans}
        dev = self.device_lookup()
        out = {ch: [] for ch in chans}
        t, dose, last = 0.0, 0.0, None

        def pt(v, i, rng_a, tc, code='01010021'):
            i += self.rng.gauss(0, self.PMU_RANGES[rng_a])
            i = max(-1.05 * rng_a, min(1.05 * rng_a, i))
            return f"{v:.6g},{i:.6g},{tc:.6g},{code}"

        # waveform capture (MEAS:TYPE 2): the rate rule seen live for spot means (<= ~4096 samples over all
        # windows) is assumed to hold for captures too; unverified until L1
        G0 = pm['seq'][g]
        cap = sum(loops * (me - ms) for sid, loops in pm['wlist'][g]
                  for mt_, ms, me in zip(G0[sid].get('MEAS:TYPE', []), G0[sid].get('MEAS:START', []),
                                         G0[sid].get('MEAS:STOP', [])) if mt_ == 2)
        cap_rate = pm['rate']
        if cap:
            n_div = 1
            while 200e6 / n_div > min(pm['rate'], 4096 / cap):
                n_div += 1
            cap_rate = 200e6 / n_div

        for sid, loops in pm['wlist'][g]:
            G, D, S = pm['seq'][g][sid], pm['seq'][dch][sid], pm['seq'][sch][sid]
            T, mt = G['TIME'], G.get('MEAS:TYPE', [0] * len(G['TIME']))
            stress = [ti if (v0 == v1 and v0 >= vth) else 0.0 for ti, v0, v1 in zip(T, G['STARTV'], G['STOPV'])]
            dur = sum(T)
            if not any(mt):
                if any(stress):
                    k = max(i for i, x in enumerate(stress) if x)
                    last = t + (loops - 1) * dur + sum(T[:k + 1])
                    dose += sum(stress) * loops
                t += dur * loops
                continue
            for _ in range(loops):
                for i, ti in enumerate(T):
                    if mt[i] == 2:
                        vg, vd, vs = G['STARTV'][i], D['STARTV'][i], S['STARTV'][i]
                        dvt = 0.0 if not dose else                             A * dose ** n_exp * max(0.0, 1 - r * math.log10(1 + (t - last) / tau))
                        Id, Ig, _ = fet_point(self.rng, dev, vg - dvt, vd - vs, 1.0)
                        step = vg - G['STARTV'][i - 1] if i else 0.0      # gate step into this plateau
                        ms, me = G['MEAS:START'][i], G['MEAS:STOP'][i]
                        for j in range(max(1, int(round((me - ms) * cap_rate)))):
                            tj = ms + j / cap_rate
                            tr = 2e-7 * step * math.exp(-tj / 1e-6)      # displacement current, tau 1 us
                            code = '01020021'
                            out[g].append(pt(vg, Ig + tr, pm['mrange'].get(g, 1e-2), t + tj, code))
                            out[dch].append(pt(vd, Id - 0.5 * tr, pm['mrange'].get(dch, 1e-2), t + tj, '01020022'))
                            out[sch].append(pt(vs, -Id - 0.5 * tr, pm['mrange'].get(sch, 1e-2), t + tj, code))
                    elif mt[i]:
                        tc = t + (G['MEAS:START'][i] + G['MEAS:STOP'][i]) / 2
                        vg, vd, vs = G['STARTV'][i], D['STARTV'][i], S['STARTV'][i]
                        dvt = 0.0 if not dose else \
                            A * dose ** n_exp * max(0.0, 1 - r * math.log10(1 + (tc - last) / tau))
                        Id, Ig, _ = fet_point(self.rng, dev, vg - dvt, vd - vs, 1.0)
                        ts = t + G['MEAS:START'][i]          # the real 4200A stamps the window start
                        out[g].append(pt(vg, Ig, pm['mrange'].get(g, 1e-2), ts))
                        out[dch].append(pt(vd, Id, pm['mrange'].get(dch, 1e-2), ts))
                        out[sch].append(pt(vs, -Id, pm['mrange'].get(sch, 1e-2), ts))
                    if stress[i]:
                        dose += ti
                        last = t + ti
                    t += ti
        return out

    def _pmu_count(self, ch):
        pts = self.pmu_data.get(ch, [])
        if self.pmu_run:
            return len(pts) * (self.pmu_run['total'] - self.pmu_run['left']) // self.pmu_run['total']
        return len(pts)

    def _pmu_query(self, c):
        head, a = self._pmu_split(c)
        if head == ':PMU:TEST:STATUS?':
            if self.pmu_run:
                if self.pmu_run['left'] > 0:
                    self.pmu_run['left'] -= 1
                    return '1'
                self.pmu_run = None
                self._update_bias()
            return '0'
        try:
            if head == ':PMU:DATA:COUNT?':
                return str(self._pmu_count(self._pch(a[0])))
            if head == ':PMU:DATA:GET':
                ch = self._pch(a[0])
                start = int(a[1]) if len(a) > 1 else 0
                n = int(a[2]) if len(a) > 2 else 2048
                if not 1 <= n <= 2048 or start < 0:
                    raise ValueError('numPoints 1..2048')
                return ';'.join(self.pmu_data.get(ch, [])[start:min(start + n, self._pmu_count(ch))])
        except (ValueError, IndexError) as e:
            self._error(f"bad arguments in {c!r}: {e}", -993)
            return ''
        self._error(f"unknown query {head!r}")
        return ''

    def clear(self):
        self._log('<device clear>')

    def close(self):
        self.bench.log.append("K4200: <close>")
