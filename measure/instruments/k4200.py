"""Keithley 4200A-SCS over KXCI (GPIB), system mode.

Adapted from S200_B1500A_4200A_py_setup/keithley4200_utils/k4200_helpers.py (idvg_sweep_smu), with
these changes, following the KXCI manual 4200A-KXCI-907-01E (page numbers in brackets):
- auto-standby (ST) on every channel of the test, not only the gate, so no output stays on at the
  end of a test [5-14]; outputs are also turned off explicitly afterwards in user mode (US; DVn) [5-34]
- SMUs not in the test are undefined (CHn with no parameters) [5-7]; VS1/VS2/VM1/VM2 likewise [5-8]
- list display (DM2 + LI) names every measured function; with DM1 only the graph's X/Y1/Y2
  are measured, so IS was never read [5-22, 5-23, 5-38]
- source terminal named VSRC/ISRC: DO treats a trailing T/S as timestamp/status [5-38]
- the status byte is checked after every command (B1 = syntax error, read with :ERROR:LAST:GET)
  and the wait acts on the first B0 (data ready) seen, since a serial poll clears it [3-3, 4-2].
  Argument errors (-993) do NOT set B1 (seen live 2026-10-15: 'RG n, 1e-09' without preamps was
  rejected silently), so :ERROR:LAST:GET is also compared with its no-error reply after each sweep
  setup and data read
- check_settings() refuses an Irange below 100 nA on SMUs that *OPT? lists without a preamp [5-42]
- the DO status letter of every reading is kept (C/T compliance, V overflow, X oscillation) [3-1]
Nothing here touches hardware at import time; connect() must be called explicitly.
"""
import math
import re
import time

RESOURCE_STR = 'GPIB0::18::INSTR'
STB_DATA_READY, STB_SYNTAX_ERROR, STB_BUSY = 1, 2, 16


class K4200Error(RuntimeError):
    pass


def connect(resource_str=RESOURCE_STR, **settings):
    """Open the real KXCI GPIB session (sends *IDN?, *OPT?). settings -> K4200 (config 'k4200' block)."""
    import pyvisa
    rm = pyvisa.ResourceManager()
    res = rm.open_resource(resource_str)
    return K4200(res, rm=rm, **settings)


def rpm_of_smu(smu):
    """RPM wiring on this system (k4200_helpers.map_rpm_to_smu): SMU1 PMU1-1, SMU2 PMU1-2, SMU3 PMU2-1, SMU4 PMU2-2."""
    return f"PMU{math.ceil(smu / 2)}-{2 if smu % 2 == 0 else 1}"


