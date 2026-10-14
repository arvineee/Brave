"""Pure tests for stats.py, siteconfig.py and charts.py (no database needed)."""
from datetime import date

import charts
import content
import siteconfig
import stats

WED = date(2026, 10, 14)  # a Wednesday; its week started Monday 12 Oct


def rows(*items):
    return [(d, h, s) for d, h, s in items]


def test_week_to_date_is_compared_with_same_days_last_week():
    data = rows(("2026-10-12", 9, 3), ("2026-10-14", 9, 3),            # this week (Mon, Wed)
                ("2026-10-05", 9, 3), ("2026-10-07", 9, 3),            # last Mon, Wed: counted
                ("2026-10-09", 9, 3))                                  # last Fri: NOT comparable yet
    h = stats.build(data, WED)["headline"]["week"]
    assert (h["cur"], h["prev"], h["dir"]) == (2, 2, "same")


def test_month_to_date_stops_at_same_day_last_month():
    data = rows(("2026-10-02", 9, 2), ("2026-09-02", 9, 2), ("2026-09-20", 9, 2))
    h = stats.build(data, WED)["headline"]["month"]
    assert (h["cur"], h["prev"]) == (1, 1)  # 20 Sept is after the 14th, so not compared yet


def test_month_to_date_when_last_month_is_shorter():
    assert stats.previous_window("month", date(2026, 3, 31))[1] == date(2026, 2, 28)


def test_level_breakdown_and_unknown_range_falls_back_to_month():
    data = rows(("2026-10-10", 8, 1), ("2026-10-11", 8, 5), ("2026-10-12", 8, 5), ("2026-09-01", 8, 3))
    b = stats.build(data, WED, "nonsense")["breakdown"]
    assert b["range"] == "month" and b["counts"] == [1, 0, 0, 0, 2]
    assert stats.build(data, WED, "all")["breakdown"]["counts"] == [1, 0, 1, 0, 2]


def test_crisis_counts_and_direction():
    data = rows(("2026-10-13", 8, 5), ("2026-10-14", 8, 5), ("2026-10-06", 8, 5))
    c = stats.build(data, WED)["crisis"]
    assert c["week"]["cur"] == 2 and c["week"]["prev"] == 1 and c["week"]["dir"] == "up"
    assert c["all"] == 3 and len(c["weekly"]) == 8 and c["weekly"][-1] == 2


def test_time_patterns_ignore_rows_without_an_hour():
    data = rows(("2026-10-12", 2, 4), ("2026-10-13", 23, 4), ("2026-10-14", None, 2), ("2026-10-14", 14, 1))
    p = stats.build(data, WED)["patterns"]
    assert p["timed"] == 3 and p["untimed"] == 1 and p["night"] == 2
    assert p["by_hour"][2] == 1 and p["by_hour"][23] == 1
    assert p["by_weekday"][0] == 1 and p["by_weekday"][2] == 2   # Mon, Wed
    assert p["mean_by_weekday"][2] == 1.5 and p["mean_by_weekday"][1] == 4.0 and p["mean_by_weekday"][4] is None


def test_malformed_rows_are_skipped():
    data = [("not a date", 1, 3), ("2026-10-14", 99, 3), ("2026-10-14", 5, 9), ("2026-10-14", 5, 3)]
    r = stats.build(data, WED)
    assert r["headline"]["all"] == 2  # score 9 dropped; bad hour kept as untimed
    assert r["patterns"]["timed"] == 1


def test_no_data_does_not_crash():
    r = stats.build([], WED)
    assert r["headline"]["all"] == 0 and r["breakdown"]["total"] == 0
    assert r["patterns"]["peak_hour"] is None and r["patterns"]["night_share"] is None


def test_insights_stay_quiet_on_tiny_numbers():
    r = stats.build(rows(("2026-10-14", 2, 5)), WED)
    assert not any("%" in line and "arrived" in line for line in r["insights"])
    assert any("small" in line.lower() for line in r["insights"])


def test_insights_report_a_shift_when_there_is_enough_data():
    this = [("2026-10-%02d" % d, 10, 4) for d in range(1, 13)]
    last = [("2026-09-%02d" % d, 10, 2) for d in range(1, 13)]
    text = " ".join(stats.build(this + last, WED)["insights"])
    assert "More check-ins" in text and "up 100 points" in text


def test_charts_have_no_inline_styles_and_are_accessible():
    r = stats.build(rows(("2026-10-14", 2, 4)), WED)
    for svg in (charts.level_bars([1, 0, 0, 0, 0], 1),
                charts.columns(r["patterns"]["by_hour"], [str(h) for h in range(24)], label="Hours"),
                charts.stacked(r["trend"]["weekly"], label="Weekly")):
        assert "style=" not in svg and 'role="img"' in svg and "aria-label=" in svg


