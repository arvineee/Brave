"""Mapenzi ya 30: #IAMBRAVE / Jagora wellness check-in site.

Pages
  /          home
  /checkin   the 10-question check-in (scored in the browser, see static/)
  /response  the personalised response, /response?score=1..5 (default 3)
  POST /api/checkin   stores the anonymous result (and an opt-in support request)

  /admin/*  staff dashboard (see dashboard.py); off unless ADMIN_PASSWORD_HASH and
            ADMIN_SECRET_KEY are set

Storage (Flask-SQLAlchemy): DATABASE_URL, e.g. postgresql://user:pass@host/db.
Without it a local SQLite file is used for development. Two separate tables:
`checkins` is anonymous (day, score, multiple-choice answers); `support_requests`
holds a contact only when the student typed one AND ticked consent, with no link
to any check-in. Free-text reflections are never sent or stored. Public pages set no
cookies, no sessions, no analytics. The only cookie is the staff login cookie, which is
limited to the path /admin.
"""
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import dashboard
import siteconfig
import trial

from content import CRISIS, DEFAULT_SCORE, FORM, LOCKED, RESPONSES, SITE, WANTS_SUPPORT

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
    hour = db.Column(db.Integer)  # Kenya-time hour 0-23, so staff can see when students check in
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
    handled = db.Column(db.Integer, nullable=False, default=0, server_default="0")


class SiteSetting(db.Model):
    """Staff-edited wording, contacts and socials, layered over content.py (see siteconfig.py)."""
    __tablename__ = "site_settings"
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class CustomQuestion(db.Model):
    """Extra questions added by staff. Never scored: they do not change the 1-5 level."""
    __tablename__ = "custom_questions"
    id = db.Column(db.Integer, primary_key=True)
    prompt = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(10), nullable=False)       # choice | multi | scale
    options = db.Column(db.Text, nullable=False)
    required = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Integer, nullable=False, default=1)
    position = db.Column(db.Integer, nullable=False, default=0)