class K4200:
    """integ_time: 1 = 0.1 PLC, 2 = 1 PLC (Normal), 3 = 10 PLC, or [delay factor, filter factor, PLC] for
    IT4 [5-40]. hold_time_s / delay_time_s: HT / DT [5-12, 5-10]. resolution: RS digits, None = leave.
    poll_s / sweep_timeout_s: wait for data ready. timeout_ms: VISA I/O timeout."""

    def __init__(self, res, rm=None, integ_time=2, hold_time_s=0.0, delay_time_s=0.0, resolution=None,
                 poll_s=0.2, sweep_timeout_s=900.0, timeout_ms=60000, log=print):
        self.res, self.rm, self.log = res, rm, log
        res.timeout = timeout_ms
        self.integ_time, self.hold, self.delay = integ_time, hold_time_s, delay_time_s
        self.resolution, self.poll_s, self.sweep_timeout_s = resolution, poll_s, sweep_timeout_s
        self.idn = res.query('*IDN?').strip()
        self.opt = res.query('*OPT?').strip()
        log(f"4200A: {self.idn} | *OPT? {self.opt}")
        self.rpms = 'RPM' in self.opt.upper()
        # *OPT? slot entries: SMUn / HPSMUn without, SMUPAn / HPSMUPAn with a preamp [4-4]
        self.preamp = {int(m.group(3)): bool(m.group(2))
                       for m in re.finditer(r'(HP)?SMU(PA)?(\d+)', self.opt.upper())}
        self.n_smu = max(self.preamp, default=0)
        self.rpm_mode, self.skip, self.channels = {}, set(), None
        self.data_ready = False
        self.read_stb()                       # clear any stale bits
        self.clear_error()
        self.no_error = self._no_error_reply()

    # --- low level ---
    def read_stb(self):
        """Serial poll. It clears B0, so a data-ready bit seen by any poll (also the syntax check right
        after ME1, if the test is already done) is latched in self.data_ready."""
        stb = int(self.res.read_stb())
        self.data_ready |= bool(stb & STB_DATA_READY)
        return stb

    def last_error(self):
        try:
            return self.res.query(':ERROR:LAST:GET').strip()
        except Exception as e:          # keep the original error visible even if this read fails
            return f"(no reply to :ERROR:LAST:GET: {type(e).__name__})"

    def clear_error(self):
        """A write, not a query: over GPIB the 4200A sends no reply to it (a query timed out live,
        2026-10-15). Only Ethernet answers ACK."""
        self.res.write(':ERROR:LAST:CLEAR')

    def _no_error_reply(self):
        """What :ERROR:LAST:GET returns right after a clear, i.e. 'no error' on this firmware. None (and
        check_errors() off) if it does not answer within 5 s."""
        old = self.res.timeout
        self.res.timeout = 5000
        try:
            reply = self.res.query(':ERROR:LAST:GET').strip()
            self.log(f"4200A: no-error reply of :ERROR:LAST:GET = {reply!r}")
            return reply
        except Exception as e:
            self.log(f"4200A: WARNING no reply to :ERROR:LAST:GET ({type(e).__name__}); argument errors "
                     "(-993) will not be detected")
            return None
        finally:
            self.res.timeout = old

    def check_errors(self, context):
        """Raise if KXCI logged an error since the last clear (catches -993, which sets no status bit)."""
        if self.no_error is None:
            return
        err = self.res.query(':ERROR:LAST:GET').strip()
        if err != self.no_error:
            self.clear_error()
            raise K4200Error(f"KXCI error during {context}: {err}")

    def write(self, cmd, optional=False):
        """Send one command and check the syntax-error bit. optional=True logs the error once and
        never sends that command again."""
        if cmd in self.skip:
            return
        self.res.write(cmd)
        if self.read_stb() & STB_SYNTAX_ERROR:
            err = self.last_error()
            self.clear_error()
            if not optional:
                raise K4200Error(f"KXCI rejected {cmd!r}: {err}")
            self.skip.add(cmd)
            self.log(f"4200A: optional {cmd!r} rejected ({err}); not sent again")

    def query(self, cmd):
        reply = self.res.query(cmd).strip()
        if self.read_stb() & STB_SYNTAX_ERROR:
            err = self.last_error()
            self.clear_error()
            raise K4200Error(f"KXCI rejected {cmd!r}: {err}")
        return reply

    def set_rpm(self, smus, mode):
        """mode 2 = SMU, 0 = pulse [7-68]. Only sent when *OPT? lists RPMs and the mode changes."""
        if not self.rpms:
            return
        todo = [s for s in smus if self.rpm_mode.get(s) != mode]
        if todo:
            self.write('SS')
            for s in todo:
                self.write(f"RP {rpm_of_smu(s)}, {mode}")
                self.rpm_mode[s] = mode

    # --- system-mode list sweep ---
    def list_sweep(self, gate, drain, source, vg_list, vd, comp, irange):
        """VAR1 voltage list on the gate, constant Vd on the drain, 0 V on the source. Returns
        {name: [(status_letter, value), ...]} for VG, IG, ID, ISRC."""
        if not 0 < len(vg_list) <= 4096:
            raise ValueError(f"list sweep needs 1..4096 points, got {len(vg_list)}")
        chans = (gate, drain, source)
        self.write('BC')
        self.clear_error()
        self.write('DE')
        self.write(f"CH{gate}, 'VG', 'IG', 1, 1")
        self.write(f"CH{drain}, 'VD', 'ID', 1, 3")
        self.write(f"CH{source}, 'VSRC', 'ISRC', 1, 3")
        for ch in range(1, self.n_smu + 1):
            if ch not in chans:
                self.write(f"CH{ch}")
        for cmd in ('VS1', 'VS2', 'VM1', 'VM2'):
            self.write(cmd, optional=True)
        self.set_rpm(chans, 2)
        self.write('SS')
        self.write(f"VL{gate}, 1, {_num(comp)}, " + ', '.join(_num(v, 4) for v in vg_list))
        self.write(f"VC{drain}, {_num(vd)}, {_num(comp)}")
        self.write(f"VC{source}, 0, {_num(comp)}")
        self.write(f"HT {_num(self.hold)}")
        self.write(f"DT {_num(self.delay)}")
        for ch in chans:
            self.write(f"ST {ch}, 1")
        it = self.integ_time
        self.write(f"IT{it}" if isinstance(it, int) else "IT4, " + ', '.join(_num(x) for x in it))
        if self.resolution:
            self.write(f"RS {int(self.resolution)}")
        for ch in chans:
            self.write(f"RG {ch}, {_num(irange)}")
        self.write('SM')
        self.write('DM2')
        self.write("LI 'VG', 'IG', 'ID', 'ISRC'")
        self.write('DR1')
        self.write('MD')
        self.check_errors('sweep setup')
        self.data_ready = False
        self.write('ME1')
        self.wait_data_ready()
        out = {n: parse_do(self.query(f"DO '{n}'")) for n in ('VG', 'IG', 'ID', 'ISRC')}
        self.check_errors('test / data read')
        return out

    def wait_data_ready(self):
        t0 = time.time()
        while not self.data_ready:
            time.sleep(self.poll_s)
            stb = self.read_stb()
            if stb & STB_SYNTAX_ERROR:
                raise K4200Error(f"KXCI error during test: {self.last_error()}")
            if self.data_ready:
                return
            if time.time() - t0 > self.sweep_timeout_s:
                self.res.write('ME4')          # abort the test
                raise K4200Error(f"no data ready after {self.sweep_timeout_s:g} s (stb={stb}); test aborted")

    def outputs_off(self):
        """User mode, DVn with no parameters disables that SMU output [5-34]; drain/source before gate.
        Before get_smus() has set the test channels, every SMU listed by *OPT? is turned off."""
        self.write('US')
        for ch in self.channels or range(1, self.n_smu + 1):
            self.write(f"DV{ch}")

    def close(self):
        try:
            self.set_rpm(list(self.rpm_mode), 0)    # RPMs back to pulse mode
        finally:
            self.res.close()
            if self.rm is not None:
                self.rm.close()