# ---------------------------------------------------------------- siteconfig ----
def test_defaults_when_nothing_stored():
    cfg = siteconfig.build({}, [])
    assert cfg["prompts"] == content.PROMPTS and cfg["crisis"] == content.CRISIS
    assert cfg["form"] == content.FORM and cfg["socials"] == [] and cfg["custom"] == []


def test_garbage_settings_fall_back_to_defaults():
    cfg = siteconfig.build({"crisis": "[[[", "prompts": "7", "labels": '{"energy": 5}',
                            "socials": '"x"', "contacts": "null"}, [{"bad": "row"}])
    assert cfg["crisis"]["phone_display"] == "0722 178 177" and cfg["custom"] == []


def test_invalid_crisis_pieces_never_blank_the_helpline():
    cfg = siteconfig.build({"crisis": '{"name": "", "phone_display": "  ", "phone_tel": "tel:evil", "counsellor_prompt": 3}'})
    assert cfg["crisis"]["name"] == "Befrienders Kenya" and cfg["crisis"]["phone_tel"] == "+254722178177"


def test_reworded_labels_keep_their_points_and_old_labels_stay_valid():
    new = ["Very high", "Fine", "Okay but tired", "Low", "Wiped out"]
    cfg = siteconfig.build({"labels": '{"energy": %s}' % __import__("json").dumps(new)})
    assert [p for _, p in cfg["form"]["energy"]] == [0, 0, 1, 2, 3]
    assert cfg["form"]["energy"][4][0] == "Wiped out"
    assert {"Exhausted.", "Wiped out"} <= cfg["allowed"]["energy"]


def test_locked_and_wrong_length_label_groups_are_ignored():
    cfg = siteconfig.build({"labels": '{"support_now": ["a","b","c"], "sleep": ["only one"]}'})
    assert cfg["form"]["support_now"] == content.FORM["support_now"] and cfg["form"]["sleep"] == content.FORM["sleep"]


def test_phone_normalising():
    assert siteconfig.normalise_phone("0722 178 177") == ("0722 178 177", "+254722178177")
    assert siteconfig.normalise_phone("+254 722 178 177")[1] == "+254722178177"
    assert siteconfig.normalise_phone("116")[1] == "116"
    assert siteconfig.normalise_phone("call me") is None and siteconfig.normalise_phone("") is None


def test_social_urls_must_be_https_with_a_real_host():
    ok = siteconfig.valid_url
    assert ok("https://instagram.com/brave")
    for bad in ("http://x.com", "javascript:alert(1)", "https://", "https://user@evil.com", "https://a b.com", "https://localhost", None):
        assert not ok(bad), bad


def test_custom_answers_are_validated_against_the_active_questions():
    qs = [{"id": 1, "kind": "choice", "options": ["Yes", "No"]},
          {"id": 2, "kind": "multi", "options": ["a", "b"]},
          {"id": 3, "kind": "scale", "options": ("", "")}]
    assert siteconfig.clean_custom_answers(None, qs) == []
    assert siteconfig.clean_custom_answers({"1": "Yes", "2": ["a", "b"], "3": 0, "9": "ignored"}, qs) == \
        [(1, "Yes"), (2, "a"), (2, "b"), (3, "0")]
    for bad in ({"1": "Maybe"}, {"2": []}, {"2": ["a", "a"]}, {"2": "a"}, {"3": 11}, {"3": True}, {"3": "5"}, [], "x"):
        assert siteconfig.clean_custom_answers(bad, qs) is None, bad


class _Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]


def test_custom_question_form_validation():
    ok, errs = siteconfig.clean_custom_question(_Form(prompt="Feel safe?", kind="choice", options="Yes\nNo\n\n"))
    assert not errs and ok["options"] == '["Yes", "No"]' and ok["required"] == 0
    for bad in (dict(prompt="x", kind="choice", options="Yes\nNo"), dict(prompt="Okay?", kind="text", options=""),
                dict(prompt="Okay?", kind="choice", options="Only one"), dict(prompt="Okay?", kind="multi", options="a\na")):
        assert siteconfig.clean_custom_question(_Form(bad))[1], bad


def test_label_form_validation_blocks_duplicates_and_overlong():
    f = _Form({"label_" + g: [lbl for lbl, _ in content.FORM[g]] for g in content.EDITABLE_LABELS})
    assert siteconfig.clean_labels(f)[1] == []
    f["label_channel"] = ["SMS", "SMS", "Instagram.", "Email."]
    assert siteconfig.clean_labels(f)[1]
    f["label_channel"] = ["SMS", "WhatsApp.", "Instagram.", "x" * 21]
    assert siteconfig.clean_labels(f)[1]
