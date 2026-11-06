"""Email notifications for live runs (run end, N failures in a row), over SMTP with STARTTLS.

Settings come from the environment, i.e. the repo's gitignored .env, which the Makefile exports:
    NOTIFY_SMTP_USER       the sending account, e.g. a Gmail address
    NOTIFY_SMTP_PASSWORD   its password; for Gmail a 16-character app password (no spaces)
    NOTIFY_EMAIL_TO        recipients, comma separated
    NOTIFY_SMTP_HOST       default smtp.gmail.com
    NOTIFY_SMTP_PORT       default 587
    NOTIFY_EMAIL_FROM      default NOTIFY_SMTP_USER
Email is off when any of the first three is missing. The password is never logged or printed.

Send one test email (lab PC):  make notify-test PY=...
"""
import argparse
import os
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

REQUIRED = ('NOTIFY_SMTP_USER', 'NOTIFY_SMTP_PASSWORD', 'NOTIFY_EMAIL_TO')
TIMEOUT_S = 30


class Mailer:
    def __init__(self, user, password, to, host='smtp.gmail.com', port=587, sender=None):
        self.user, self._password = user, password
        self.to = [a.strip() for a in to.split(',') if a.strip()]
        self.host, self.port = host, int(port)
        self.sender = sender or user

    def __repr__(self):
        return f"Mailer({self.describe()})"

    def describe(self):
        return f"to {', '.join(self.to)} via {self.host}:{self.port} as {self.user}"

    def send(self, subject, body):
        """Send one plain-text email; raises on any SMTP or network error."""
        msg = EmailMessage()
        msg['From'], msg['To'], msg['Subject'] = self.sender, ', '.join(self.to), subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_S) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(self.user, self._password)
            s.send_message(msg)


def from_env(env=None):
    """-> Mailer, or None when a required setting is missing."""
    env = os.environ if env is None else env
    if not all(env.get(k, '').strip() for k in REQUIRED):
        return None
    return Mailer(env['NOTIFY_SMTP_USER'].strip(), env['NOTIFY_SMTP_PASSWORD'].strip(), env['NOTIFY_EMAIL_TO'],
                  host=env.get('NOTIFY_SMTP_HOST', '').strip() or 'smtp.gmail.com',
                  port=env.get('NOTIFY_SMTP_PORT', '').strip() or 587,
                  sender=env.get('NOTIFY_EMAIL_FROM', '').strip() or None)


def missing(env=None):
    """The required settings that are not set (for the 'email off' log line)."""
    env = os.environ if env is None else env
    return [k for k in REQUIRED if not env.get(k, '').strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--test', action='store_true', help='send one test email')
    a = ap.parse_args(argv)
    m = from_env()
    if m is None:
        print(f"Email is off: {', '.join(missing())} not set (add them to .env)")
        return 1
    print(f"Email settings: {m.describe()}")
    if a.test:
        m.send('[probe] test email', f"Test email from measure.notify, sent {time.strftime('%Y-%m-%d %H:%M:%S')}.\n"
                                     "Live runs will email here when they end.")
        print("Test email sent.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