_DO_ITEM = re.compile(r'^\s*([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[Ee][-+]?\d+)?)\s*$')


def parse_do(raw):
    """'N-1.0000E+00,C+5.0000E-03,...' (status letter, optional space, value) -> [(letter, float), ...]."""
    out = []
    for item in raw.strip().strip(',').split(','):
        if not item.strip():
            continue
        m = _DO_ITEM.match(item)
        if not m:
            raise K4200Error(f"Unparseable DO reading {item!r}")
        out.append((m.group(1), float(m.group(2))))
    return out


def _num(v, decimals=None):
    """KXCI numbers: at most 12 characters, 2-digit exponent [5-6]."""
    v = float(v)
    if decimals is not None:
        s = f"{round(v, decimals) + 0.0:.{decimals}f}"
    else:
        s = f"{v:.6g}"
    if len(s) > 12:
        raise ValueError(f"number {v!r} does not fit the 12-character KXCI field")
    return s


def get_smus(k, mapping):
    """mapping like {'gate': 'smu1', 'drain': 'smu2', 'source': 'smu3'} -> (1, 2, 3)."""
    chans = []
    for t in ('gate', 'drain', 'source'):
        m = re.fullmatch(r'smu(\d)', str(mapping[t]).strip().lower())
        if not m:
            raise ValueError(f"smu_map {t}: {mapping[t]!r} is not 'smuN'")
        chans.append(int(m.group(1)))
    if len(set(chans)) != 3:
        raise ValueError(f"smu_map uses an SMU twice: {mapping}")
    k.channels = (chans[1], chans[2], chans[0])   # outputs_off order: drain, source, gate
    return tuple(chans)


def check_settings(k, smus, meas):
    """Before the run: the lowest autorange (RG) is 1 pA with a preamp, 100 nA without [5-42]."""
    lo = float(meas['Irange'])
    for ch in smus:
        if ch not in k.preamp:
            raise K4200Error(f"SMU{ch} is not in *OPT? ({k.opt})")
        if not k.preamp[ch] and lo < 1e-7:
            raise K4200Error(f"Irange {lo:g} A needs a preamp, but *OPT? lists SMU{ch} without one ({k.opt}); "
                             "the lowest range there is 1e-07 A")


def zero_outputs(k):
    """Turn all three test SMU outputs off (user mode DVn)."""
    k.outputs_off()


def close(k):
    k.close()
