"""Mapenzi ya 30: #IAMBRAVE / Jagora wellness check-in site.

Pages
  /          home
  /checkin   the 10-question check-in (scored in the browser, see static/)
  /response  the personalised response, /response?score=1..5 (default 3)
  POST /api/checkin   stores the anonymous result (and an opt-in support request)

Storage (Flask-SQLAlchemy): DATABASE_URL, e.g. postgresql://user:pass@host/db.
Without it a local SQLite file is used for development. Two separate tables:
`checkins` is anonymous (day, score, multiple-choice answers); `support_requests`
holds a contact only when the student typed one AND ticked consent, with no link
to any check-in. Free-text reflections are never sent or stored. No cookies,
no sessions, no analytics.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import SQLAlchemyError

from content import CRISIS, DEFAULT_SCORE, FORM, RESPONSES, SITE, WANTS_SUPPORT

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EAT = timezone(timedelta(hours=3))  # Kenya time

db = SQLAlchemy()
log = logging.getLogger("mapenzi")


class Checkin(db.Model):
    """Anonymous: no name, contact, IP, user agent or free text."""
    __tablename__ = "checkins"
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)
    energy = db.Column(db.String(60), nullable=False)
    sleep = db.Column(db.String(30), nullable=False)
    connection = db.Column(db.Integer, nullable=False)
    kept_up = db.Column(db.String(400), nullable=False)
    support_now = db.Column(db.String(20), nullable=False)
    support_type = db.Column(db.String(80))
    channel = db.Column(db.String(20))
    __table_args__ = (
        db.CheckConstraint("score BETWEEN 1 AND 5", name="ck_score"),
        db.CheckConstraint("connection BETWEEN 0 AND 10", name="ck_connection"),
    )


class SupportRequest(db.Model):
    """Opt-in only. Deliberately has no reference to a Checkin row."""
    __tablename__ = "support_requests"
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10), nullable=False, index=True)
    support_type = db.Column(db.String(80), nullable=False)
    channel = db.Column(db.String(20), nullable=False)
    contact = db.Column(db.String(120), nullable=False)


def database_uri():
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):  # older style used by some hosts
            url = "postgresql://" + url[len("postgres://"):]
        return url
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
    return "sqlite:///" + os.path.join(BASE_DIR, "instance", "mapenzi.db")


def parse_score(raw):
    """Return a valid score 1-5; anything missing or invalid falls back to 3."""
    try:
        score = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_SCORE
    return score if score in RESPONSES else DEFAULT_SCORE


ALLOWED = {key: {label for label, _ in opts} for key, opts in FORM.items()}


def clean_checkin(data):
    """Validate the JSON body. Returns (checkin_dict, request_dict_or_None) or None.

    Strict allow-list: only known option labels are accepted; any other field
    (including stray free text) is ignored and never stored.
    """
    if not isinstance(data, dict):
        return None

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    score, conn = data.get("score"), data.get("connection")
    if not is_int(score) or score not in RESPONSES:
        return None
    if not is_int(conn) or not 0 <= conn <= 10:
        return None
    energy, sleep, support_now = data.get("energy"), data.get("sleep"), data.get("support_now")
    if energy not in ALLOWED["energy"] or sleep not in ALLOWED["sleep"]:
        return None
    if support_now not in ALLOWED["support_now"]:
        return None
    kept = data.get("kept_up")
    if (not isinstance(kept, list) or not kept or len(kept) > len(ALLOWED["kept_up"])
            or len(set(kept)) != len(kept) or any(k not in ALLOWED["kept_up"] for k in kept)):
        return None

    support_type = channel = None
    req = data.get("request")
    if support_now in WANTS_SUPPORT:
        support_type, channel = data.get("support_type"), data.get("channel")
        if support_type not in ALLOWED["support_type"] or channel not in ALLOWED["channel"]:
            return None
        if req is not None:
            if (not isinstance(req, dict) or req.get("consent") is not True
                    or not isinstance(req.get("contact"), str)):
                return None
            contact = req["contact"].strip()
            if not 1 <= len(contact) <= 120:
                return None
            req = {"contact": contact}
    elif req is not None:
        return None  # a contact without asking for support makes no sense

    return {
        "score": score, "energy": energy, "sleep": sleep, "connection": conn,
        "kept_up": kept, "support_now": support_now,
        "support_type": support_type, "channel": channel,
    }, req


def create_app(config=None):
    app = Flask(__name__)
    # No SECRET_KEY and no `session`: Flask never sets a cookie.
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri(),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=4096,
    )
    if config:
        app.config.update(config)
    db.init_app(app)
    with app.app_context():
        db.create_all()

    @app.route("/")
    def home():
        return render_template("home.html", site=SITE, crisis=CRISIS)

    @app.route("/checkin")
    def checkin():
        return render_template("checkin.html", f=FORM, crisis=CRISIS, site=SITE,
                               wants_support=list(WANTS_SUPPORT))

    @app.route("/response")
    def response():
        score = parse_score(request.args.get("score"))
        return render_template(
            "response.html", score=score, r=RESPONSES[score], crisis=CRISIS,
            site=SITE, is_crisis=(score == 5),
        )

    @app.post("/api/checkin")
    def api_checkin():
        cleaned = clean_checkin(request.get_json(silent=True))
        if cleaned is None:
            return jsonify(error="invalid"), 400
        c, req = cleaned
        day = datetime.now(EAT).date().isoformat()
        try:
            db.session.add(Checkin(
                day=day, score=c["score"], energy=c["energy"], sleep=c["sleep"],
                connection=c["connection"], kept_up="; ".join(c["kept_up"]),
                support_now=c["support_now"], support_type=c["support_type"],
                channel=c["channel"],
            ))
            if req:
                db.session.add(SupportRequest(
                    day=day, support_type=c["support_type"], channel=c["channel"],
                    contact=req["contact"],
                ))
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            log.exception("could not save check-in")
            return jsonify(error="unavailable"), 503
        return "", 204

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="invalid"), 400

    @app.after_request
    def headers(resp):
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        if request.endpoint == "static":
            resp.headers["Cache-Control"] = "public, max-age=3600"
        else:  # pages differ by query string; keep them out of shared caches
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
