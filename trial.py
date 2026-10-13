"""40-day trial + activation key logic. Pure functions, no Flask or database imports.

How it works
  * The first start with LICENSE_SECRET set creates ONE row in the `license` table:
    random install_id, start time, last_seen, activated flag, and a signature.
  * The signature is HMAC-SHA256(LICENSE_SECRET, row fields). LICENSE_SECRET lives in the
    server environment, never in the repo or the database, so editing the row by hand
    (moving the start date, flipping `activated`) breaks the signature and locks the app.
  * The activation key is HMAC(LICENSE_SECRET, install_id), so only someone holding the
    secret can make a key, and a key only works for that one installation.
  * Database triggers (below) also refuse deleting the row, changing install_id or
    started_at, un-activating, or moving last_seen backwards.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%dT%H:%M:%SZ"
TOUCH_EVERY = timedelta(hours=1)  # how often last_seen is refreshed (clock-rollback guard)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(dt):
    return dt.strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def _mac(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sign(secret, install_id, started_at, last_seen, activated):
    return _mac(secret, f"row|{install_id}|{started_at}|{last_seen}|{int(activated)}")


def activation_key(secret, install_id):
    h = _mac(secret, f"activate|{install_id}")[:20].upper()
    return "-".join(h[i:i + 4] for i in range(0, 20, 4))


def _norm(k):
    return "".join(ch for ch in str(k).upper() if ch.isalnum())


def key_ok(secret, install_id, entered):
    if not secret:
        return False
    want = _norm(activation_key(secret, install_id))
    return hmac.compare_digest(want.encode(), _norm(entered).encode())


def evaluate(row, secret, at, trial_days):
    """row: dict(install_id, started_at, last_seen, activated, sig).
    Returns {"open": bool, "reason": licensed|trial|expired|tampered|unconfigured, "days_left": int}."""
    def out(is_open, reason, days=0):
        return {"open": is_open, "reason": reason, "days_left": days}

    if not secret:
        return out(False, "unconfigured")
    try:
        good = hmac.compare_digest(
            sign(secret, row["install_id"], row["started_at"], row["last_seen"], row["activated"]),
            str(row["sig"]))
        if not good:
            return out(False, "tampered")
        if row["activated"]:
            return out(True, "licensed")
        started, seen = parse(row["started_at"]), parse(row["last_seen"])
    except (KeyError, ValueError, TypeError):
        return out(False, "tampered")
    effective = max(at, seen)  # a rolled-back server clock cannot extend the trial
    left = started + timedelta(days=trial_days) - effective
    if left.total_seconds() <= 0:
        return out(False, "expired")
    return out(True, "trial", int(-(-left.total_seconds() // 86400)))


def is_stale(last_seen, at):
    return at - parse(last_seen) >= TOUCH_EVERY


SQLITE_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS license_no_delete BEFORE DELETE ON license
       BEGIN SELECT RAISE(ABORT, 'license row is protected'); END""",
    """CREATE TRIGGER IF NOT EXISTS license_no_tamper BEFORE UPDATE ON license
       WHEN NEW.install_id <> OLD.install_id OR NEW.started_at <> OLD.started_at
         OR (OLD.activated = 1 AND NEW.activated = 0) OR NEW.last_seen < OLD.last_seen
       BEGIN SELECT RAISE(ABORT, 'license row is protected'); END""",
]

POSTGRES_TRIGGERS = [
    """CREATE OR REPLACE FUNCTION license_guard() RETURNS trigger AS $$
       BEGIN
         IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'license row is protected'; END IF;
         IF NEW.install_id <> OLD.install_id OR NEW.started_at <> OLD.started_at
            OR (OLD.activated = 1 AND NEW.activated = 0) OR NEW.last_seen < OLD.last_seen THEN
           RAISE EXCEPTION 'license row is protected';
         END IF;
         RETURN NEW;
       END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS license_guard_trg ON license",
    """CREATE TRIGGER license_guard_trg BEFORE UPDATE OR DELETE ON license
       FOR EACH ROW EXECUTE FUNCTION license_guard()""",
]
