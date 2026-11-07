# 0004: Email notifications from live runs

**Decision.** `measure.run_map.run` emails the end of every live run, and optionally each streak of
`alert_after_fails` devices in a row that were not measured. It sends through `measure.notify`, which uses the
standard library's `smtplib` with STARTTLS. The default is Gmail with an app password. The settings are
`NOTIFY_*` keys in `.env`.

**Why.** Full maps run unattended for a day. On wd_bobafet_6802, a probing fault failed eight devices in a row
before anyone looked. The standard library is enough for plain-text mail, so there is no new dependency, and
the hook in `run` covers the CLI and the web app.

**Rules.**
- Live runs only; a dry run sends nothing unless a test injects `notify_fn`.
- A send that fails is logged and never stops the run or its cleanup. The email goes out after the outputs are
  zeroed and the probes separated.
- The password lives only in `.env` (gitignored); it is never logged.

**Cost.** Nothing is sent if the process dies (a closed window, a power cut, a kill). A slow SMTP server can
delay the end of a run by up to its 30 s timeout per email.
