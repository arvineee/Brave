"""Staff dashboard for Brave: /admin

Off by default. It switches on only when BOTH are set in the server environment:
  ADMIN_PASSWORD_HASH   from `python admin.py hashpw`
  ADMIN_SECRET_KEY      a long random string that signs the login cookie
Without them every /admin URL returns 404, so a half-configured site is never open.

Privacy model (unchanged from the public site)
  * Public pages still set no cookies. The login cookie exists only after a staff login and is
    limited to the path /admin (HttpOnly, SameSite=Strict, Secure on https).
  * Rows are anonymous. Staff see day, a part of day, the level and the multiple-choice answers;
    never an exact time. Exact hours appear only as counts in the charts.
  * The two reflection boxes are never sent to the server, so there is nothing to read there.
  * Support requests (contacts) are the one sensitive list: shown only here, deletable.
"""
import csv
import hmac
import io
import json
import logging
import secrets
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Callable

from flask import Response, abort, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

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
    get_config: Callable
    eat: Any


def part_of_day(hour):
    if hour is None:
        return "Not recorded"
    if hour >= 23 or hour < 5:
        return "Late night"
    if hour < 12:
        return "Morning"
    if hour < 17:
        return "Afternoon"
    return "Evening"


def csv_safe(v):
    """Stop spreadsheet programs treating a cell as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def register(app, d):
    db = d.db
    failed = []  # timestamps of wrong passwords (no IP or other data kept)

    def enabled():
        return bool(app.config.get("ADMIN_PASSWORD_HASH") and app.config.get("SECRET_KEY"))

    def admin_only(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not enabled():
                abort(404)
            if not session.get("admin"):
                return redirect(url_for("admin_login"))
            if request.method == "POST":
                sent, have = request.form.get("csrf", ""), session.get("csrf", "")
                if not have or not hmac.compare_digest(sent, have):
                    abort(400)
            return view(*args, **kwargs)
        return wrapped

    def page(template, active, **ctx):
        return render_template("admin/" + template, active=active,
                               csrf=session.get("csrf", ""), **ctx)

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
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not enabled():
            abort(404)
        if session.get("admin"):
            return redirect(url_for("admin_overview"))
        error, status = None, 200
        if request.method == "POST":
            now = time.time()
            failed[:] = [t for t in failed if now - t < 600]
            if len(failed) >= 10:
                error, status = "Too many attempts. Please wait a few minutes.", 429
            elif check_password_hash(app.config["ADMIN_PASSWORD_HASH"],
                                     request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["admin"] = True
                session["csrf"] = secrets.token_urlsafe(24)
                return redirect(url_for("admin_overview"))
            else:
                failed.append(now)
                error, status = "That password was not accepted.", 403
        return render_template("admin/login.html", error=error), status

    @app.post("/admin/logout")
    @admin_only
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    # ----------------------------------------------------------- overview ----
    @app.get("/admin/")
    @admin_only
    def admin_overview():
        today = datetime.now(d.eat).date()
        raw = db.session.query(d.Checkin.day, d.Checkin.hour, d.Checkin.score).all()
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
                    custom_results=custom_results())

    def custom_results():
        questions = {q.id: q for q in d.CustomQuestion.query.order_by(d.CustomQuestion.position,
                                                                       d.CustomQuestion.id)}
        if not questions:
            return []
        per_q = defaultdict(Counter)
        for qid, answer, n in (db.session.query(d.CustomAnswer.question_id, d.CustomAnswer.answer,
                                                db.func.count())
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
        q = d.Checkin.query
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
    @admin_only
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
        items = [{
            "row": r, "part": part_of_day(r.hour), "level": stats.LEVELS[r.score],
            "custom": [(titles.get(qid, "Removed question"), ", ".join(a))
                       for qid, a in extra.get(r.id, {}).items()],
        } for r in rows]
        return page("responses.html", "responses", items=items, total=total, score=score,
                    page_no=pg, pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
                    levels=stats.LEVELS)

    @app.get("/admin/responses.csv")
    @admin_only
    def admin_responses_csv():
        q, _score = checkin_query()
        rows = q.order_by(d.Checkin.id.desc()).limit(CSV_LIMIT).all()
        extra = answers_for([r.id for r in rows])
        titles = {x.id: x.prompt for x in d.CustomQuestion.query}
        qids = sorted({qid for per in extra.values() for qid in per})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Date", "Part of day", "Level", "Level name", "Energy", "Sleep",
                    "Connection (0-10)", "Kept up by", "Wants support", "Support type", "Channel"]
                   + [titles.get(i, f"Question {i}") for i in qids])
        for r in rows:
            w.writerow([csv_safe(x) for x in (
                r.day, part_of_day(r.hour), r.score, stats.LEVELS[r.score], r.energy, r.sleep,
                r.connection, r.kept_up, r.support_now, r.support_type, r.channel)]
                + [csv_safe(", ".join(extra.get(r.id, {}).get(i, []))) for i in qids])
        name = f"mapenzi-checkins-{datetime.now(d.eat).date().isoformat()}.csv"
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------ support ----
    @app.get("/admin/support")
    @admin_only
    def admin_support():
        rows = (d.SupportRequest.query
                .order_by(d.SupportRequest.handled, d.SupportRequest.id.desc()).all())
        return page("support.html", "support", rows=rows,
                    waiting=sum(1 for r in rows if not r.handled))

    @app.post("/admin/support/<int:rid>/handled")
    @admin_only
    def admin_support_handled(rid):
        r = db.session.get(d.SupportRequest, rid)
        if r is None:
            abort(404)

        def change():
            r.handled = 0 if r.handled else 1
        save(change, "Updated.")
        return redirect(url_for("admin_support"))

    @app.post("/admin/support/<int:rid>/delete")
    @admin_only
    def admin_support_delete(rid):
        r = db.session.get(d.SupportRequest, rid)
        if r is None:
            abort(404)
        save(lambda: db.session.delete(r), "Request deleted.")
        return redirect(url_for("admin_support"))

    @app.post("/admin/support/purge-handled")
    @admin_only
    def admin_support_purge():
        n = d.SupportRequest.query.filter(d.SupportRequest.handled == 1).count()
        save(lambda: d.SupportRequest.query.filter(d.SupportRequest.handled == 1)
             .delete(synchronize_session=False), f"Deleted {n} handled request(s).")
        return redirect(url_for("admin_support"))

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
        return page("contacts.html", "contacts", cfg=cfg, rows=rows[:siteconfig.MAX_SOCIALS],
                    platforms=content.SOCIAL_PLATFORMS, max_socials=siteconfig.MAX_SOCIALS)

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
