"""Tiny server-side SVG charts for the admin dashboard.

No JavaScript, no external libraries, and no `style=` attributes (the site's CSP forbids
inline styles). Colours come from CSS classes in static/admin.css (.b1 to .b5, .bar).
"""
from markupsafe import Markup, escape

from stats import LEVELS

W = 320


def _svg(height, label, body):
    return Markup(f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" '
                  f'aria-label="{escape(label)}" focusable="false">{body}</svg>')


def level_bars(counts, total, label="Check-ins by level"):
    """Horizontal bars, one row per level 1..5, with count and percentage."""
    top = max(counts) or 1
    rows = []
    for i, c in enumerate(counts):
        y = i * 30
        w = round(120 * c / top)
        pct = "" if not total else f" ({round(c * 100 / total)}%)"
        rows.append(
            f'<text class="axis" x="0" y="{y + 17}">{escape(LEVELS[i + 1])}</text>'
            f'<rect class="b{i + 1}" x="104" y="{y + 4}" width="{max(w, 2 if c else 0)}" '
            f'height="18" rx="3"/>'
            f'<text class="val" x="{104 + max(w, 2 if c else 0) + 6}" y="{y + 17}">{c}{pct}</text>')
    summary = "; ".join(f"{LEVELS[i + 1]} {c}" for i, c in enumerate(counts))
    return _svg(150, f"{label}: {summary}", "".join(rows))


def columns(values, labels, *, label, every=1, cls="bar", marks=()):
    """Vertical bars. `marks` = indexes drawn in the alert colour (e.g. night hours)."""
    n = len(values)
    top = max(values) or 1
    left, base, height = 22, 110, 134
    step = (W - left) / n
    bw = max(step * 0.68, 2)
    body = [f'<line class="grid" x1="{left}" y1="{base}" x2="{W}" y2="{base}"/>',
            f'<text class="axis" x="0" y="14">{top}</text>',
            f'<text class="axis" x="0" y="{base}">0</text>']
    for i, v in enumerate(values):
        h = round((base - 18) * v / top)
        x = left + i * step + (step - bw) / 2
        c = cls + (" mark" if i in marks else "")
        body.append(f'<rect class="{c}" x="{x:.1f}" y="{base - h}" width="{bw:.1f}" '
                    f'height="{h}" rx="2"/>')
        if n <= 8 and v:
            body.append(f'<text class="val mid" x="{x + bw / 2:.1f}" y="{base - h - 4}">{v}</text>')
        if i % every == 0:
            body.append(f'<text class="axis mid" x="{x + bw / 2:.1f}" y="{height - 4}">'
                        f'{escape(labels[i])}</text>')
    summary = ", ".join(f"{labels[i]} {v}" for i, v in enumerate(values))
    return _svg(height, f"{label}: {summary}", "".join(body))


def stacked(series, *, label):
    """series: [{'label', 'counts': [5 ints]}] drawn as stacked columns, level 1 at the bottom."""
    n = len(series)
    top = max((sum(s["counts"]) for s in series), default=0) or 1
    left, base, height = 22, 110, 134
    step = (W - left) / n
    bw = step * 0.68
    body = [f'<line class="grid" x1="{left}" y1="{base}" x2="{W}" y2="{base}"/>',
            f'<text class="axis" x="0" y="14">{top}</text>',
            f'<text class="axis" x="0" y="{base}">0</text>']
    for i, s in enumerate(series):
        x = left + i * step + (step - bw) / 2
        y = base
        for lvl, c in enumerate(s["counts"], start=1):
            if not c:
                continue
            h = (base - 18) * c / top
            y -= h
            body.append(f'<rect class="b{lvl}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                        f'height="{h:.1f}"/>')
        body.append(f'<text class="axis mid" x="{x + bw / 2:.1f}" y="{height - 4}">'
                    f'{escape(s["label"].split()[0])}</text>')
    summary = "; ".join(f"week of {s['label']}: {sum(s['counts'])}" for s in series)
    return _svg(height, f"{label}: {summary}", "".join(body))
