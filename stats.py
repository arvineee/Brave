"""Dashboard statistics. Pure functions over (day, hour, score) tuples: no Flask, no database.

All dates are Kenya time (the app stores `day` and `hour` in EAT). Periods are calendar
weeks (Monday to Sunday) and calendar months. A period still in progress is compared with
the SAME elapsed days of the previous period, so a Wednesday is never judged against a full
last week.

Everything counts SUBMISSIONS, not students: check-ins are anonymous, so one student who
checks in three times is three submissions.
"""
from datetime import date, timedelta

LEVELS = {1: "Thriving", 2: "Holding steady", 3: "Noticing strain", 4: "Struggling", 5: "In crisis"}
RANGES = {"week": "This week", "month": "This month", "last_month": "Last month", "all": "All time"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SMALL_N = 10                   # below this, percentages are too noisy to read much into
NIGHT_HOURS = (23, 0, 1, 2, 3, 4)   # 11 pm to 4:59 am


# ---------------------------------------------------------------- helpers ----
def parse_rows(raw):
    """[(day_str, hour, score)] -> [(date, hour|None, score)], skipping malformed rows."""
    rows = []
    for day, hour, score in raw:
        try:
            d = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if not isinstance(score, int) or not 1 <= score <= 5:
            continue
        h = hour if isinstance(hour, int) and 0 <= hour <= 23 else None
        rows.append((d, h, score))
    return rows


def week_start(d):
    return d - timedelta(days=d.weekday())


def month_start(d):
    return d.replace(day=1)


def add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def window(key, today):
    """Inclusive (start, end); (None, None) means no limit."""
    if key == "week":
        return week_start(today), today
    if key == "month":
        return month_start(today), today
    if key == "last_month":
        return add_months(month_start(today), -1), month_start(today) - timedelta(days=1)
    return None, None


def previous_window(key, today):
    """The matching stretch of the previous period (same elapsed days), or None."""
    if key == "week":
        s = week_start(today) - timedelta(days=7)
        return s, s + (today - week_start(today))
    if key == "month":
        s = add_months(month_start(today), -1)
        last = month_start(today) - timedelta(days=1)
        return s, min(s + timedelta(days=today.day - 1), last)
    return None


def counts_in(rows, start, end):
    c = [0] * 5
    for d, _h, s in rows:
        if (start is None or d >= start) and (end is None or d <= end):
            c[s - 1] += 1
    return c


def share(count, total):
    return None if not total else round(count * 100 / total, 1)


def compare(cur, prev):
    diff = cur - prev
    return {"cur": cur, "prev": prev, "diff": diff,
            "dir": "up" if diff > 0 else "down" if diff < 0 else "same"}


def _peak(values):
    top = max(values) if values else 0
    return values.index(top) if top > 0 else None


def hour_label(h):
    return f"{h:02d}:00"


# ------------------------------------------------------------------ build ----
def build(raw_rows, today, range_key="month"):
    if range_key not in RANGES:
        range_key = "month"
    rows = parse_rows(raw_rows)

    def total(key):
        return sum(counts_in(rows, *window(key, today)))

    # headline numbers: period to date versus the same stretch of the previous period
    pw, pm = previous_window("week", today), previous_window("month", today)
    week_c, month_c = counts_in(rows, *window("week", today)), counts_in(rows, *window("month", today))
    prev_week_c, prev_month_c = counts_in(rows, *pw), counts_in(rows, *pm)
    headline = {
        "week": compare(sum(week_c), sum(prev_week_c)),
        "month": compare(sum(month_c), sum(prev_month_c)),
        "all": len(rows),
        "last_month_full": total("last_month"),
    }

    # score breakdown for the selected range
    start, end = window(range_key, today)
    counts = counts_in(rows, start, end)
    n = sum(counts)
    breakdown = {"range": range_key, "label": RANGES[range_key], "counts": counts, "total": n,
                 "pct": [share(c, n) for c in counts], "small": 0 < n < SMALL_N}

    # trend: month to date vs the same stretch of last month, as a share of submissions
    cur45 = share(month_c[3] + month_c[4], sum(month_c))
    prev45 = share(prev_month_c[3] + prev_month_c[4], sum(prev_month_c))
    trend = {
        "this_counts": month_c, "last_counts": prev_month_c,
        "this_total": sum(month_c), "last_total": sum(prev_month_c),
        "this_pct": [share(c, sum(month_c)) for c in month_c],
        "last_pct": [share(c, sum(prev_month_c)) for c in prev_month_c],
        "share45_this": cur45, "share45_last": prev45,
        "share45_delta": None if cur45 is None or prev45 is None else round(cur45 - prev45, 1),
        "small": sum(month_c) < SMALL_N or sum(prev_month_c) < SMALL_N,
        "weekly": [],
    }
    this_ws = week_start(today)
    for i in range(7, -1, -1):
        ws = this_ws - timedelta(days=7 * i)
        c = counts_in(rows, ws, min(ws + timedelta(days=6), today))
        trend["weekly"].append({"start": ws, "label": f"{ws.day} {ws.strftime('%b')}",
                                "counts": c, "total": sum(c)})

    # crisis flags (score 5)
    crisis = {
        "week": compare(week_c[4], prev_week_c[4]),
        "month": compare(month_c[4], prev_month_c[4]),
        "all": sum(1 for r in rows if r[2] == 5),
        "weekly": [w["counts"][4] for w in trend["weekly"]],
        "weekly_labels": [w["label"] for w in trend["weekly"]],
    }

    # when do check-ins happen? (selected range)
    in_range = [r for r in rows
                if (start is None or r[0] >= start) and (end is None or r[0] <= end)]
    by_hour = [0] * 24
    timed = 0
    for _d, h, _s in in_range:
        if h is not None:
            by_hour[h] += 1
            timed += 1
    by_day, score_sum = [0] * 7, [0] * 7
    for d, _h, s in in_range:
        by_day[d.weekday()] += 1
        score_sum[d.weekday()] += s
    night = sum(by_hour[h] for h in NIGHT_HOURS)
    ph, pd = _peak(by_hour), _peak(by_day)
    patterns = {
        "by_hour": by_hour, "timed": timed, "untimed": len(in_range) - timed,
        "night": night, "night_share": share(night, timed),
        "peak_hour": None if ph is None else hour_label(ph),
        "by_weekday": by_day,
        "mean_by_weekday": [round(score_sum[i] / by_day[i], 2) if by_day[i] else None
                            for i in range(7)],
        "peak_weekday": None if pd is None else WEEKDAYS[pd],
        "small": 0 < len(in_range) < SMALL_N,
    }

    return {"today": today, "headline": headline, "breakdown": breakdown, "trend": trend,
            "crisis": crisis, "patterns": patterns,
            "insights": insights(headline, breakdown, trend, crisis, patterns)}


# --------------------------------------------------------------- insights ----
def _pp(x):
    return f"{abs(x):g} point{'s' if abs(x) != 1 else ''}"


def insights(headline, breakdown, trend, crisis, patterns):
    """A few plain-language sentences. Cautious by design: no claims on tiny numbers."""
    out = []
    d = trend["share45_delta"]
    if d is not None and not trend["small"]:
        if d == 0:
            out.append("The share of students who are struggling or in crisis is the same as "
                       "at this point last month.")
        else:
            out.append(
                f"{'More' if d > 0 else 'Fewer'} check-ins are in the struggling or in-crisis "
                f"range this month: {trend['share45_this']:g}% against {trend['share45_last']:g}% "
                f"at this point last month ({'up' if d > 0 else 'down'} {_pp(d)}).")
    c = crisis["month"]
    if c["cur"] or c["prev"]:
        word = {"up": "up from", "down": "down from", "same": "the same as"}[c["dir"]]
        out.append(f"In-crisis check-ins this month: {c['cur']}, {word} {c['prev']} "
                   f"at this point last month.")
    if patterns["night_share"] is not None and patterns["timed"] >= SMALL_N \
            and patterns["night_share"] >= 15:
        out.append(f"{patterns['night_share']:g}% of timed check-ins in this range arrived "
                   f"between 11 pm and 5 am.")
    means = [(m, WEEKDAYS[i]) for i, m in enumerate(patterns["mean_by_weekday"])
             if m is not None and patterns["by_weekday"][i] >= 3]
    if len(means) >= 4 and not patterns["small"]:
        hi, lo = max(means), min(means)
        if hi[0] - lo[0] >= 0.3:
            out.append(f"Average level is highest on {hi[1]} ({hi[0]:g}) and lowest on "
                       f"{lo[1]} ({lo[0]:g}).")
    if breakdown["small"] or trend["small"]:
        out.append("Numbers are still small, so read the percentages as a rough guide only.")
    return out
