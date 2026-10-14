"""Tests for the staff dashboard (/admin) and the editable site content."""
import re
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

import content
from app import Checkin, CustomAnswer, CustomQuestion, SupportRequest, create_app, db

PASSWORD = "correct horse battery staple"


@pytest.fixture
def app(tmp_path):
    return create_app({
        "TESTING": True, "LICENSE_SECRET": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}",
        "ADMIN_PASSWORD_HASH": generate_password_hash(PASSWORD),
        "SECRET_KEY": "test-signing-key", "SESSION_COOKIE_SECURE": False,
    })


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(client):
    r = client.post("/admin/login", data={"password": PASSWORD})
    assert r.status_code == 302
    with client.session_transaction() as s:
        token = s["csrf"]
    client.csrf = token
    return client


def post(c, url, **data):
    return c.post(url, data={"csrf": c.csrf, **data})


def valid(**over):
    d = {"score": 3, "energy": "Low", "sleep": "5-6.", "connection": 4,
         "kept_up": ["School deadlines"], "support_now": "No"}
    d.update(over)
    return d


def builtin_form(**over):
    d = {f"prompt_{k}": v for k, v in content.PROMPTS.items()}
    for g in content.EDITABLE_LABELS:
        d[f"label_{g}"] = [label for label, _ in content.FORM[g]]
    d.update(over)
    return d


