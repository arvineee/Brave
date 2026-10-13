import json
import re
import shutil
import subprocess

import pytest

from app import Checkin, SupportRequest, clean_checkin, create_app, db


@pytest.fixture
def app(tmp_path):
    a = create_app({"TESTING": True, "LICENSE_SECRET": "test-secret",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 't.db'}"})
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def valid(**over):
    d = {"score": 3, "energy": "Low", "sleep": "5-6.", "connection": 4,
         "kept_up": ["School deadlines"], "support_now": "No"}
    d.update(over)
    return d


def wants(**over):
    d = valid(support_now="Yes", support_type="Personal therapy.", channel="WhatsApp.")
    d.update(over)
    return d


# ---------- pages ----------
def test_home_page(client):
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in html
    assert 'href="/checkin"' in html
    assert 'class="logo"' in html and "#IAMBRAVE" in html
    assert 'id="crisis"' in html and "0722 178 177" in html


@pytest.mark.parametrize("score,title", [(1, "Thriving"), (2, "Holding Steady"),
                                         (3, "Noticing Strain"), (4, "Struggling"),
                                         (5, "In Crisis")])
def test_each_score_has_distinct_title(client, score, title):
    html = client.get(f"/response?score={score}").get_data(as_text=True)
    assert f'class="state-title">{title}<' in html


@pytest.mark.parametrize("path", ["/", "/checkin"] + [f"/response?score={n}" for n in range(1, 6)])
def test_crisis_resources_on_every_page(client, path):
    html = client.get(path).get_data(as_text=True)
    assert 'id="crisis"' in html
    assert "0722 178 177" in html and "Befrienders Kenya" in html
    assert "campus counsellor" in html and "#IAMBRAVE" in html


@pytest.mark.parametrize("score", [1, 2, 3, 4, 5])
def test_response_page_never_hides_crisis(client, score):
    html = client.get(f"/response?score={score}").get_data(as_text=True)
    # no element is hidden from the user (decorative aria-hidden icons are fine)
    assert not re.search(r"(?<![-\w])hidden(?![-\w])", html) and "display:none" not in html
    assert "<script" not in html


def test_crisis_first_and_highlighted_on_score_5(client):
    html = client.get("/response?score=5").get_data(as_text=True)
    assert html.index('id="crisis"') < html.index('id="response-container"')
    assert "highlight-crisis" in html


def test_crisis_after_content_on_other_scores(client):
    html = client.get("/response?score=2").get_data(as_text=True)
    assert html.index('id="response-container"') < html.index('id="crisis"')
    assert "highlight-crisis" not in html


@pytest.mark.parametrize("q", ["", "?score=", "?score=abc", "?score=0", "?score=9", "?score=-1", "?score=3.5"])
def test_default_is_score_3(client, q):
    html = client.get("/response" + q).get_data(as_text=True)
    assert 'class="state-title">Noticing Strain<' in html


def test_checkin_has_all_ten_questions(client):
    html = client.get("/checkin").get_data(as_text=True)
    for n in range(1, 11):
        assert f"{n}. " in html
    assert "scoring.js" in html and "checkin.js" in html


def test_only_non_personal_fields_are_named(client):
    """If JavaScript fails and the form submits as a plain GET, nothing personal is in the URL."""
    html = client.get("/checkin").get_data(as_text=True)
    named = re.findall(r"<(?:input|select|textarea)\b[^>]*\sname=\"(\w+)\"", html)
    assert set(named) == {"score", "connection"}


def test_option_points_rendered(client):
    html = client.get("/checkin").get_data(as_text=True)
    assert '<option value="3">Exhausted.</option>' in html
    assert 'data-points="1" data-label="Money worries."' in html


# ---------- privacy / headers ----------
@pytest.mark.parametrize("path", ["/", "/checkin", "/response?score=4"])
def test_no_cookies_no_external_resources(client, path):
    r = client.get(path)
    assert "Set-Cookie" not in r.headers
    html = r.get_data(as_text=True)
    assert "http://" not in html and "https://" not in html
    assert "<script>" not in html  # no inline script
    assert "analytics" not in html.lower()
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_static_is_cacheable_pages_are_not(client):
    assert "max-age" in client.get("/static/style.css").headers["Cache-Control"]
    assert client.get("/").headers["Cache-Control"] == "no-store"


# ---------- responsive CSS ----------
def test_css_is_mobile_first_and_never_wider_than_360(client):
    css = client.get("/static/style.css").get_data(as_text=True)
    assert "#0d9488" in css
    assert "@media (min-width:400px)" in css and "@media (min-width:860px)" in css
    for m in re.finditer(r"(?<![-\w(])(?:min-)?width:\s*(\d+)px", css):
        assert int(m.group(1)) <= 360, m.group(0)
    assert "max-width:min" not in css


# ---------- storage ----------
def test_anonymous_checkin_is_stored(app, client):
    r = client.post("/api/checkin", json=valid())
    assert r.status_code == 204
    assert "Set-Cookie" not in r.headers
    with app.app_context():
        row = Checkin.query.one()
        assert (row.score, row.energy, row.sleep, row.connection) == (3, "Low", "5-6.", 4)
        assert row.kept_up == "School deadlines" and row.support_type is None
        assert SupportRequest.query.count() == 0


def test_free_text_and_extra_fields_are_never_stored(app, client):
    body = wants(text="my secret reflection", name="Jane", phone="0700000000")
    assert client.post("/api/checkin", json=body).status_code == 204
    with app.app_context():
        row = Checkin.query.one()
        dump = json.dumps({c.name: getattr(row, c.name) for c in Checkin.__table__.columns})
        assert "secret" not in dump and "Jane" not in dump and "0700000000" not in dump


def test_support_request_needs_consent_and_is_separate(app, client):
    ok = wants(request={"contact": " 0711 222 333 ", "consent": True})
    assert client.post("/api/checkin", json=ok).status_code == 204
    with app.app_context():
        assert Checkin.query.count() == 1
        sr = SupportRequest.query.one()
        assert sr.contact == "0711 222 333" and sr.channel == "WhatsApp."
        assert not hasattr(sr, "checkin_id") and not hasattr(sr, "score")
        assert "contact" not in Checkin.__table__.columns


@pytest.mark.parametrize("req", [
    {"contact": "0711", "consent": False},
    {"contact": "0711"},
    {"contact": "", "consent": True},
    {"contact": "x" * 121, "consent": True},
    {"contact": 123, "consent": True},
    "0711",
])
def test_support_request_rejected_without_valid_consent(app, client, req):
    assert client.post("/api/checkin", json=wants(request=req)).status_code == 400
    with app.app_context():
        assert Checkin.query.count() == 0 and SupportRequest.query.count() == 0


def test_contact_without_asking_for_support_is_rejected(client):
    r = client.post("/api/checkin", json=valid(request={"contact": "0711", "consent": True}))
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [
    valid(score=0), valid(score=6), valid(score="3"), valid(score=True),
    valid(energy="Fantastic"), valid(sleep="lots"), valid(connection=11),
    valid(connection=-1), valid(connection=2.5), valid(kept_up=[]),
    valid(kept_up=["Nope"]), valid(kept_up=["Screen time.", "Screen time."]),
    valid(support_now="Perhaps"),
    valid(support_now="Yes"),                      # missing support_type / channel
    wants(channel="Carrier pigeon"),
    [], "x", None,
])
def test_invalid_payloads_rejected(app, client, bad):
    r = client.post("/api/checkin", json=bad)
    assert r.status_code == 400
    with app.app_context():
        assert Checkin.query.count() == 0


