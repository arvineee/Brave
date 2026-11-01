"""Staff dashboard for Brave: /admin

Off by default. It switches on only when BOTH are set in the server environment:
  ADMIN_PASSWORD_HASH   from `python admin.py hashpw`
  ADMIN_SECRET_KEY      a long random string that signs the login cookie
Without them every /admin URL returns 404, so a half-configured site is never open.

Two kinds of staff sign in on the same page (nothing on it says which is which):
  * the administrator: the password above, username "admin" (or left empty); sees every school.
  * a school manager: username and password created by the administrator under Schools; sees
    only that school's statistics. Every query goes through scope(), so a manager can never
    reach another school's rows, and the pages that change settings are administrator-only.

Privacy model (unchanged from the public site)
  * Public pages still set no cookies. The login cookie exists only after a staff login and is
    limited to the path /admin (HttpOnly, SameSite=Strict, Secure on https).
  * Rows are anonymous. Staff see day, a part of day, the level and the multiple-choice answers;
    never an exact time. Exact hours appear only as counts in the charts.
  * The two reflection boxes are never sent to the server, so there is nothing to read there.
  * Support requests (contacts) are the one sensitive list: shown only here, deletable.
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import textwrap
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
from typing import Any, Callable

from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from flask import (Response, abort, flash, g, redirect, render_template, request, session,
                   url_for)
from sqlalchemy import and_, case, func, select, true
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

import segno

import charts
import content
import siteconfig
import stats

log = logging.getLogger("mapenzi.admin")
PER_PAGE = 25
CSV_LIMIT = 50000


@dataclass
class Deps:
    db: Any
    Checkin: Any
    SupportRequest: Any
    SiteSetting: Any
    CustomQuestion: Any
    CustomAnswer: Any
    School: Any
    Manager: Any
    get_config: Callable
    eat: Any


part_of_day = stats.part_of_day  # one definition, shared with the reports


QR_TAG = "#IAMBRAVE"  # printed under every QR code


def qr_svg(url, title=None):
    """The QR code for `url`, with QR_TAG printed underneath, as SVG bytes. With a `title`
    (a school's name) the name is printed above the tag, so a poster says which school it is for.
    Drawn on the server, so no outside service is used."""
    matrix = segno.make(url, error="m").matrix      # 1 = dark module, no quiet zone
    n, border = len(matrix), 4                        # modules; quiet zone
    size = n + 2 * border
    lines = []
    if title:
        lines = textwrap.wrap(title, 20, break_long_words=True)
        if len(lines) > 3:
            lines = lines[:3]
            lines[-1] = lines[-1][:19] + "\u2026"
    width = max(size, 64) if lines else size
    label = 10 + 7 * len(lines)                      # space under the code
    x0 = (width - size) / 2
    cells = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                cells.append(f"M{x + x0 + border:g} {y + border}h1v1h-1z")
    text = []
    for i, line in enumerate(lines):
        text.append(f'<text x="{width / 2:g}" y="{size + 3 + 7 * (i + 1) - 2}" text-anchor="middle" '
                    f'font-family="Arial, Helvetica, sans-serif" font-size="5.2" '
                    f'font-weight="bold" fill="#000000">{xml_escape(line)}</text>')
    text.append(f'<text x="{width / 2:g}" y="{size + label - 3}" text-anchor="middle" '
                f'font-family="Arial, Helvetica, sans-serif" font-size="{4.4 if lines else 6}" '
                f'font-weight="bold" fill="#000000">{QR_TAG}</text>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {size + label}" '
        f'width="{width * 8:g}" height="{(size + label) * 8}">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<path fill="#000000" d="{"".join(cells)}"/>'
        + "".join(text) + '</svg>'
    )
    return svg.encode("utf-8")


# ----------------------------------------------------- school / manager input ----
NAME_OK = re.compile(r"^[\w .,'\u2019&()\-]{2,60}$")
USER_OK = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,59}$")
MIN_PASSWORD = 10


def clean_school_name(raw):
    name = " ".join((raw or "").split())
    if not NAME_OK.match(name):
        return None, ["School name: 2 to 60 characters, letters, numbers and . , ' & ( ) - only."]
    return name, []


def make_slug(name, taken):
    """URL piece for the QR code: lower-case ASCII, unique among `taken`."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:34].strip("-") or "school"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}-{i}", i + 1
    return slug


def clean_username(raw):
    u = (raw or "").strip().lower()
    if u == "admin" or not USER_OK.match(u):
        return None, ["Username: 3 to 60 characters, letters, numbers and . _ @ - only "
                      "(and not 'admin')."]
    return u, []


def clean_password(raw):
    pw = raw or ""
    if len(pw) < MIN_PASSWORD or len(pw) > 200:
        return None, [f"Password: at least {MIN_PASSWORD} characters."]
    return pw, []


def pw_stamp(manager):
    """Changes whenever the password does, so a reset signs the manager out everywhere."""
    return hashlib.sha256(manager.password_hash.encode()).hexdigest()[:16]


class Scope:
    """Which schools the current page covers: all, only the general link, or one school."""
    def __init__(self, kind, school_id=None, label="All schools", arg=None):
        self.kind, self.school_id, self.label, self.arg = kind, school_id, label, arg


def csv_safe(v):
    """Stop spreadsheet programs treating a cell as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s



def register(app, d):
    db = d.db
    failed = {}  # username -> timestamps of wrong passwords (no IP or other data kept)

    def enabled():
        return bool(app.config.get("ADMIN_PASSWORD_HASH") and app.config.get("SECRET_KEY"))

    def current_staff():
        """The signed-in staff member re-checked against the database on every request, so
        switching a manager off or resetting their password takes effect at once."""
        role = session.get("role")
        if role == "admin":
            return {"role": "admin", "school_id": None, "school": None}
        if role == "manager" and isinstance(session.get("manager_id"), int):
            m = db.session.get(d.Manager, session["manager_id"])
            if m is not None and m.active and session.get("pwv") == pw_stamp(m):
                school = db.session.get(d.School, m.school_id)
                if school is not None:
                    return {"role": "manager", "school_id": school.id, "school": school}
        session.clear()
        return None

    def guard(view, admin_only_page):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not enabled():
                abort(404)
            who = current_staff()
            if who is None:
                return redirect(url_for("admin_login"))
            if admin_only_page and who["role"] != "admin":
                abort(403)
            g.staff = who
            if request.method == "POST":
                sent, have = request.form.get("csrf", ""), session.get("csrf", "")
                if not have or not hmac.compare_digest(sent, have):
                    abort(400)
            return view(*args, **kwargs)
        return wrapped

    def staff_only(view):      # any signed-in staff member (the data is limited by scope())
        return guard(view, False)

    def admin_only(view):      # the administrator only
        return guard(view, True)

    def current_scope():
        sc = getattr(g, "scope", None)
        if sc is not None:
            return sc
        who = g.staff
        if who["role"] == "manager":
            sc = Scope("school", who["school_id"], who["school"].name)
        else:
            raw = request.args.get("school", "")
            school = None
            if raw == "none":
                sc = Scope("none", None, "Unassigned (general link)", "none")
            else:
                try:
                    school = db.session.get(d.School, int(raw))
                except (ValueError, TypeError):
                    school = None
                sc = (Scope("school", school.id, school.name, str(school.id)) if school
                      else Scope("all"))
        g.scope = sc
        return sc

    def sfilter(col):
        """SQL condition limiting `col` (a school_id column) to the current scope."""
        sc = current_scope()
        if sc.kind == "none":
            return col.is_(None)
        if sc.kind == "school":
            return col == sc.school_id
        return true()

    def sargs():
        sc = current_scope()
        return {"school": sc.arg} if sc.arg else {}

    def school_names():
        return {s_.id: s_.name for s_ in d.School.query}

    def page(template, active, **ctx):
        admin = g.staff["role"] == "admin"
        pick = (d.School.query.order_by(d.School.name).all() if admin else [])
        return render_template("admin/" + template, active=active,
                               csrf=session.get("csrf", ""), me=g.staff, scope=current_scope(),
                               sargs=sargs(), pick=pick, **ctx)

    def save(fn, ok_message):
        """Run a database change; flash the outcome. Returns True on success."""
        try:
            fn()
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            log.exception("admin save failed")
            flash("Sorry, that could not be saved. Please try again.", "error")
            return False
        flash(ok_message, "ok")
        return True

    def put_setting(key, value):
        row = db.session.get(d.SiteSetting, key)
        text = json.dumps(value, ensure_ascii=False)
        if row is None:
            db.session.add(d.SiteSetting(key=key, value=text))
        else:
            row.value = text

    def drop_setting(key):
        row = db.session.get(d.SiteSetting, key)
        if row is not None:
            db.session.delete(row)

    def flash_all(errors):
        for e in errors:
            flash(e, "error")

    # ------------------------------------------------------------ session ----
    dummy_hash = []   # checked for unknown usernames, so timing does not reveal which exist

    def check_login(username, password):
        """Session fields for a valid sign-in, else None. One page, one message: the form never
        says what kind of account it is for."""
        if username in ("", "admin"):
            if check_password_hash(app.config["ADMIN_PASSWORD_HASH"], password):
                return {"role": "admin"}
            return None
        m = d.Manager.query.filter_by(username=username).first()
        if not dummy_hash:
            dummy_hash.append(generate_password_hash(secrets.token_hex(8)))
        ok = check_password_hash(m.password_hash if m else dummy_hash[0], password)
        if m is not None and ok and m.active:
            return {"role": "manager", "manager_id": m.id, "pwv": pw_stamp(m)}
        return None

    def throttled(key, now):
        for k in list(failed):
            failed[k] = [t for t in failed[k] if now - t < 600]
            if not failed[k]:
                del failed[k]
        return len(failed.get(key, ())) >= 10 or sum(len(v) for v in failed.values()) >= 100

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not enabled():
            abort(404)
        if current_staff() is not None:
            return redirect(url_for("admin_overview"))
        error, status = None, 200
        if request.method == "POST":
            now = time.time()
            username = (request.form.get("username") or "").strip().lower()[:60]
            if throttled(username, now):
                error, status = "Too many attempts. Please wait a few minutes.", 429
            else:
                who = check_login(username, request.form.get("password", ""))
                if who:
                    session.clear()
                    session.permanent = True
                    session.update(who)
                    session["csrf"] = secrets.token_urlsafe(24)
                    return redirect(url_for("admin_overview"))
                failed.setdefault(username, []).append(now)
                error, status = "That username or password was not accepted.", 403
        return render_template("admin/login.html", error=error), status

    @app.post("/admin/logout")
    @staff_only
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    # ----------------------------------------------------------- overview ----
    @app.get("/admin/")
    @staff_only
    def admin_overview():
        today = datetime.now(d.eat).date()
        raw = (db.session.query(d.Checkin.day, d.Checkin.hour, d.Checkin.score)
               .filter(sfilter(d.Checkin.school_id)).all())
        data = stats.build(raw, today, request.args.get("range", "month"))
        b, p, t, c = data["breakdown"], data["patterns"], data["trend"], data["crisis"]
        hours = [str(h) for h in range(24)]
        figs = {
            "levels": charts.level_bars(b["counts"], b["total"]),
            "weekly": charts.stacked(t["weekly"], label="Check-ins per week by level"),
            "crisis": charts.columns(c["weekly"], c["weekly_labels"],
                                     label="In-crisis check-ins per week", cls="bar crisis"),
            "hours": charts.columns(p["by_hour"], hours, label="Check-ins by hour of day",
                                    every=3, marks=set(stats.NIGHT_HOURS)),
            "weekdays": charts.columns(p["by_weekday"], list(stats.WEEKDAYS),
                                       label="Check-ins by day of week"),
        }
        return page("overview.html", "overview", data=data, figs=figs, ranges=stats.RANGES,
                    levels=stats.LEVELS, weekdays=stats.WEEKDAYS,
                    custom_results=custom_results(), by_school=by_school(today))

    def by_school(today):
        """Administrator, all schools: one line per school (this month, crisis, all time)."""
        if g.staff["role"] != "admin" or current_scope().kind != "all":
            return []
        ms = today.replace(day=1).isoformat()
        c = d.Checkin
        rows = (db.session.query(
            c.school_id, func.count(),
            func.sum(case((c.day >= ms, 1), else_=0)),
            func.sum(case((and_(c.day >= ms, c.score == 5), 1), else_=0)))
            .group_by(c.school_id).all())
        seen = {sid: (n, m or 0, k or 0) for sid, n, m, k in rows}
        names = school_names()
        out = [{"id": sid, "name": name, "all": seen.get(sid, (0, 0, 0))[0],
                "month": seen.get(sid, (0, 0, 0))[1], "crisis": seen.get(sid, (0, 0, 0))[2]}
               for sid, name in names.items()]
        if None in seen:
            n, m, k = seen[None]
            out.append({"id": None, "name": "Unassigned (general link)", "all": n,
                        "month": m, "crisis": k})
        out.sort(key=lambda r: (-r["all"], r["name"].lower()))
        return out if len(out) > 1 else []

    def custom_results():
        questions = {q.id: q for q in d.CustomQuestion.query.order_by(d.CustomQuestion.position,
                                                                       d.CustomQuestion.id)}
        if not questions:
            return []
        per_q = defaultdict(Counter)
        for qid, answer, n in (db.session.query(d.CustomAnswer.question_id, d.CustomAnswer.answer,
                                                db.func.count())
                               .filter(d.CustomAnswer.checkin_id.in_(
                                   select(d.Checkin.id).where(sfilter(d.Checkin.school_id))))
                               .group_by(d.CustomAnswer.question_id, d.CustomAnswer.answer)):
            per_q[qid][answer] += n
        out = []
        for qid, q in questions.items():
            counts = per_q.get(qid)
            if not counts:
                continue
            total = sum(counts.values())
            if q.kind == "scale":
                rows = [(str(i), counts.get(str(i), 0)) for i in range(11)]
                mean = round(sum(int(a) * n for a, n in counts.items()) / total, 1)
            else:
                order = siteconfig.custom_options(q.kind, q.options)
                rows = [(a, counts[a]) for a in order if a in counts]
                rows += [(a, n) for a, n in counts.items() if a not in order]  # reworded since
                mean = None
            out.append({"prompt": q.prompt, "kind": q.kind, "rows": rows, "total": total,
                        "mean": mean, "active": bool(q.active)})
        return out

    # ---------------------------------------------------------- responses ----
    def checkin_query():
        q = d.Checkin.query.filter(sfilter(d.Checkin.school_id))
        try:
            score = int(request.args.get("score", ""))
        except ValueError:
            score = None
        if score in range(1, 6):
            q = q.filter(d.Checkin.score == score)
        else:
            score = None
        return q, score

    def answers_for(ids):
        by_checkin = defaultdict(lambda: defaultdict(list))
        if ids:
            for r in d.CustomAnswer.query.filter(d.CustomAnswer.checkin_id.in_(ids)):
                by_checkin[r.checkin_id][r.question_id].append(r.answer)
        return by_checkin

    @app.get("/admin/responses")
    @staff_only
    def admin_responses():
        q, score = checkin_query()
        try:
            pg = max(int(request.args.get("page", "1")), 1)
        except ValueError:
            pg = 1
        total = q.count()
        rows = q.order_by(d.Checkin.id.desc()).offset((pg - 1) * PER_PAGE).limit(PER_PAGE).all()
        extra = answers_for([r.id for r in rows])
        titles = {x.id: x.prompt for x in d.CustomQuestion.query}
        names = school_names()
        items = [{
            "row": r, "part": part_of_day(r.hour), "level": stats.LEVELS[r.score],
            "school": names.get(r.school_id, "Unassigned"),
            "custom": [(titles.get(qid, "Removed question"), ", ".join(a))
                       for qid, a in extra.get(r.id, {}).items()],
        } for r in rows]
        return page("responses.html", "responses", items=items, total=total, score=score,
                    page_no=pg, pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
                    levels=stats.LEVELS)

    @app.get("/admin/responses.csv")
    @staff_only
    def admin_responses_csv():
        q, _score = checkin_query()
        rows = q.order_by(d.Checkin.id.desc()).limit(CSV_LIMIT).all()
        extra = answers_for([r.id for r in rows])
        titles = {x.id: x.prompt for x in d.CustomQuestion.query}
        names = school_names()
        qids = sorted({qid for per in extra.values() for qid in per})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Part of day", "Level", "Level name", "Energy", "Sleep",
                    "Connection (0-10)", "Kept up by", "Wants support", "Support type", "Channel",
                    "School"]
                   + [titles.get(i, f"Question {i}") for i in qids])
        for r in rows:
            w.writerow([csv_safe(x) for x in (
                r.day, part_of_day(r.hour), r.score, stats.LEVELS[r.score], r.energy, r.sleep,
                r.connection, r.kept_up, r.support_now, r.support_type, r.channel,
                names.get(r.school_id, "Unassigned"))]
                + [csv_safe(", ".join(extra.get(r.id, {}).get(i, []))) for i in qids])
        name = f"mapenzi-checkins-{datetime.now(d.eat).date().isoformat()}.csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ support ----
    def own_request(rid):
        """A support request this person may touch: managers only their own school's."""
        r = db.session.get(d.SupportRequest, rid)
        if r is None or (g.staff["role"] == "manager"
                         and (r.school_id != g.staff["school_id"] or not r.released)):
            abort(404)
        return r

    @app.get("/admin/support")
    @staff_only
    def admin_support():
        q = d.SupportRequest.query.filter(sfilter(d.SupportRequest.school_id))
        if g.staff["role"] == "manager":   # a manager only sees what the administrator released
            q = q.filter(d.SupportRequest.released == 1)
        rows = q.order_by(d.SupportRequest.handled, d.SupportRequest.id.desc()).all()
        pending = [r for r in rows if not r.released]
        released = [r for r in rows if r.released]
        return page("support.html", "support", rows=released, pending=pending,
                    names=school_names(), waiting=sum(1 for r in released if not r.handled))

    def release_ids(ids, value):
        """Set `released` on the given requests that have a school (the administrator's review).
        Returns how many changed. Requests from the general link have no manager to receive them."""
        q = d.SupportRequest.query.filter(d.SupportRequest.id.in_(ids),
                                          d.SupportRequest.school_id.isnot(None),
                                          d.SupportRequest.released != value)
        n = q.count()
        save(lambda: q.update({"released": value}, synchronize_session=False),
             (f"Pushed {n} request(s) to the school manager(s)." if value
              else f"Took back {n} request(s) from the school manager(s)."))
        return n

    @app.post("/admin/support/release")
    @admin_only
    def admin_support_release():
        """Bulk: the ticked requests, or every waiting request in the current school scope."""
        if request.form.get("all"):
            ids = [r_id for (r_id,) in db.session.query(d.SupportRequest.id).filter(
                sfilter(d.SupportRequest.school_id), d.SupportRequest.released == 0)]
        else:
            ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        if not ids:
            flash("Tick at least one request first.", "error")
        else:
            release_ids(ids, 1)
        return redirect(url_for("admin_support", **sargs()))

    @app.post("/admin/support/<int:rid>/release")
    @admin_only
    def admin_support_release_one(rid):
        r = own_request(rid)
        if r.school_id is None:
            flash("This request came from the general link, so there is no school manager to "
                  "send it to.", "error")
        else:
            release_ids([r.id], 1)
        return redirect(url_for("admin_support", **sargs()))

    @app.post("/admin/support/<int:rid>/withdraw")
    @admin_only
    def admin_support_withdraw(rid):
        r = own_request(rid)
        release_ids([r.id], 0)
        return redirect(url_for("admin_support", **sargs()))

    @app.post("/admin/support/<int:rid>/handled")
    @staff_only
    def admin_support_handled(rid):
        r = own_request(rid)

        def change():
            r.handled = 0 if r.handled else 1
        save(change, "Updated.")
        return redirect(url_for("admin_support", **sargs()))

    @app.post("/admin/support/<int:rid>/delete")
    @staff_only
    def admin_support_delete(rid):
        r = own_request(rid)
        save(lambda: db.session.delete(r), "Request deleted.")
        return redirect(url_for("admin_support", **sargs()))

    @app.post("/admin/support/purge-handled")
    @staff_only
    def admin_support_purge():
        q = d.SupportRequest.query.filter(d.SupportRequest.handled == 1,
                                          sfilter(d.SupportRequest.school_id))
        if g.staff["role"] == "manager":
            q = q.filter(d.SupportRequest.released == 1)
        n = q.count()
        save(lambda: q.delete(synchronize_session=False), f"Deleted {n} handled request(s).")
        return redirect(url_for("admin_support", **sargs()))

    # ---------------------------------------------------------- questions ----
    @app.get("/admin/questions")
    @admin_only
    def admin_questions():
        cfg = d.get_config()
        custom = d.CustomQuestion.query.order_by(d.CustomQuestion.position,
                                                 d.CustomQuestion.id).all()
        counts = dict(db.session.query(d.CustomAnswer.question_id, db.func.count())
                      .group_by(d.CustomAnswer.question_id).all())
        view = [{"q": q, "options": siteconfig.custom_options(q.kind, q.options),
                 "answers": counts.get(q.id, 0)} for q in custom]
        return page("questions.html", "questions", cfg=cfg, defaults=content, custom=view,
                    kinds=siteconfig.CUSTOM_KINDS, caps=content.EDITABLE_LABELS,
                    max_custom=siteconfig.MAX_CUSTOM, max_options=siteconfig.MAX_OPTIONS,
                    active_count=sum(1 for q in custom if q.active))

    @app.post("/admin/questions/builtin")
    @admin_only
    def admin_questions_builtin():
        prompts, e1 = siteconfig.clean_prompts(request.form)
        labels, e2 = siteconfig.clean_labels(request.form)
        if e1 or e2:
            flash_all(e1 + e2)
            return redirect(url_for("admin_questions"))

        def change():
            put_setting("prompts", prompts)
            put_setting("labels", labels)
        save(change, "Question wording saved. It is live on the check-in now.")
        return redirect(url_for("admin_questions"))

    @app.post("/admin/questions/reset")
    @admin_only
    def admin_questions_reset():
        def change():
            drop_setting("prompts")
            drop_setting("labels")
        save(change, "Question wording restored to the original.")
        return redirect(url_for("admin_questions"))

    @app.post("/admin/questions/custom")
    @admin_only
    def admin_custom_add():
        clean, errors = siteconfig.clean_custom_question(request.form)
        active = d.CustomQuestion.query.filter_by(active=1).count()
        if not errors and active >= siteconfig.MAX_CUSTOM:
            errors = [f"The form can carry at most {siteconfig.MAX_CUSTOM} extra questions. "
                      "Hide one first."]
        if errors:
            flash_all(errors)
            return redirect(url_for("admin_questions"))
        top = db.session.query(db.func.max(d.CustomQuestion.position)).scalar() or 0

        def change():
            db.session.add(d.CustomQuestion(position=top + 1, active=1, **clean))
        save(change, "Question added. It is live at the end of the check-in.")
        return redirect(url_for("admin_questions"))

    @app.post("/admin/questions/custom/<int:qid>/edit")
    @admin_only
    def admin_custom_edit(qid):
        q = db.session.get(d.CustomQuestion, qid)
        if q is None:
            abort(404)
        clean, errors = siteconfig.clean_custom_question(request.form)
        if errors:
            flash_all(errors)
            return redirect(url_for("admin_questions"))
        try:
            position = max(int(request.form.get("position", q.position)), 0)
        except ValueError:
            position = q.position

        def change():
            q.prompt, q.kind, q.options = clean["prompt"], clean["kind"], clean["options"]
            q.required, q.position = clean["required"], position
        save(change, "Question updated.")
        return redirect(url_for("admin_questions"))

    @app.post("/admin/questions/custom/<int:qid>/toggle")
    @admin_only
    def admin_custom_toggle(qid):
        q = db.session.get(d.CustomQuestion, qid)
        if q is None:
            abort(404)
        if not q.active and d.CustomQuestion.query.filter_by(active=1).count() >= siteconfig.MAX_CUSTOM:
            flash(f"At most {siteconfig.MAX_CUSTOM} extra questions can be shown at once.", "error")
            return redirect(url_for("admin_questions"))

        def change():
            q.active = 0 if q.active else 1
        save(change, "Question shown." if not q.active else "Question hidden from the form.")
        return redirect(url_for("admin_questions"))

    @app.post("/admin/questions/custom/<int:qid>/delete")
    @admin_only
    def admin_custom_delete(qid):
        q = db.session.get(d.CustomQuestion, qid)
        if q is None:
            abort(404)

        def change():
            d.CustomAnswer.query.filter_by(question_id=qid).delete(synchronize_session=False)
            db.session.delete(q)
        save(change, "Question and its answers deleted.")
        return redirect(url_for("admin_questions"))

    # ------------------------------------------------- contacts & socials ----
    @app.get("/admin/contacts")
    @admin_only
    def admin_contacts():
        cfg = d.get_config()
        blanks = max(2, 3 - len(cfg["socials"]))
        rows = cfg["socials"] + [{"platform": "", "label": "", "url": ""}] * blanks
        helplines = cfg["crisis"]["helplines"]
        spare = max(1, min(2, siteconfig.MAX_HELPLINES - len(helplines)))
        help_rows = (helplines + [{"name": "", "phone_display": "", "phone_tel": ""}] * spare)
        return page("contacts.html", "contacts", cfg=cfg, rows=rows[:siteconfig.MAX_SOCIALS],
                    platforms=content.SOCIAL_PLATFORMS, max_socials=siteconfig.MAX_SOCIALS,
                    helplines=help_rows[:siteconfig.MAX_HELPLINES],
                    max_helplines=siteconfig.MAX_HELPLINES)

    @app.post("/admin/contacts")
    @admin_only
    def admin_contacts_save():
        section = request.form.get("section")
        if section == "crisis":
            clean, errors = siteconfig.clean_crisis(request.form)
            if errors:
                flash_all(errors)
            else:
                save(lambda: put_setting("crisis", clean),
                     "Helpline details saved. They now show on every page.")
        elif section == "contacts":
            clean, errors = siteconfig.clean_contacts(request.form)
            if errors:
                flash_all(errors)
            else:
                save(lambda: put_setting("contacts", clean), "Contact details saved.")
        elif section == "socials":
            clean, errors = siteconfig.clean_socials(request.form)
            if errors:
                flash_all(errors)
            else:
                save(lambda: put_setting("socials", clean), "Social links saved.")
        else:
            abort(400)
        return redirect(url_for("admin_contacts"))

    # ---------------------------------------------------- staff sign-in link ----
    @app.context_processor
    def staff_login_context():
        """Shows a 'Staff sign in' button on the public footer, only when the dashboard is on."""
        return {"staff_login": enabled()}

    # ------------------------------------------------------------- QR code ----
    def saved_address():
        row = db.session.get(d.SiteSetting, siteconfig.PUBLIC_URL_KEY)
        return siteconfig.stored_public_url(row.value) if row is not None else None

    def check_in_address():
        """(url, saved). The address staff saved; until they save one, the address this site
        is reached at, with /checkin added."""
        saved = saved_address()
        if saved:
            return saved, True
        base = app.config.get("SITE_URL") or request.url_root.rstrip("/")
        return base + "/checkin", False

    def school_address(school):
        """The address a school's QR code opens: the site's address plus /s/<slug>."""
        saved = saved_address()
        if saved:
            p = urlparse(saved)
            base = f"{p.scheme}://{p.netloc}"
        else:
            base = app.config.get("SITE_URL") or request.url_root.rstrip("/")
        return f"{base}/s/{school.slug}"

    def qr_school(sid):
        """The school whose QR page this person may open: managers only their own."""
        school = db.session.get(d.School, sid)
        if school is None or (g.staff["role"] == "manager" and school.id != g.staff["school_id"]):
            abort(404)
        return school

    @app.get("/admin/qr")
    @staff_only
    def admin_qr():
        if g.staff["role"] == "manager":
            return redirect(url_for("admin_school_qr", sid=g.staff["school_id"]))
        url, saved = check_in_address()
        schools = d.School.query.order_by(d.School.name).all()
        return page("qr.html", "qr", url=url, saved=saved, schools=schools)

    @app.post("/admin/qr")
    @admin_only
    def admin_qr_save():
        url, errors = siteconfig.clean_public_url(request.form.get("url"))
        if errors:
            flash_all(errors)
            return redirect(url_for("admin_qr"))
        save(lambda: put_setting(siteconfig.PUBLIC_URL_KEY, url),
             "Address saved. The QR codes now use this site address.")
        return redirect(url_for("admin_qr"))

    @app.get("/admin/qr.svg")
    @admin_only
    def admin_qr_image():
        url, _saved = check_in_address()
        return Response(qr_svg(url), mimetype="image/svg+xml")

    @app.get("/admin/qr/download.svg")
    @admin_only
    def admin_qr_download():
        url, _saved = check_in_address()
        return Response(qr_svg(url), mimetype="image/svg+xml",
                        headers={"Content-Disposition":
                                 'attachment; filename="mapenzi-checkin-qr.svg"'})

    @app.get("/admin/schools/<int:sid>/qr")
    @staff_only
    def admin_school_qr(sid):
        school = qr_school(sid)
        return page("school_qr.html", "qr", school=school, url=school_address(school))

    @app.get("/admin/schools/<int:sid>/qr.svg")
    @staff_only
    def admin_school_qr_image(sid):
        school = qr_school(sid)
        return Response(qr_svg(school_address(school), school.name), mimetype="image/svg+xml")

    @app.get("/admin/schools/<int:sid>/qr/download.svg")
    @staff_only
    def admin_school_qr_download(sid):
        school = qr_school(sid)
        return Response(qr_svg(school_address(school), school.name), mimetype="image/svg+xml",
                        headers={"Content-Disposition":
                                 f'attachment; filename="mapenzi-qr-{school.slug}.svg"'})

    # ------------------------------------------------------------- schools ----
    @app.get("/admin/schools")
    @admin_only
    def admin_schools():
        counts = dict(db.session.query(d.Checkin.school_id, func.count())
                      .group_by(d.Checkin.school_id).all())
        managers = {}
        for m in d.Manager.query.order_by(d.Manager.username):
            managers.setdefault(m.school_id, []).append(m)
        schools = d.School.query.order_by(d.School.name).all()
        contact_counts = dict(db.session.query(d.SupportRequest.school_id, func.count())
                              .group_by(d.SupportRequest.school_id).all())
        return page("schools.html", "schools", schools=schools, counts=counts,
                    contact_counts=contact_counts,
                    managers=managers, min_password=MIN_PASSWORD,
                    urls={s_.id: school_address(s_) for s_ in schools})

    @app.post("/admin/schools")
    @admin_only
    def admin_school_add():
        name, errors = clean_school_name(request.form.get("name"))
        if errors:
            flash_all(errors)
            return redirect(url_for("admin_schools"))
        slug = make_slug(name, {s_.slug for s_ in d.School.query})
        save(lambda: db.session.add(d.School(name=name, slug=slug, active=1)),
             f"{name} added. Its QR code is ready under QR codes.")
        return redirect(url_for("admin_schools"))

    def school_or_404(sid):
        school = db.session.get(d.School, sid)
        if school is None:
            abort(404)
        return school

    @app.post("/admin/schools/<int:sid>/rename")
    @admin_only
    def admin_school_rename(sid):
        school = school_or_404(sid)
        name, errors = clean_school_name(request.form.get("name"))
        if errors:
            flash_all(errors)
        else:
            def change():
                school.name = name
            save(change, "School name updated. Download its QR code again to print the new name.")
        return redirect(url_for("admin_schools"))

    @app.post("/admin/schools/<int:sid>/toggle")
    @admin_only
    def admin_school_toggle(sid):
        school = school_or_404(sid)

        def change():
            school.active = 0 if school.active else 1
        save(change, "School switched off. Its QR code stops working, but its data and its "
             "managers are kept." if school.active else "School switched on.")
        return redirect(url_for("admin_schools"))

    @app.post("/admin/schools/<int:sid>/delete")
    @admin_only
    def admin_school_delete(sid):
        """Remove a school that is no longer needed. The administrator must type its name.
        Always deleted with it: its managers and its contacts (personal data that is no longer
        needed). Its anonymous check-ins are kept as "Unassigned" unless the box is ticked."""
        school = school_or_404(sid)
        if " ".join((request.form.get("confirm") or "").split()).lower() != school.name.lower():
            flash("The name you typed does not match, so nothing was deleted.", "error")
            return redirect(url_for("admin_schools"))
        drop_checkins = bool(request.form.get("drop_checkins"))
        name = school.name

        def change():
            d.SupportRequest.query.filter_by(school_id=sid).delete(synchronize_session=False)
            d.Manager.query.filter_by(school_id=sid).delete(synchronize_session=False)
            checkins = d.Checkin.query.filter_by(school_id=sid)
            if drop_checkins:
                ids = [i for (i,) in db.session.query(d.Checkin.id).filter_by(school_id=sid)]
                for i in range(0, len(ids), 500):
                    d.CustomAnswer.query.filter(d.CustomAnswer.checkin_id.in_(ids[i:i + 500])
                                                ).delete(synchronize_session=False)
                checkins.delete(synchronize_session=False)
            else:
                checkins.update({"school_id": None}, synchronize_session=False)
            db.session.delete(school)

        save(change, f"{name} deleted with its managers and contacts. " + (
            "Its check-ins were deleted too." if drop_checkins
            else "Its anonymous check-ins are kept under Unassigned."))
        return redirect(url_for("admin_schools"))

    @app.post("/admin/schools/<int:sid>/managers")
    @admin_only
    def admin_manager_add(sid):
        school = school_or_404(sid)
        user, e1 = clean_username(request.form.get("username"))
        pw, e2 = clean_password(request.form.get("password"))
        if not (e1 or e2) and d.Manager.query.filter_by(username=user).first():
            e1 = ["That username is already taken."]
        if e1 or e2:
            flash_all(e1 + e2)
            return redirect(url_for("admin_schools"))
        save(lambda: db.session.add(d.Manager(username=user, school_id=school.id, active=1,
                                              password_hash=generate_password_hash(pw))),
             f"Manager {user} added for {school.name}.")
        return redirect(url_for("admin_schools"))

    def manager_or_404(mid):
        m = db.session.get(d.Manager, mid)
        if m is None:
            abort(404)
        return m

    @app.post("/admin/managers/<int:mid>/password")
    @admin_only
    def admin_manager_password(mid):
        m = manager_or_404(mid)
        pw, errors = clean_password(request.form.get("password"))
        if errors:
            flash_all(errors)
        else:
            def change():
                m.password_hash = generate_password_hash(pw)
            save(change, f"New password set for {m.username}. They are signed out everywhere.")
        return redirect(url_for("admin_schools"))

    @app.post("/admin/managers/<int:mid>/toggle")
    @admin_only
    def admin_manager_toggle(mid):
        m = manager_or_404(mid)

        def change():
            m.active = 0 if m.active else 1
        save(change, f"{m.username} can no longer sign in." if m.active
             else f"{m.username} can sign in again.")
        return redirect(url_for("admin_schools"))

    @app.post("/admin/managers/<int:mid>/delete")
    @admin_only
    def admin_manager_delete(mid):
        m = manager_or_404(mid)
        save(lambda: db.session.delete(m), f"Manager {m.username} deleted.")
        return redirect(url_for("admin_schools"))

    # -------------------------------------------------------------- report ----
    def report_period(args):
        """(start, end) from ?from=&to=. Missing or bad dates mean this month up to today;
        dates given the wrong way round are swapped."""
        today = datetime.now(d.eat).date()
        try:
            start = date.fromisoformat(args.get("from", ""))
        except ValueError:
            start = today.replace(day=1)
        try:
            end = date.fromisoformat(args.get("to", ""))
        except ValueError:
            end = today
        if start > end:
            start, end = end, start
        return start, end

    def build_report(start, end):
        lo, hi = start.isoformat(), end.isoformat()
        rows = (db.session.query(d.Checkin.day, d.Checkin.hour, d.Checkin.score)
                .filter(d.Checkin.day >= lo, d.Checkin.day <= hi,
                        sfilter(d.Checkin.school_id)).all())
        summary = stats.report(rows)
        sq = d.SupportRequest.query.filter(d.SupportRequest.day >= lo,
                                           d.SupportRequest.day <= hi,
                                           sfilter(d.SupportRequest.school_id))
        if g.staff["role"] == "manager":
            sq = sq.filter(d.SupportRequest.released == 1)
        sup = sq.all()
        # Counts only: no contact details ever go into a report.
        summary["support"] = {
            "total": len(sup),
            "waiting": sum(1 for r in sup if not r.handled),
            "by_type": Counter(r.support_type or "Not given" for r in sup).most_common(),
            "by_channel": Counter(r.channel or "Not given" for r in sup).most_common(),
        }
        return summary

    @app.get("/admin/report")
    @staff_only
    def admin_report():
        start, end = report_period(request.args)
        today = datetime.now(d.eat).date()
        return page("report.html", "report",
                    p={"start": start, "end": end, "today": today},
                    r=build_report(start, end), levels=stats.LEVELS, weekdays=stats.WEEKDAYS,
                    parts=stats.PARTS,
                    csv_link=url_for("admin_report_csv", **{"from": start.isoformat(),
                                                             "to": end.isoformat()}, **sargs()))

    @app.get("/admin/report.csv")
    @staff_only
    def admin_report_csv():
        start, end = report_period(request.args)
        r = build_report(start, end)
        buf = io.StringIO()
        w = csv.writer(buf)

        def line(*cells):
            w.writerow([csv_safe(c) for c in cells])

        line("Mapenzi ya 30 check-in report")
        line("School", current_scope().label)
        line("From", start.isoformat())
        line("To", end.isoformat())
        line("Check-ins (submissions)", r["total"])
        line("Average level (1-5)", "" if r["mean"] is None else r["mean"])
        line()
        line("Level", "Level name", "Check-ins", "Share %")
        for n, name in stats.LEVELS.items():
            line(n, name, r["counts"][n - 1], "" if r["pct"][n - 1] is None else r["pct"][n - 1])
        line()
        line("Part of day", "Check-ins")
        for part in stats.PARTS:
            line(part, r["by_part"][part])
        line()
        line("Day of week", "Check-ins")
        for i, wd in enumerate(stats.WEEKDAYS):
            line(wd, r["by_weekday"][i])
        line()
        line("Support requests", r["support"]["total"])
        line("Waiting to be contacted", r["support"]["waiting"])
        for t, n in r["support"]["by_type"]:
            line("Type: " + t, n)
        for c, n in r["support"]["by_channel"]:
            line("Channel: " + c, n)
        name = f"mapenzi-report-{start.isoformat()}-to-{end.isoformat()}.csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
