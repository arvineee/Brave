"""Tests for multi-school support: school check-in pages, the single sign-in page, manager
scoping (a manager must never reach another school's data), and the school QR codes."""
import shutil
import sqlite3
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from app import Checkin, Manager, School, SupportRequest, create_app, db
from dashboard import make_slug, qr_svg

PASSWORD = "correct horse battery staple"
MGR_PW = "kagumo-secret-pw"


def valid(**over):
    d = {"score": 3, "energy": "Low", "sleep": "5-6.", "connection": 4,
         "kept_up": ["School deadlines"], "support_now": "No"}
    d.update(over)
    return d


@pytest.fixture
def app(tmp_path):
    a = create_app({
        "TESTING": True, "LICENSE_SECRET": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}",
        "ADMIN_PASSWORD_HASH": generate_password_hash(PASSWORD),
        "SECRET_KEY": "test-signing-key", "SESSION_COOKIE_SECURE": False,
    })
    a.test_client().get("/")   # starts the trial clock before any rows are inserted
    return a


@pytest.fixture
def world(app):
    """Two schools, a manager each, and data in both plus the general link."""
    with app.app_context():
        a = School(name="Kagumo High School", slug="kagumo-high", active=1)
        b = School(name="St. Mary's Girls", slug="st-marys-girls", active=1)
        db.session.add_all([a, b])
        db.session.flush()
        db.session.add_all([
            Manager(username="kagumo", school_id=a.id, active=1,
                    password_hash=generate_password_hash(MGR_PW)),
            Manager(username="marys", school_id=b.id, active=1,
                    password_hash=generate_password_hash(MGR_PW))])
        base = dict(day="2026-10-20", hour=9, energy="Low", sleep="5-6.", connection=4,
                    kept_up="School deadlines", support_now="No")
        for sid, score, n in ((a.id, 2, 3), (a.id, 5, 1), (b.id, 3, 2), (None, 4, 1)):
            for _ in range(n):
                db.session.add(Checkin(school_id=sid, score=score, **base))
        db.session.add_all([
            SupportRequest(day="2026-10-20", support_type="Counselling", channel="Phone",
                           contact="0711-KAGUMO", school_id=a.id),
            SupportRequest(day="2026-10-20", support_type="Counselling", channel="Phone",
                           contact="0722-MARYS", school_id=b.id)])
        db.session.commit()
        return {"a": a.id, "b": b.id}


def sign_in(app, username, password):
    c = app.test_client()
    r = c.post("/admin/login", data={"username": username, "password": password})
    assert r.status_code == 302, r.status_code
    with c.session_transaction("/admin/") as s:
        c.csrf = s["csrf"]
    return c


def post(c, url, **data):
    return c.post(url, data={"csrf": c.csrf, **data})


# ------------------------------------------------------- student side ----
def test_school_page_shows_name_and_is_not_indexed(app, world):
    r = app.test_client().get("/s/kagumo-high")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Kagumo High School" in html
    assert 'data-school="kagumo-high"' in html
    assert "noindex" in html and "noindex" in r.headers["X-Robots-Tag"]


def test_general_page_has_no_school(app, world):
    html = app.test_client().get("/checkin").get_data(as_text=True)
    assert "data-school" not in html and "Checking in for" not in html


def test_unknown_or_switched_off_school_is_404(app, world):
    c = app.test_client()
    assert c.get("/s/nope").status_code == 404
    with app.app_context():
        db.session.get(School, world["a"]).active = 0
        db.session.commit()
    assert c.get("/s/kagumo-high").status_code == 404
    assert c.post("/api/checkin", json=valid(school="kagumo-high")).status_code == 400


def test_checkin_is_saved_against_the_school(app, world):
    c = app.test_client()
    with app.app_context():
        before = Checkin.query.filter_by(school_id=world["b"]).count()
    assert c.post("/api/checkin", json=valid(school="st-marys-girls")).status_code == 204
    with app.app_context():
        assert Checkin.query.filter_by(school_id=world["b"]).count() == before + 1


def test_support_request_carries_the_school_but_no_checkin_link(app, world):
    body = valid(support_now="Yes", support_type="Personal therapy.", channel="WhatsApp.",
                 school="kagumo-high", request={"contact": "0700 000 000", "consent": True})
    assert app.test_client().post("/api/checkin", json=body).status_code == 204
    with app.app_context():
        r = SupportRequest.query.filter_by(contact="0700 000 000").one()
        assert r.school_id == world["a"]
        assert "checkin_id" not in SupportRequest.__table__.columns