def test_oversized_and_non_json_bodies_rejected(client):
    assert client.post("/api/checkin", data="x" * 5000, content_type="application/json").status_code == 400
    assert client.post("/api/checkin", data="not json", content_type="text/plain").status_code == 400


def test_no_support_answer_drops_support_fields():
    c, req = clean_checkin(valid(support_type="Personal therapy.", channel="SMS"))
    assert c["support_type"] is None and c["channel"] is None and req is None


def test_database_failure_does_not_crash(app, client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def boom(*a, **k):
        raise OperationalError("x", {}, Exception("db down"))
    monkeypatch.setattr(db.session, "commit", boom)
    assert client.post("/api/checkin", json=valid()).status_code == 503


def test_postgres_url_is_normalised(monkeypatch):
    from app import database_uri
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/d")
    assert database_uri() == "postgresql://u:p@h:5432/d"


# ---------- scoring rules (run the real scoring.js through Node) ----------
node = shutil.which("node")


def run_score(answers):
    code = "const s=require('./static/scoring.js');console.log(s.computeScore(%s))" % json.dumps(answers)
    return int(subprocess.check_output([node, "-e", code], text=True).strip())


@pytest.mark.skipif(not node, reason="node not installed")
@pytest.mark.parametrize("answers,expected", [
    ({"energy": 0, "sleep": 0, "support": 0, "connection": 10, "keptUp": [0], "text": ""}, 1),
    ({"energy": 1, "sleep": 1, "support": 0, "connection": 7, "keptUp": [], "text": ""}, 2),
    ({"energy": 2, "sleep": 2, "support": 1, "connection": 5, "keptUp": [1], "text": ""}, 3),
    ({"energy": 3, "sleep": 3, "support": 3, "connection": 5, "keptUp": [], "text": ""}, 4),
    ({"energy": 3, "sleep": 3, "support": 3, "connection": 0, "keptUp": [1, 1, 1], "text": ""}, 5),
    ({"energy": 0, "sleep": 0, "support": 0, "connection": 10, "keptUp": [1, 1, 1, 1], "text": ""}, 1),
    ({"energy": 0, "sleep": 0, "support": 0, "connection": 10, "keptUp": [], "text": "I want to die"}, 5),
    ({"energy": 0, "sleep": 0, "support": 0, "connection": 10, "keptUp": [], "text": "nataka kufa"}, 5),
])
def test_scoring(answers, expected):
    assert run_score(answers) == expected


# ---------- 40-day trial lock ----------
def _age_trial(app, days):
    """Test helper: move the start back by rewriting the row with a valid signature."""
    import trial
    from datetime import timedelta
    from sqlalchemy import text
    from app import License
    with app.app_context():
        client_state = app.test_client().get("/")  # starts the trial
        row = db.session.get(License, 1)
        started = trial.stamp(trial.parse(row.started_at) - timedelta(days=days))
        sig = trial.sign("test-secret", row.install_id, started, started, 0)
        # triggers forbid this on purpose; drop them just for the test
        db.session.execute(text("DROP TRIGGER license_no_tamper"))
        db.session.execute(text(
            "UPDATE license SET started_at=:s, last_seen=:s, sig=:g"), {"s": started, "g": sig})
        db.session.commit()
        return row.install_id


def test_trial_starts_and_site_is_open(client):
    assert client.get("/").status_code == 200


def test_expired_trial_locks_pages_but_keeps_crisis(app, client):
    _age_trial(app, 41)
    r = client.get("/checkin")
    assert r.status_code == 302 and r.headers["Location"].endswith("/locked")
    page = client.get("/locked").get_data(as_text=True)
    assert "0722 178 177" in page and 'id="crisis"' in page
    assert client.get("/response?score=5").status_code == 200
    assert client.post("/api/checkin", json=valid()).status_code == 503


def test_wrong_key_rejected_right_key_unlocks(app, client):
    import trial
    iid = _age_trial(app, 41)
    assert client.post("/unlock", data={"key": "AAAA-AAAA-AAAA-AAAA-AAAA"}).status_code == 403
    key = trial.activation_key("test-secret", iid)
    assert client.post("/unlock", data={"key": key}).status_code == 302
    assert client.get("/checkin").status_code == 200


def test_row_edits_are_blocked_by_database(app, client):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    client.get("/")
    with app.app_context():
        for sql in ("DELETE FROM license", "UPDATE license SET started_at='2099-01-01T00:00:00Z'"):
            with pytest.raises(SQLAlchemyError):
                db.session.execute(text(sql))
            db.session.rollback()


def test_hand_edited_row_without_secret_locks(app, client):
    from sqlalchemy import text
    client.get("/")
    with app.app_context():
        db.session.execute(text("UPDATE license SET activated=1"))  # allowed by trigger, bad sig
        db.session.commit()
    assert client.get("/checkin").status_code == 302


def test_missing_secret_fails_closed(tmp_path):
    a = create_app({"TESTING": True, "LICENSE_SECRET": "",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'n.db'}"})
    assert a.test_client().get("/checkin").status_code == 302


# ---------- SEO ----------
def test_home_and_checkin_are_indexable_with_meta(client):
    for path in ("/", "/checkin"):
        h = client.get(path).get_data(as_text=True)
        assert 'content="index, follow' in h and "noindex" not in h
        assert f'<link rel="canonical" href="http://localhost{path}">' in h
        assert 'property="og:image"' in h and 'name="twitter:card"' in h


def test_home_has_valid_jsonld(client):
    h = client.get("/").get_data(as_text=True)
    raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S).group(1)
    data = json.loads(raw.replace("\\u0027", "'"))
    assert {n["@type"] for n in data["@graph"]} == {"Organization", "WebSite"}


def test_private_pages_are_noindex(client):
    for path in ("/response?score=3", "/locked"):
        r = client.get(path)
        assert "noindex" in r.headers["X-Robots-Tag"]
        assert 'content="noindex, nofollow"' in r.get_data(as_text=True)


def test_robots_and_sitemap(client):
    r = client.get("/robots.txt").get_data(as_text=True)
    assert "Sitemap: http://localhost/sitemap.xml" in r and "Disallow: /api/" in r
    s = client.get("/sitemap.xml").get_data(as_text=True)
    assert "<loc>http://localhost/</loc>" in s and "/response" not in s


def test_robots_and_sitemap_stay_open_when_locked(app, client):
    _age_trial(app, 41)
    assert client.get("/robots.txt").status_code == 200
    assert client.get("/sitemap.xml").status_code == 200