class CustomAnswer(db.Model):
    """One row per chosen option. Anonymous like the check-in it belongs to."""
    __tablename__ = "custom_answers"
    id = db.Column(db.Integer, primary_key=True)
    checkin_id = db.Column(db.Integer, db.ForeignKey("checkins.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    question_id = db.Column(db.Integer, nullable=False, index=True)
    answer = db.Column(db.String(200), nullable=False)


class License(db.Model):
    """Single protected row (id=1). Signed with LICENSE_SECRET, guarded by DB triggers."""
    __tablename__ = "license"
    id = db.Column(db.Integer, primary_key=True)
    install_id = db.Column(db.String(32), nullable=False)
    started_at = db.Column(db.String(20), nullable=False)
    last_seen = db.Column(db.String(20), nullable=False)
    activated = db.Column(db.Integer, nullable=False, default=0)
    sig = db.Column(db.String(64), nullable=False)


def _row(r):
    return {"install_id": r.install_id, "started_at": r.started_at,
            "last_seen": r.last_seen, "activated": r.activated, "sig": r.sig}


def install_triggers():
    """Database-level guard on the license row (SQLite and PostgreSQL)."""
    name = db.engine.dialect.name
    stmts = (trial.POSTGRES_TRIGGERS if name == "postgresql"
             else trial.SQLITE_TRIGGERS if name == "sqlite" else [])
    try:
        for s in stmts:
            db.session.execute(text(s))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()  # another worker may be creating them at the same moment
        log.warning("license triggers not (re)created", exc_info=True)


def ensure_columns():
    """create_all() adds new tables but not new columns on existing ones. Add them here so the
    live database upgrades itself on the first start of this version."""
    wanted = {"checkins": (("hour", "INTEGER"),),
              "support_requests": (("handled", "INTEGER NOT NULL DEFAULT 0"),)}
    have = inspect(db.engine)
    for table, cols in wanted.items():
        existing = {c["name"] for c in have.get_columns(table)}
        for name, ddl in cols:
            if name in existing:
                continue
            try:  # table and column names are constants above, never user input
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()  # another worker may have added it a moment ago
                log.warning("could not add %s.%s", table, name, exc_info=True)


def start_trial(secret):
    """Create the license row (starts the clock). Safe if another worker does it first."""
    t = trial.stamp(trial.now())
    iid = secrets.token_hex(8)
    db.session.add(License(id=1, install_id=iid, started_at=t, last_seen=t,
                           activated=0, sig=trial.sign(secret, iid, t, t, 0)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()


def read_state(secret, trial_days):
    """Current licence state. The first call with a secret set starts the trial."""
    if not secret:
        return trial.evaluate(None, "", trial.now(), trial_days)
    row = db.session.get(License, 1)
    if row is None:
        if db.session.query(Checkin.id).first() is not None:
            return {"open": False, "reason": "tampered", "days_left": 0}  # row deleted
        start_trial(secret)
        row = db.session.get(License, 1)
    at = trial.now()
    state = trial.evaluate(_row(row), secret, at, trial_days)
    if state["reason"] == "trial" and trial.is_stale(row.last_seen, at):
        row.last_seen = trial.stamp(max(at, trial.parse(row.last_seen)))
        row.sig = trial.sign(secret, row.install_id, row.started_at, row.last_seen, row.activated)
        db.session.commit()
    return state


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


def clean_checkin(data, allowed=None):
    """Validate the JSON body. Returns (checkin_dict, request_dict_or_None) or None.

    Strict allow-list: only known option labels are accepted; any other field
    (including stray free text) is ignored and never stored. `allowed` lets the app pass
    the labels staff have reworded; it defaults to the labels in content.py.
    """
    if not isinstance(data, dict):
        return None
    ALLOWED = allowed or globals()["ALLOWED"]

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
    # Questions 8-10 are always visible. Support details are kept when the student wants
    # support OR left a contact (with consent), even if they answered "No" to question 7.
    if support_now in WANTS_SUPPORT or req is not None:
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

    return {
        "score": score, "energy": energy, "sleep": sleep, "connection": conn,
        "kept_up": kept, "support_now": support_now,
        "support_type": support_type, "channel": channel,
    }, req


def create_app(config=None):
    app = Flask(__name__)
    # Public pages never touch `session`, so Flask sets no cookie on them. The staff dashboard
    # is the only user of sessions: a signed cookie limited to /admin (see dashboard.py).
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_uri(),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=8192,
        SECRET_KEY=os.environ.get("ADMIN_SECRET_KEY") or None,
        ADMIN_PASSWORD_HASH=os.environ.get("ADMIN_PASSWORD_HASH", ""),
        SESSION_COOKIE_NAME="brave_admin",
        SESSION_COOKIE_PATH="/admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("ADMIN_COOKIE_SECURE", "1") != "0",  # 0 = local http
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        LICENSE_SECRET=os.environ.get("LICENSE_SECRET", ""),  # server env only, never in the repo
        TRIAL_DAYS=int(os.environ.get("TRIAL_DAYS", "40")),
        SITE_URL=os.environ.get("SITE_URL", "").rstrip("/"),  # e.g. https://mapenzi.example.org
    )
    if config:
        app.config.update(config)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        ensure_columns()
        install_triggers()

    def get_config():
        """Effective wording, contacts and custom questions for this request. Any database
        problem falls back to the defaults in content.py, so crisis resources always render."""
        cfg = getattr(g, "site_cfg", None)
        if cfg is None:
            try:
                settings = {r.key: r.value for r in SiteSetting.query.all()}
                rows = [{"id": q.id, "prompt": q.prompt, "kind": q.kind, "options": q.options,
                         "required": q.required, "active": q.active, "position": q.position}
                        for q in CustomQuestion.query.all()]
            except SQLAlchemyError:
                db.session.rollback()
                log.warning("site settings unavailable; using defaults", exc_info=True)
                settings, rows = {}, []
            cfg = g.site_cfg = siteconfig.build(settings, rows)
        return cfg


    failed_keys = []  # in-memory timestamps of wrong keys (no IP or user data kept)

    @app.before_request
    def gate():
        ep = request.endpoint
        if ep in (None, "static", "locked", "unlock", "robots", "sitemap"):
            return None
        try:
            state = read_state(app.config["LICENSE_SECRET"], app.config["TRIAL_DAYS"])
        except SQLAlchemyError:
            db.session.rollback()
            log.exception("licence check failed; allowing the request")
            return None
        if state["open"]:
            return None
        # Safety: the crisis page for a score of 5 is never blocked.
        if ep == "response" and parse_score(request.args.get("score")) == 5:
            return None
        if ep == "api_checkin":
            return jsonify(error="locked"), 503
        return redirect(url_for("locked"))

    @app.get("/locked")
    def locked():
        return render_template("locked.html", crisis=get_config()["crisis"], site=SITE,
                               L=LOCKED, error=None)

    @app.post("/unlock")
    def unlock():
        now = time.time()
        failed_keys[:] = [t for t in failed_keys if now - t < 600]
        if len(failed_keys) >= 10:
            return render_template("locked.html", crisis=get_config()["crisis"], site=SITE,
                                   L=LOCKED, error=LOCKED["too_many"]), 429
        secret = app.config["LICENSE_SECRET"]
        row = db.session.get(License, 1)
        valid = (row is not None and secret and
                 trial.evaluate(_row(row), secret, trial.now(), app.config["TRIAL_DAYS"])["reason"]
                 in ("trial", "expired"))
        if valid and trial.key_ok(secret, row.install_id, request.form.get("key", "")):
            at = trial.stamp(max(trial.now(), trial.parse(row.last_seen)))
            row.last_seen, row.activated = at, 1
            row.sig = trial.sign(secret, row.install_id, row.started_at, at, 1)
            db.session.commit()
            return redirect(url_for("home"))
        failed_keys.append(now)
        return render_template("locked.html", crisis=get_config()["crisis"], site=SITE,
                               L=LOCKED, error=LOCKED["wrong_key"]), 403

    NOINDEX_ENDPOINTS = {"response", "locked", "unlock", "api_checkin"}

    @app.context_processor
    def footer_context():
        cfg = get_config()
        return {"contacts": cfg["contacts"], "socials": cfg["socials"]}

    def site_url():
        return app.config["SITE_URL"] or request.url_root.rstrip("/")

    @app.context_processor
    def seo_context():
        base = site_url()
        return {
            "site_url": base,
            "canonical_url": base + request.path,  # no query string: /?utm=x is the same page
            "jsonld": {
                "@context": "https://schema.org",
                "@graph": [
                    {"@type": "Organization", "@id": base + "/#org", "name": SITE["org"],
                     "brand": "#IAMBRAVE", "url": base + "/",
                     "logo": base + url_for("static", filename="brave-logo.svg"),
                     "address": {"@type": "PostalAddress", "addressRegion": "Murang'a County",
                                 "addressCountry": "KE"}},
                    {"@type": "WebSite", "@id": base + "/#site", "url": base + "/",
                     "name": "Mapenzi ya 30", "inLanguage": "en-KE",
                     "publisher": {"@id": base + "/#org"}},
                ],
            },
        }

    @app.get("/robots.txt")
    def robots():
        body = f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\n\nSitemap: {site_url()}/sitemap.xml\n"
        return app.response_class(body, mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        urls = "".join(f"  <url><loc>{site_url()}{p}</loc></url>\n" for p in ("/", "/checkin"))
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
        return app.response_class(body, mimetype="application/xml")

    @app.route("/")
    def home():
        return render_template("home.html", site=SITE, crisis=get_config()["crisis"])

    @app.route("/checkin")
    def checkin():
        cfg = get_config()
        return render_template("checkin.html", f=cfg["form"], p=cfg["prompts"],
                               custom=cfg["custom"], crisis=cfg["crisis"], site=SITE,
                               wants_support=list(WANTS_SUPPORT))

    @app.route("/response")
    def response():
        score = parse_score(request.args.get("score"))
        return render_template(
            "response.html", score=score, r=RESPONSES[score], crisis=get_config()["crisis"],
            site=SITE, is_crisis=(score == 5),
        )

    @app.post("/api/checkin")
    def api_checkin():
        body = request.get_json(silent=True)
        cfg = get_config()
        cleaned = clean_checkin(body, cfg["allowed"])
        if cleaned is None:
            return jsonify(error="invalid"), 400
        c, req = cleaned
        answers = siteconfig.clean_custom_answers(body.get("custom"), cfg["custom"])
        if answers is None:
            return jsonify(error="invalid"), 400
        now = datetime.now(EAT)
        day = now.date().isoformat()
        try:
            row = Checkin(
                day=day, hour=now.hour, score=c["score"], energy=c["energy"], sleep=c["sleep"],
                connection=c["connection"], kept_up="; ".join(c["kept_up"]),
                support_now=c["support_now"], support_type=c["support_type"],
                channel=c["channel"],
            )
            db.session.add(row)
            db.session.flush()  # gives `row` its id for the extra answers
            for qid, answer in answers:
                db.session.add(CustomAnswer(checkin_id=row.id, question_id=qid, answer=answer))
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
        ep = request.endpoint or ""
        if ep in NOINDEX_ENDPOINTS or ep.startswith("admin_"):  # private pages never go in search
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        if request.endpoint == "static":
            resp.headers["Cache-Control"] = "public, max-age=3600"
        else:  # pages differ by query string; keep them out of shared caches
            resp.headers["Cache-Control"] = "no-store"
        return resp

    dashboard.register(app, dashboard.Deps(
        db=db, Checkin=Checkin, SupportRequest=SupportRequest, SiteSetting=SiteSetting,
        CustomQuestion=CustomQuestion, CustomAnswer=CustomAnswer, get_config=get_config,
        eat=EAT))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
