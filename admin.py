"""Read-only reports and clean-up for Brave staff. Run on the server:

    python admin.py stats                 totals, scores per level, per day
    python admin.py requests              support requests (contains contacts!)
    python admin.py purge --days 30       delete support requests older than N days
    python admin.py license               trial status, days left, install id
    python admin.py starttrial            start the clock on a site that already has check-ins
    python admin.py genkey                print the activation key for this install
                                          (needs LICENSE_SECRET in the environment)
    python admin.py hashpw                choose the password for the /admin dashboard and print
                                          the ADMIN_PASSWORD_HASH value to put on the server
"""
import argparse
import getpass
from datetime import datetime, timedelta

from sqlalchemy import func

import trial
from app import EAT, Checkin, SupportRequest, app, db, read_state, start_trial, License


def stats():
    total = db.session.query(func.count(Checkin.id)).scalar()
    print(f"Check-ins: {total}")
    labels = {1: "Thriving", 2: "Holding Steady", 3: "Noticing Strain", 4: "Struggling", 5: "In Crisis"}
    rows = dict(db.session.query(Checkin.score, func.count()).group_by(Checkin.score).all())
    for s in range(1, 6):
        print(f"  {s} {labels[s]:<16}{rows.get(s, 0)}")
    print("Per day:")
    for day, n in (db.session.query(Checkin.day, func.count()).group_by(Checkin.day)
                   .order_by(Checkin.day.desc()).limit(14)):
        print(f"  {day}  {n}")


def requests_():
    for r in SupportRequest.query.order_by(SupportRequest.id):
        print(f"{r.day} | {r.channel} | {r.support_type} | {r.contact}")


def purge(days):
    cutoff = (datetime.now(EAT) - timedelta(days=days)).date().isoformat()
    n = SupportRequest.query.filter(SupportRequest.day < cutoff).delete()
    db.session.commit()
    print(f"Deleted {n} support request(s) older than {days} days.")


def license_():
    s = read_state(app.config["LICENSE_SECRET"], app.config["TRIAL_DAYS"])
    print(f"State: {s['reason']}  (site {'open' if s['open'] else 'LOCKED'})")
    if s["reason"] == "trial":
        print(f"Days left: {s['days_left']}")
    row = db.session.get(License, 1)
    if row:
        print(f"Install id: {row.install_id}\nStarted:    {row.started_at}")


def starttrial():
    """Only needed when check-ins already exist but there is no licence row (first upgrade)."""
    secret = app.config["LICENSE_SECRET"]
    if not secret:
        raise SystemExit("Set LICENSE_SECRET first.")
    if db.session.get(License, 1):
        raise SystemExit("A trial already exists; nothing changed.")
    start_trial(secret)
    license_()


def genkey():
    secret = app.config["LICENSE_SECRET"]
    row = db.session.get(License, 1)
    if not secret or row is None:
        raise SystemExit("Set LICENSE_SECRET and start the app once first.")
    print(trial.activation_key(secret, row.install_id))


def hashpw():
    from werkzeug.security import generate_password_hash
    pw = getpass.getpass("New admin password (min 12 characters): ")
    if len(pw) < 12:
        raise SystemExit("Too short: use at least 12 characters.")
    if getpass.getpass("Repeat it: ") != pw:
        raise SystemExit("The two entries differ; nothing printed.")
    print("\nSet this in the server environment (PythonAnywhere: the WSGI file), single-quoted:")
    print(f"  ADMIN_PASSWORD_HASH='{generate_password_hash(pw)}'")
    print("You also need ADMIN_SECRET_KEY, e.g.:  python -c \"import secrets; print(secrets.token_hex(32))\"")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("requests")
    sub.add_parser("license")
    sub.add_parser("genkey")
    sub.add_parser("starttrial")
    sub.add_parser("hashpw")
    pg = sub.add_parser("purge")
    pg.add_argument("--days", type=int, required=True)
    a = p.parse_args()
    if a.cmd == "hashpw":  # only asks for a password; no query is run
        hashpw()
        raise SystemExit(0)
    with app.app_context():
        if a.cmd == "stats":
            stats()
        elif a.cmd == "requests":
            requests_()
        elif a.cmd == "license":
            license_()
        elif a.cmd == "starttrial":
            starttrial()
        elif a.cmd == "genkey":
            genkey()
        else:
            purge(a.days)