def test_no_school_means_unassigned_and_bad_school_is_rejected(app, world):
    c = app.test_client()
    assert c.post("/api/checkin", json=valid()).status_code == 204
    with app.app_context():
        assert Checkin.query.filter_by(school_id=None).count() == 2
    for bad in ("nope", 5, ["x"], "x" * 80, ""):
        assert c.post("/api/checkin", json=valid(school=bad)).status_code == 400


def test_response_page_links_back_to_the_school(app, world):
    html = app.test_client().get("/response?score=2&school=kagumo-high").get_data(as_text=True)
    assert "/s/kagumo-high" in html
    html = app.test_client().get("/response?score=2&school=bogus").get_data(as_text=True)
    assert "/s/" not in html


def test_old_database_upgrades_itself(tmp_path):
    """A database from before this version gets the new column and keeps its rows."""
    old = tmp_path / "old.db"
    src = Path(__file__).parent / "instance" / "mapenzi.db"
    if not src.exists():
        pytest.skip("no sample database")
    shutil.copy(src, old)
    con = sqlite3.connect(old)
    have_cols = {r[1] for r in con.execute("pragma table_info(checkins)")}
    n_before = con.execute("select count(*) from checkins").fetchone()[0]
    con.close()
    a = create_app({"TESTING": True, "LICENSE_SECRET": "s",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{old}"})
    with a.app_context():
        assert Checkin.query.count() == n_before
        assert Checkin.query.filter(Checkin.school_id.isnot(None)).count() == 0
    con = sqlite3.connect(old)
    assert "school_id" in {r[1] for r in con.execute("pragma table_info(checkins)")}
    assert "school_id" in {r[1] for r in con.execute("pragma table_info(support_requests)")}
    con.close()
    assert have_cols  # (the sample may or may not already carry the column)


# ------------------------------------------------------------- sign in ----
def test_sign_in_page_gives_no_hint_about_account_types(app):
    html = app.test_client().get("/admin/login").get_data(as_text=True).lower()
    assert 'name="username"' in html and 'name="password"' in html
    for word in ("manager", "school", "administrator"):
        assert word not in html


def test_admin_signs_in_with_admin_or_blank_username(app, world):
    for u in ("admin", "", "  ADMIN "):
        c = sign_in(app, u, PASSWORD)
        assert c.get("/admin/schools").status_code == 200


def test_manager_signs_in_and_cannot_use_admin_pages(app, world):
    c = sign_in(app, "Kagumo", MGR_PW)   # user names are not case sensitive
    assert c.get("/admin/").status_code == 200
    for path in ("/admin/schools", "/admin/questions", "/admin/contacts", "/admin/qr.svg",
                 "/admin/qr/download.svg"):
        assert c.get(path).status_code == 403, path
    for path, data in (("/admin/schools", {"name": "X School"}), ("/admin/questions/reset", {}),
                       ("/admin/contacts", {"section": "crisis"}), ("/admin/qr", {"url": "https://x.org/c"})):
        assert c.post(path, data={"csrf": c.csrf, **data}).status_code == 403, path


def test_failures_look_identical_and_manager_password_is_not_admin_password(app, world):
    c = app.test_client()
    bodies = set()
    for u, p in (("kagumo", "wrong"), ("nobody-here", "wrong"), ("admin", "wrong"),
                 ("kagumo", PASSWORD), ("marys", PASSWORD)):
        r = c.post("/admin/login", data={"username": u, "password": p})
        assert r.status_code == 403
        bodies.add(r.get_data(as_text=True))
    assert len(bodies) == 1
    # a manager's password does not open the admin account either
    assert c.post("/admin/login", data={"username": "admin", "password": MGR_PW}).status_code == 403


def test_switched_off_manager_cannot_sign_in_or_stay_signed_in(app, world):
    c = sign_in(app, "kagumo", MGR_PW)
    with app.app_context():
        Manager.query.filter_by(username="kagumo").one().active = 0
        db.session.commit()
    assert c.get("/admin/").status_code == 302      # sent back to the sign-in page
    assert app.test_client().post("/admin/login", data={"username": "kagumo",
                                                       "password": MGR_PW}).status_code == 403


def test_password_reset_signs_the_manager_out(app, world):
    m = sign_in(app, "kagumo", MGR_PW)
    admin = sign_in(app, "admin", PASSWORD)
    with app.app_context():
        mid = Manager.query.filter_by(username="kagumo").one().id
    assert post(admin, f"/admin/managers/{mid}/password", password="a brand new password").status_code == 302
    assert m.get("/admin/").status_code == 302
    assert app.test_client().post("/admin/login", data={"username": "kagumo",
                                  "password": "a brand new password"}).status_code == 302


def test_lockout_is_per_account(app, world):
    c = app.test_client()
    for _ in range(10):
        c.post("/admin/login", data={"username": "kagumo", "password": "nope"})
    assert c.post("/admin/login", data={"username": "kagumo", "password": MGR_PW}).status_code == 429
    assert c.post("/admin/login", data={"username": "marys", "password": MGR_PW}).status_code == 302


# ------------------------------------------------------------- scoping ----
def test_manager_sees_only_their_school(app, world):
    c = sign_in(app, "kagumo", MGR_PW)
    over = c.get("/admin/").get_data(as_text=True)
    assert "Kagumo High School" in over and "St. Mary" not in over
    # 4 check-ins at Kagumo (3 + 1 in crisis); the site holds 7 in total
    assert '<p class="adm-stat-label">All time</p><p class="adm-stat-num">4</p>' in over

    rows = c.get("/admin/responses").get_data(as_text=True)
    assert "4 check-ins." in rows

    csv = c.get("/admin/responses.csv").get_data(as_text=True)
    assert csv.count("Kagumo High School") == 4
    assert "St. Mary" not in csv and "Unassigned" not in csv

    sup = c.get("/admin/support").get_data(as_text=True)
    assert "0711-KAGUMO" in sup and "0722-MARYS" not in sup

    rep = c.get("/admin/report?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Kagumo High School" in rep
    rcsv = c.get("/admin/report.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)
    assert "Check-ins (submissions),4" in rcsv and "School,Kagumo High School" in rcsv


def test_manager_cannot_widen_the_scope_with_the_url(app, world):
    c = sign_in(app, "kagumo", MGR_PW)
    for q in (f"?school={world['b']}", "?school=none", "?school=", f"?school={world['a']}&school={world['b']}"):
        assert "4 check-ins." in c.get("/admin/responses" + q).get_data(as_text=True), q
        csv = c.get("/admin/responses.csv" + q).get_data(as_text=True)
        assert "St. Mary" not in csv and "Unassigned" not in csv, q
        assert "0722-MARYS" not in c.get("/admin/support" + q).get_data(as_text=True), q
        assert "Check-ins (submissions),4" in c.get(
            "/admin/report.csv" + q + "&from=2026-10-01&to=2026-10-31" if q else
            "/admin/report.csv?from=2026-10-01&to=2026-10-31").get_data(as_text=True)


def test_manager_cannot_touch_another_schools_support_request(app, world):
    with app.app_context():
        other = SupportRequest.query.filter_by(contact="0722-MARYS").one().id
        mine = SupportRequest.query.filter_by(contact="0711-KAGUMO").one().id
    c = sign_in(app, "kagumo", MGR_PW)
    assert post(c, f"/admin/support/{other}/handled").status_code == 404
    assert post(c, f"/admin/support/{other}/delete").status_code == 404
    with app.app_context():
        assert db.session.get(SupportRequest, other).handled == 0
    assert post(c, f"/admin/support/{mine}/handled").status_code == 302
    with app.app_context():
        assert db.session.get(SupportRequest, mine).handled == 1
    # "delete all contacted" only removes this school's
    with app.app_context():
        db.session.get(SupportRequest, other).handled = 1
        db.session.commit()
    post(c, "/admin/support/purge-handled")
    with app.app_context():
        assert db.session.get(SupportRequest, other) is not None
        assert db.session.get(SupportRequest, mine) is None


def test_manager_qr_is_only_their_own(app, world):
    c = sign_in(app, "kagumo", MGR_PW)
    r = c.get("/admin/qr")
    assert r.status_code == 302 and f"/admin/schools/{world['a']}/qr" in r.headers["Location"]
    assert c.get(f"/admin/schools/{world['a']}/qr").status_code == 200
    for path in (f"/admin/schools/{world['b']}/qr", f"/admin/schools/{world['b']}/qr.svg",
                 f"/admin/schools/{world['b']}/qr/download.svg"):
        assert c.get(path).status_code == 404, path
    svg = c.get(f"/admin/schools/{world['a']}/qr.svg")
    assert svg.mimetype == "image/svg+xml" and b"Kagumo High School" in svg.data


def test_custom_question_results_are_scoped(app, world):
    from app import CustomAnswer, CustomQuestion
    with app.app_context():
        q = CustomQuestion(prompt="Do you feel safe?", kind="choice", options="Yes\nNo",
                           required=0, active=1, position=1)
        db.session.add(q)
        db.session.flush()
        for c in Checkin.query.all():
            db.session.add(CustomAnswer(checkin_id=c.id, question_id=q.id,
                                        answer="Yes" if c.school_id == world["a"] else "No"))
        db.session.commit()
    c = sign_in(app, "kagumo", MGR_PW)
    over = c.get("/admin/").get_data(as_text=True)
    assert "Do you feel safe?" in over
    # all four Kagumo answers are "Yes"; no "No" answers leak in from the other schools
    assert "No</" not in over.split("Do you feel safe?")[1][:600]


def test_admin_sees_everything_and_can_filter(app, world):
    c = sign_in(app, "admin", PASSWORD)
    over = c.get("/admin/").get_data(as_text=True)
    assert '<p class="adm-stat-label">All time</p><p class="adm-stat-num">7</p>' in over
    assert "By school" in over and "Unassigned" in over
    assert "4 check-ins." in c.get(f"/admin/responses?school={world['a']}").get_data(as_text=True)
    assert "1 check-in." in c.get("/admin/responses?school=none").get_data(as_text=True)
    csv = c.get("/admin/responses.csv").get_data(as_text=True)
    assert "Kagumo High School" in csv and "St. Mary" in csv and "Unassigned" in csv
    sup = c.get("/admin/support").get_data(as_text=True)
    assert "0711-KAGUMO" in sup and "0722-MARYS" in sup
    assert "0722-MARYS" not in c.get(f"/admin/support?school={world['a']}").get_data(as_text=True)
    assert "Check-ins (submissions),2" in c.get(
        f"/admin/report.csv?from=2026-10-01&to=2026-10-31&school={world['b']}").get_data(as_text=True)


# ----------------------------------------------------- schools admin ----
def test_admin_adds_school_and_manager(app):
    c = sign_in(app, "admin", PASSWORD)
    assert post(c, "/admin/schools", name="Murang'a Boys' High School").status_code == 302
    assert post(c, "/admin/schools", name="Murang'a Boys' High School").status_code == 302
    with app.app_context():
        slugs = sorted(s.slug for s in School.query)
        assert slugs == ["murang-a-boys-high-school", "murang-a-boys-high-school-2"]
        sid = School.query.first().id
    assert post(c, f"/admin/schools/{sid}/managers", username="Jane.W", password="long enough pw").status_code == 302
    m = sign_in(app, "jane.w", "long enough pw")
    assert m.get("/admin/").status_code == 200
    assert "Murang&#39;a Boys&#39; High School" in m.get("/admin/").get_data(as_text=True)


@pytest.mark.parametrize("username,password", [
    ("admin", "long enough pw"), ("ADMIN", "long enough pw"), ("a", "long enough pw"),
    ("has space", "long enough pw"), ("ok.name", "short"), ("kagumo", "long enough pw")])
def test_bad_manager_details_are_refused(app, world, username, password):
    c = sign_in(app, "admin", PASSWORD)
    before = None
    with app.app_context():
        before = Manager.query.count()
    post(c, f"/admin/schools/{world['a']}/managers", username=username, password=password)
    with app.app_context():
        assert Manager.query.count() == before


@pytest.mark.parametrize("name", ["", "A", "<script>alert(1)</script>", "x" * 61, "Bad\u0000Name"])
def test_bad_school_names_are_refused(app, name):
    c = sign_in(app, "admin", PASSWORD)
    post(c, "/admin/schools", name=name)
    with app.app_context():
        assert School.query.count() == 0


def test_switching_a_school_off_keeps_data_and_manager_access(app, world):
    c = sign_in(app, "admin", PASSWORD)
    m = sign_in(app, "kagumo", MGR_PW)
    assert post(c, f"/admin/schools/{world['a']}/toggle").status_code == 302
    assert app.test_client().get("/s/kagumo-high").status_code == 404
    assert "4 check-ins." in m.get("/admin/responses").get_data(as_text=True)


def test_rename_changes_the_name_but_never_the_address(app, world):
    c = sign_in(app, "admin", PASSWORD)
    post(c, f"/admin/schools/{world['a']}/rename", name="Kagumo Secondary")
    html = app.test_client().get("/s/kagumo-high").get_data(as_text=True)
    assert "Kagumo Secondary" in html


# ------------------------------------------------------------ QR / slug ----
def test_qr_svg_prints_the_school_name_escaped_and_wrapped():
    svg = qr_svg("https://x.org/s/a", "St. Mary's Girls & Boys <High> School of Murang'a County").decode()
    assert "&amp;" in svg and "&lt;High&gt;" in svg and "<High>" not in svg
    assert svg.count("<text") <= 4 and "#IAMBRAVE" in svg
    plain = qr_svg("https://x.org/checkin").decode()   # the general code is unchanged
    assert plain.count("<text") == 1 and "#IAMBRAVE" in plain


def test_slug_is_ascii_and_unique():
    assert make_slug("Kagumo High School", set()) == "kagumo-high-school"
    assert make_slug("Kagumo High School", {"kagumo-high-school"}) == "kagumo-high-school-2"
    assert make_slug("Ñandú  Sch.", set()) == "nandu-sch"
    assert make_slug("(((", set()) == "school"
