"""Read-only reports and clean-up for Brave staff. Run on the server:

    python admin.py stats                 totals, scores per level, per day
    python admin.py requests              support requests (contains contacts!)
    python admin.py purge --days 30       delete support requests older than N days
"""
import argparse
from datetime import datetime, timedelta

from sqlalchemy import func

from app import EAT, Checkin, SupportRequest, app, db


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


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    sub.add_parser("requests")
    pg = sub.add_parser("purge")
    pg.add_argument("--days", type=int, required=True)
    a = p.parse_args()
    with app.app_context():
        if a.cmd == "stats":
            stats()
        elif a.cmd == "requests":
            requests_()
        else:
            purge(a.days)
