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
    1e-12 A with a preamp, 1e-7 A without."""
    PAGES = {'DE': {'CH', 'VS', 'VM'}, 'SS': {'VL', 'VC', 'HT', 'DT', 'ST', 'RP'},
             'SM': {'DM', 'LI', 'XN', 'YA', 'YB'}, 'MD': {'ME'}, 'US': {'DV'}}
    MODELESS = {'BC', 'DR', 'IT', 'RS', 'RG'}

    def __init__(self, bench, device_lookup=lambda: (10.0, 2.0), fail_on_trigger=None, seed=0,
                 opt='SMUPA1,SMUPA2,SMUPA3,SMU4,,,,', busy_polls=2):
        self.bench, self.device_lookup, self.fail_on_trigger = bench, device_lookup, fail_on_trigger
        self.rng, self.opt, self.busy_polls = random.Random(seed), opt, busy_polls
        self.preamp = {int(m.group(3)): bool(m.group(2)) for m in re.finditer(r'(HP)?SMU(PA)?(\d+)', opt)}
        self.n_smu = max(self.preamp, default=0)
        self.page, self.timeout, self.n_trigger = None, 60000, 0
        self.stb, self.err, self.dr, self.dm, self.li = 0, '', False, None, []
        self.ch, self.vl, self.vc, self.standby, self.out = {}, {}, {}, set(), {}
        self.buf, self._busy = {}, 0

    def _log(self, s):
        self.bench.log.append(f"K4200: {s}")

    def _update_bias(self):
        self.bench.bias_live = any(abs(v) > 0 for v in self.out.values())

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

    def query(self, cmd):
        self._log(cmd)
        c = cmd.strip()
        if c == '*IDN?':
            return 'KEITHLEY INSTRUMENTS,KI4200A,FAKE,V0.0'
        if c == '*OPT?':
            return self.opt
        if c == ':ERROR:LAST:GET':
            return self.err or 'No error. (0)'
        if c == ':ERROR:LAST:CLEAR':        # no reply over GPIB: a query of it times out on the real tool
            raise TimeoutError("VI_ERROR_TMO: :ERROR:LAST:CLEAR sends no reply")
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
        if a[1] not in ('0', '1', '2', '3'):
            raise ValueError('RP')

    def _DM(self, a):
        self.dm = int(a[0])

    def _LI(self, a):
        self.li = [x.strip("'") for x in a]

    def _XN(self, a):
        pass

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
        last = {g: vg[-1], d: vd, s: vs}
        for ch in self.ch:
            self.out[ch] = 0.0 if ch in self.standby else last.get(ch, 0.0)
        self._update_bias()
        self._busy = self.busy_polls
        if not self._busy:
            self._done()

    def clear(self):
        self._log('<device clear>')

    def close(self):
        self.bench.log.append("K4200: <close>")