# ---------------------------------------------------------------- access ----
def test_admin_is_off_unless_configured(tmp_path):
    a = create_app({"TESTING": True, "LICENSE_SECRET": "s",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'x.db'}"})
    c = a.test_client()
    for path in ("/admin/", "/admin/login", "/admin/support", "/admin/questions"):
        assert c.get(path).status_code == 404


def test_pages_need_login(client):
    for path in ("/admin/", "/admin/responses", "/admin/responses.csv", "/admin/support",
                 "/admin/questions", "/admin/contacts"):
        r = client.get(path)
        assert r.status_code == 302 and "/admin/login" in r.headers["Location"]


def test_wrong_password_rejected_right_one_logs_in(client):
    assert client.post("/admin/login", data={"password": "nope"}).status_code == 403
    assert client.get("/admin/").status_code == 302
    assert client.post("/admin/login", data={"password": PASSWORD}).status_code == 302
    assert client.get("/admin/").status_code == 200


def test_login_is_throttled(client):
    for _ in range(10):
        client.post("/admin/login", data={"password": "bad"})
    assert client.post("/admin/login", data={"password": PASSWORD}).status_code == 429


def test_cookie_is_limited_to_admin_and_public_pages_stay_cookie_free(client):
    r = client.post("/admin/login", data={"password": PASSWORD})
    cookie = r.headers["Set-Cookie"]
    assert "Path=/admin" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    for path in ("/", "/checkin", "/response?score=5", "/robots.txt"):
        assert "Set-Cookie" not in client.get(path).headers


def test_admin_pages_are_noindex_and_not_cached(admin):
    r = admin.get("/admin/")
    assert "noindex" in r.headers["X-Robots-Tag"] and r.headers["Cache-Control"] == "no-store"
    assert "Disallow: /admin/" in admin.get("/robots.txt").get_data(as_text=True)


def test_post_without_csrf_token_is_refused(admin):
    assert admin.post("/admin/questions/reset").status_code == 400
    assert admin.post("/admin/questions/reset", data={"csrf": "wrong"}).status_code == 400
    assert admin.post("/admin/logout", data={}).status_code == 400


def test_logout_ends_the_session(admin):
    assert post(admin, "/admin/logout").status_code == 302
    assert admin.get("/admin/").status_code == 302


# -------------------------------------------------------- stored columns ----
def test_checkin_records_the_hour_and_nothing_more_precise(app, client):
    assert client.post("/api/checkin", json=valid()).status_code == 204
    with app.app_context():
        row = Checkin.query.one()
        assert isinstance(row.hour, int) and 0 <= row.hour <= 23
        assert not any(c.name in ("minute", "created_at", "timestamp") for c in Checkin.__table__.columns)


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE checkins (id INTEGER PRIMARY KEY, day VARCHAR(10) NOT NULL, score INTEGER NOT NULL,
          energy VARCHAR(60) NOT NULL, sleep VARCHAR(30) NOT NULL, connection INTEGER NOT NULL,
          kept_up VARCHAR(400) NOT NULL, support_now VARCHAR(20) NOT NULL, support_type VARCHAR(80), channel VARCHAR(20));
        CREATE TABLE support_requests (id INTEGER PRIMARY KEY, day VARCHAR(10) NOT NULL,
          support_type VARCHAR(80) NOT NULL, channel VARCHAR(20) NOT NULL, contact VARCHAR(120) NOT NULL);
        INSERT INTO checkins (day, score, energy, sleep, connection, kept_up, support_now) VALUES ('2026-10-01', 2, 'Low', '5-6.', 4, 'Screen time.', 'No');
        INSERT INTO support_requests (day, support_type, channel, contact) VALUES ('2026-10-01', 'Other:', 'SMS', '0700');
    """)
    con.commit(); con.close()
    a = create_app({"TESTING": True, "LICENSE_SECRET": "s", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}"})
    with a.app_context():
        row = Checkin.query.one()
        assert row.hour is None and row.score == 2          # old data kept, no time recorded
        assert SupportRequest.query.one().handled == 0
        from sqlalchemy import inspect
        assert {"hour"} <= {c["name"] for c in inspect(db.engine).get_columns("checkins")}


# ------------------------------------------------------------- dashboard ----
def test_overview_with_and_without_data(app, admin):
    assert "No check-ins yet" in admin.get("/admin/").get_data(as_text=True)
    for score in (1, 3, 5, 5):
        admin.post("/api/checkin", json=valid(score=score))
    html = admin.get("/admin/?range=all").get_data(as_text=True)
    assert "In crisis (level 5)" in html and "Time of day" in html and "<svg" in html
    assert "style=" not in html
    assert admin.get("/admin/?range=garbage").status_code == 200


def test_responses_list_filter_and_csv(app, admin):
    admin.post("/api/checkin", json=valid(score=4))
    admin.post("/api/checkin", json=valid(score=1))
    html = admin.get("/admin/responses").get_data(as_text=True)
    assert "never sent to the server" in html and html.count('class="adm-row"') == 2
    assert admin.get("/admin/responses?score=4").get_data(as_text=True).count('class="adm-row"') == 1
    r = admin.get("/admin/responses.csv")
    assert r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    lines = r.get_data(as_text=True).strip().splitlines()
    assert len(lines) == 3 and lines[0].startswith("Date,Part of day,Level")
    assert "Hour" not in lines[0] and "Time" not in lines[0].replace("Part of day", "")  # no exact time


def test_support_requests_toggle_delete_and_purge(app, admin):
    body = valid(support_now="Yes", support_type="Personal therapy.", channel="SMS",
                 request={"contact": "0711 222 333", "consent": True})
    admin.post("/api/checkin", json=body)
    assert "0711 222 333" in admin.get("/admin/support").get_data(as_text=True)
    with app.app_context():
        rid = SupportRequest.query.one().id
    post(admin, f"/admin/support/{rid}/handled")
    with app.app_context():
        assert SupportRequest.query.one().handled == 1
    post(admin, "/admin/support/purge-handled")
    with app.app_context():
        assert SupportRequest.query.count() == 0 and Checkin.query.count() == 1
    assert post(admin, "/admin/support/999/delete").status_code == 404


# ------------------------------------------------------------- questions ----
def test_reworded_questions_go_live_and_old_pages_still_submit(admin):
    new = ["Very high", "Fine", "Okay but tired", "Low", "Wiped out"]
    form = builtin_form(prompt_q1="How is your energy this week?", label_energy=new)
    assert post(admin, "/admin/questions/builtin", **form).status_code == 302
    html = admin.get("/checkin").get_data(as_text=True)
    assert "1. How is your energy this week?" in html
    assert '<option value="3">Wiped out</option>' in html and "Exhausted." not in html
    assert admin.post("/api/checkin", json=valid(energy="Wiped out")).status_code == 204
    assert admin.post("/api/checkin", json=valid(energy="Exhausted.")).status_code == 204   # stale page
    assert admin.post("/api/checkin", json=valid(energy="Made up")).status_code == 400


def test_invalid_question_edit_changes_nothing(admin):
    assert post(admin, "/admin/questions/builtin", **builtin_form(prompt_q1="x")).status_code == 302
    assert content.PROMPTS["q1"] in admin.get("/checkin").get_data(as_text=True)


def test_restore_original_wording(admin):
    post(admin, "/admin/questions/builtin", **builtin_form(prompt_q5="Something good this week"))
    assert "Something good this week" in admin.get("/checkin").get_data(as_text=True)
    post(admin, "/admin/questions/reset")
    assert content.PROMPTS["q5"] in admin.get("/checkin").get_data(as_text=True)


def test_scoring_points_cannot_be_changed_from_the_dashboard(admin):
    post(admin, "/admin/questions/builtin", **builtin_form(label_energy=["a", "b", "c", "d", "e"]))
    html = admin.get("/checkin").get_data(as_text=True)
    assert '<option value="0">a</option>' in html and '<option value="3">e</option>' in html


def test_custom_question_lifecycle(app, admin):
    r = post(admin, "/admin/questions/custom", prompt="Do you feel safe at school?", kind="choice",
             options="Yes\nSometimes\nNo", required="1")
    assert r.status_code == 302
    with app.app_context():
        q = CustomQuestion.query.one()
        qid = q.id
    html = admin.get("/checkin").get_data(as_text=True)
    assert "11. Do you feel safe at school?" in html and f'data-cq="{qid}"' in html
    assert set(re.findall(r"<(?:input|select|textarea)\b[^>]*\sname=\"(\w+)\"", html)) == {"score", "connection"}

    assert admin.post("/api/checkin", json=valid(custom={str(qid): "Sometimes"})).status_code == 204
    assert admin.post("/api/checkin", json=valid(custom={str(qid): "Maybe"})).status_code == 400
    assert admin.post("/api/checkin", json=valid(custom="x")).status_code == 400
    with app.app_context():
        assert [a.answer for a in CustomAnswer.query.all()] == ["Sometimes"]
        assert Checkin.query.count() == 1
    assert "Do you feel safe at school?" in admin.get("/admin/?range=all").get_data(as_text=True)
    assert "Sometimes" in admin.get("/admin/responses").get_data(as_text=True)
    assert "Do you feel safe at school?" in admin.get("/admin/responses.csv").get_data(as_text=True)

    post(admin, f"/admin/questions/custom/{qid}/toggle")           # hide
    assert "Do you feel safe" not in admin.get("/checkin").get_data(as_text=True)
    post(admin, f"/admin/questions/custom/{qid}/toggle")           # show again
    post(admin, f"/admin/questions/custom/{qid}/edit", prompt="Do you feel safe on campus?",
         kind="choice", options="Yes\nNo", position="1")
    assert "Do you feel safe on campus?" in admin.get("/checkin").get_data(as_text=True)

    post(admin, f"/admin/questions/custom/{qid}/delete")
    with app.app_context():
        assert CustomQuestion.query.count() == 0 and CustomAnswer.query.count() == 0
        assert Checkin.query.count() == 1                           # the check-in itself stays


def test_custom_question_validation_and_limit(app, admin):
    for bad in (dict(prompt="Hi", kind="choice", options="a\nb"), dict(prompt="Free text?", kind="text", options=""),
                dict(prompt="One option", kind="choice", options="only")):
        post(admin, "/admin/questions/custom", **bad)
    with app.app_context():
        assert CustomQuestion.query.count() == 0
    for i in range(12):
        post(admin, "/admin/questions/custom", prompt=f"Question number {i}", kind="scale")
    with app.app_context():
        assert CustomQuestion.query.count() == 10


def test_scale_and_multi_custom_answers(app, admin):
    post(admin, "/admin/questions/custom", prompt="How is the canteen?", kind="scale", scale_low="Bad", scale_high="Great")
    post(admin, "/admin/questions/custom", prompt="Which clubs do you attend?", kind="multi", options="Drama\nSports\nDebate")
    with app.app_context():
        scale, multi = [q.id for q in CustomQuestion.query.order_by(CustomQuestion.id)]
    ok = valid(custom={str(scale): 7, str(multi): ["Drama", "Debate"]})
    assert admin.post("/api/checkin", json=ok).status_code == 204
    assert admin.post("/api/checkin", json=valid(custom={str(scale): 11})).status_code == 400
    assert admin.post("/api/checkin", json=valid(custom={str(multi): []})).status_code == 400
    with app.app_context():
        assert sorted(a.answer for a in CustomAnswer.query) == ["7", "Debate", "Drama"]


# ------------------------------------------------- contacts and socials ----
def test_crisis_details_can_be_edited_and_show_everywhere(admin):
    r = post(admin, "/admin/contacts", section="crisis", name="Befrienders Kenya", phone_display="0700 000 111",
             counsellor_prompt="Speak to your campus counsellor in confidence.", footer_tag="#IAMBRAVE • Murang'a County")
    assert r.status_code == 302
    for path in ("/", "/checkin", "/response?score=5", "/response?score=1"):
        html = admin.get(path).get_data(as_text=True)
        assert 'id="crisis"' in html and "0700 000 111" in html and 'href="tel:+254700000111"' in html
        assert "0722 178 177" not in html


def test_invalid_crisis_edit_is_refused_and_defaults_stay(admin):
    post(admin, "/admin/contacts", section="crisis", name="", phone_display="not a number",
         counsellor_prompt="", footer_tag="")
    html = admin.get("/response?score=5").get_data(as_text=True)
    assert "0722 178 177" in html and "Befrienders Kenya" in html


def test_crisis_resources_survive_a_settings_table_failure(app, client, monkeypatch):
    from sqlalchemy.exc import OperationalError
    import app as appmod

    class Boom:
        @property
        def query(self):
            raise OperationalError("x", {}, Exception("down"))
    monkeypatch.setattr(appmod, "SiteSetting", Boom())
    html = client.get("/response?score=5").get_data(as_text=True)
    assert "0722 178 177" in html and 'id="crisis"' in html


def test_contacts_and_socials_show_in_the_footer(admin):
    post(admin, "/admin/contacts", section="contacts", email="hello@brave.example", phone="0700 111 222")
    post(admin, "/admin/contacts", section="socials", social_platform=["Instagram", "TikTok", "X"],
         social_url=["https://www.instagram.com/iambrave", "", ""], social_label=["@iambrave", "", ""])
    html = admin.get("/").get_data(as_text=True)
    assert "mailto:hello@brave.example" in html and "tel:+254700111222" in html
    assert 'class="social-link" href="https://www.instagram.com/iambrave"' in html
    assert 'rel="noopener noreferrer"' in html and "TikTok" not in html


def test_bad_social_links_are_refused(admin):
    for url in ("http://insecure.example", "javascript:alert(1)", "https://u@evil.example"):
        post(admin, "/admin/contacts", section="socials", social_platform=["Website"], social_url=[url], social_label=[""])
    assert "social-link" not in admin.get("/").get_data(as_text=True)
    assert post(admin, "/admin/contacts", section="nonsense").status_code == 400


def test_admin_is_locked_with_the_rest_of_the_site_when_the_trial_ends(app, admin):
    from test_app import _age_trial
    _age_trial(app, 41)
    r = admin.get("/admin/")
    assert r.status_code == 302 and "/locked" in r.headers["Location"]


# ------------------------------------------------------------ QR code ----
def save_url(c, url):
    return c.post("/admin/qr", data={"csrf": c.csrf, "url": url})


def test_qr_is_drawn_from_the_saved_address_and_follows_changes(admin):
    r = admin.get("/admin/qr.svg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.get_data()
    assert "/checkin" in admin.get("/admin/qr").get_data(as_text=True)  # default before any save

    assert save_url(admin, "https://old.example.org/checkin").status_code == 302
    old = admin.get("/admin/qr.svg").get_data()
    assert save_url(admin, "https://new.example.org/checkin").status_code == 302
    new = admin.get("/admin/qr.svg").get_data()
    assert old != new  # the picture really is generated from the stored address
    assert "https://new.example.org/checkin" in admin.get("/admin/qr").get_data(as_text=True)

    dl = admin.get("/admin/qr/download.svg")
    assert "attachment" in dl.headers["Content-Disposition"] and dl.get_data() == new


def test_bad_qr_address_is_refused_and_the_old_one_stays(admin):
    save_url(admin, "https://good.example.org/checkin")
    good = admin.get("/admin/qr.svg").get_data()
    for bad in ("", "good.example.org", "https://bad address.org", "ftp://x.org/c",
                "https://user@evil.org/c"):
        save_url(admin, bad)
    assert admin.get("/admin/qr.svg").get_data() == good


def test_qr_and_report_pages_need_login(client):
    for path in ("/admin/qr", "/admin/qr.svg", "/admin/report", "/admin/report.csv"):
        r = client.get(path)
        assert r.status_code == 302 and "/admin/login" in r.headers["Location"]


def test_staff_sign_in_button_only_when_dashboard_is_on(app, client, tmp_path):
    assert "Staff sign in" in client.get("/").get_data(as_text=True)
    off = create_app({"TESTING": True, "LICENSE_SECRET": "s",
                      "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'off.db'}"})
    assert "Staff sign in" not in off.test_client().get("/").get_data(as_text=True)


# ------------------------------------------------------------- report ----
def _checkin(day, score, hour=9):
    return Checkin(day=day, hour=hour, score=score, energy="Low", sleep="5-6.", connection=4,
                   kept_up="School deadlines", support_now="No")


def test_report_counts_the_period_and_never_shows_contacts(app, admin):
    with app.app_context():
        db.session.add_all([_checkin("2026-10-02", 1), _checkin("2026-10-03", 5, hour=23),
                            _checkin("2025-01-01", 3),  # outside the period
                            SupportRequest(day="2026-10-02", support_type="Personal therapy.",
                                           channel="SMS", contact="0711 222 333")])
        db.session.commit()
    csv = admin.get("/admin/report.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Check-ins (submissions),2" in csv
    assert "Support requests,1" in csv and "Waiting to be contacted,1" in csv
    assert "0711 222 333" not in csv
    html = admin.get("/admin/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "0711 222 333" not in html and "Mapenzi ya 30 check-in report" in html
    assert "Personal therapy." in html


def test_report_bad_or_reversed_dates_fall_back_safely(admin):
    assert admin.get("/admin/report?from=nope&to=also-nope").status_code == 200
    csv = admin.get("/admin/report.csv?from=2026-10-31&to=2026-10-01").get_data(as_text=True)
    assert "From,2026-10-01" in csv and "To,2026-10-31" in csv
    assert admin.get("/admin/report").status_code == 200  # no dates at all: this month
