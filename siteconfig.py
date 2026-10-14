"""Staff-editable site content, layered over the defaults in content.py.

Pure functions only (no Flask, no database), so they are easy to test and cannot take the
site down. `build()` NEVER raises and always falls back to the defaults in content.py: a
broken or empty settings table must not remove the crisis helpline from a page.

Stored settings (table `site_settings`, key -> JSON text)
  prompts   {"q1": "wording", ...}              wording of the ten built-in questions
  labels    {"energy": ["High", ...], ...}      reworded option labels (same count, same order)
  crisis    {"name", "phone_display", "phone_tel", "counsellor_prompt", "footer_tag"}
  contacts  {"email", "phone"}                  shown in the footer
  socials   [{"platform", "label", "url"}]      shown in the footer
"""
import json
import re
from urllib.parse import urlparse

import content

CUSTOM_KINDS = {"choice": "Pick one", "multi": "Pick any that apply", "scale": "Scale 0 to 10"}
MAX_CUSTOM = 10          # active extra questions on the form
MAX_OPTIONS = 12
MAX_SOCIALS = 8

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


# ---------------------------------------------------------------- reading ----
def _json(raw, default):
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _text(v, lo, hi):
    """A trimmed single-line string within [lo, hi] characters, else None."""
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v if lo <= len(v) <= hi else None


def custom_options(kind, raw):
    """Stored options JSON -> list of labels (choice/multi) or (low, high) (scale)."""
    if kind == "scale":
        d = _json(raw, {})
        return (str(d.get("low", ""))[:40], str(d.get("high", ""))[:40])
    return [o for o in _json(raw, []) if isinstance(o, str)]


def build(settings, custom_rows=()):
    """settings: {key: json_text}. custom_rows: dicts with id, prompt, kind, options, required,
    active, position. Returns the effective configuration."""
    try:
        return _build(settings or {}, custom_rows)
    except Exception:  # a bad row must never break the public site
        return _build({}, ())


def _build(settings, custom_rows):
    prompts = dict(content.PROMPTS)
    for k, v in _json(settings.get("prompts"), {}).items():
        t = _text(v, 3, 200)
        if k in prompts and t:
            prompts[k] = t

    form = {g: list(opts) for g, opts in content.FORM.items()}
    allowed = {g: {label for label, _ in opts} for g, opts in content.FORM.items()}
    for g, labels in _json(settings.get("labels"), {}).items():
        base = content.FORM.get(g)
        cap = content.EDITABLE_LABELS.get(g)
        if not base or not cap or not isinstance(labels, list) or len(labels) != len(base):
            continue
        clean = [_text(x, 1, cap) for x in labels]
        if None in clean or len(set(clean)) != len(clean):
            continue
        form[g] = [(clean[i], base[i][1]) for i in range(len(base))]
        allowed[g] |= set(clean)  # a page opened before an edit can still submit

    crisis = dict(content.CRISIS)
    stored = _json(settings.get("crisis"), {})
    for key, lo, hi in (("name", 2, 60), ("phone_display", 3, 30),
                        ("counsellor_prompt", 5, 200), ("footer_tag", 3, 60)):
        t = _text(stored.get(key), lo, hi)
        if t:
            crisis[key] = t
    tel = stored.get("phone_tel")
    if isinstance(tel, str) and re.fullmatch(r"\+?\d{3,15}", tel):
        crisis["phone_tel"] = tel

    contacts = dict(content.CONTACTS)
    stored = _json(settings.get("contacts"), {})
    if _text(stored.get("email"), 3, 254) and _EMAIL.match(stored["email"]):
        contacts["email"] = stored["email"]
    t = _text(stored.get("phone"), 3, 30)
    if t:
        contacts["phone"] = t
    contacts["phone_tel"] = (normalise_phone(contacts["phone"]) or [None, ""])[1] \
        if contacts["phone"] else ""

    socials = []
    for s in _json(settings.get("socials"), []):
        if isinstance(s, dict) and s.get("platform") in content.SOCIAL_PLATFORMS \
                and valid_url(s.get("url")):
            socials.append({"platform": s["platform"], "url": s["url"],
                            "label": _text(s.get("label"), 1, 40) or s["platform"]})

    custom = []
    for r in sorted(custom_rows, key=lambda r: (r["position"], r["id"])):
        if not r["active"] or r["kind"] not in CUSTOM_KINDS:
            continue
        opts = custom_options(r["kind"], r["options"])
        if r["kind"] != "scale" and len(opts) < 2:
            continue
        custom.append({"id": r["id"], "prompt": r["prompt"], "kind": r["kind"],
                       "options": opts, "required": bool(r["required"])})
    custom = custom[:MAX_CUSTOM]

    return {"prompts": prompts, "form": form, "allowed": allowed, "crisis": crisis,
            "contacts": contacts, "socials": socials, "custom": custom}


# ------------------------------------------------------------- validation ----
def normalise_phone(raw):
    """'0722 178 177' -> ('0722 178 177', '+254722178177'). Short codes (116) stay as typed.
    Returns None if it is not a plausible number."""
    shown = _text(raw, 3, 30)
    if not shown:
        return None
    d = re.sub(r"[\s\-().]", "", shown)
    if d.startswith("+"):
        tel = d
    elif d.startswith("00"):
        tel = "+" + d[2:]
    elif d.startswith("0"):
        tel = "+254" + d[1:]
    elif d.startswith("254"):
        tel = "+" + d
    else:
        tel = d
    return (shown, tel) if re.fullmatch(r"\+?\d{3,15}", tel) else None


def valid_url(u):
    if not isinstance(u, str) or len(u) > 200 or re.search(r"\s", u):
        return False
    p = urlparse(u)
    return p.scheme == "https" and bool(p.hostname) and "@" not in p.netloc and "." in p.hostname


def clean_prompts(form):
    """form: mapping of 'prompt_q1'.. -> text. Returns (dict, errors)."""
    out, errors = {}, []
    for k in content.PROMPTS:
        t = _text(form.get("prompt_" + k), 3, 200)
        if t is None:
            errors.append(f"Question {k[1:]} needs wording of 3 to 200 characters.")
        else:
            out[k] = t
    return out, errors


def clean_labels(form):
    """form.getlist-like: needs .get and .getlist. Returns (dict, errors)."""
    out, errors = {}, []
    for g, cap in content.EDITABLE_LABELS.items():
        base = content.FORM[g]
        vals = [_text(v, 1, cap) for v in form.getlist("label_" + g)]
        if len(vals) != len(base):
            errors.append(f"The answer options for “{g}” were incomplete; nothing changed there.")
            continue
        if None in vals:
            errors.append(f"Each answer option needs 1 to {cap} characters ({g.replace('_', ' ')}).")
        elif len(set(vals)) != len(vals):
            errors.append(f"Answer options must all be different ({g.replace('_', ' ')}).")
        else:
            out[g] = vals
    return out, errors


def clean_crisis(form):
    errors = []
    name = _text(form.get("name"), 2, 60)
    counsellor = _text(form.get("counsellor_prompt"), 5, 200)
    tag = _text(form.get("footer_tag"), 3, 60)
    phone = normalise_phone(form.get("phone_display"))
    if not name:
        errors.append("Helpline name is required.")
    if not phone:
        errors.append("The helpline number must be a real phone number, for example 0722 178 177.")
    if not counsellor:
        errors.append("The campus counsellor message is required.")
    if not tag:
        errors.append("The footer tag is required.")
    if errors:
        return None, errors
    return {"name": name, "phone_display": phone[0], "phone_tel": phone[1],
            "counsellor_prompt": counsellor, "footer_tag": tag}, []


def clean_contacts(form):
    errors, out = [], {"email": "", "phone": ""}
    email = (form.get("email") or "").strip()
    if email:
        if _EMAIL.match(email) and len(email) <= 254:
            out["email"] = email
        else:
            errors.append("That email address does not look right.")
    phone = (form.get("phone") or "").strip()
    if phone:
        if normalise_phone(phone):
            out["phone"] = normalise_phone(phone)[0]
        else:
            errors.append("That phone number does not look right.")
    return out, errors


def clean_socials(form):
    out, errors = [], []
    platforms = form.getlist("social_platform")
    urls = form.getlist("social_url")
    labels = form.getlist("social_label")
    for i, url in enumerate(urls):
        url = url.strip()
        if not url:
            continue
        platform = platforms[i] if i < len(platforms) else ""
        if platform not in content.SOCIAL_PLATFORMS:
            errors.append("Choose a platform for every link.")
        elif not valid_url(url):
            errors.append(f"{platform}: the link must start with https:// and be a full address.")
        else:
            label = _text(labels[i] if i < len(labels) else "", 1, 40) or platform
            out.append({"platform": platform, "label": label, "url": url})
    if len(out) > MAX_SOCIALS:
        errors.append(f"At most {MAX_SOCIALS} social links.")
    return out, errors


def clean_custom_question(form):
    """Returns (dict(prompt, kind, options_json, required), errors)."""
    errors = []
    prompt = _text(form.get("prompt"), 3, 200)
    kind = form.get("kind")
    if not prompt:
        errors.append("The question needs wording of 3 to 200 characters.")
    if kind not in CUSTOM_KINDS:
        errors.append("Choose a question type.")
    options = None
    if kind == "scale":
        options = json.dumps({"low": (_text(form.get("scale_low"), 1, 40) or ""),
                              "high": (_text(form.get("scale_high"), 1, 40) or "")})
    elif kind in ("choice", "multi"):
        labels = []
        for line in (form.get("options") or "").splitlines():
            t = _text(line, 1, 80)
            if t:
                labels.append(t)
            elif line.strip():
                errors.append("Each answer option can be at most 80 characters.")
        if len(set(labels)) != len(labels):
            errors.append("Answer options must all be different.")
        if not 2 <= len(labels) <= MAX_OPTIONS:
            errors.append(f"Give between 2 and {MAX_OPTIONS} answer options, one per line.")
        options = json.dumps(labels)
    if errors:
        return None, errors
    return {"prompt": prompt, "kind": kind, "options": options,
            "required": 1 if form.get("required") else 0}, []


def clean_custom_answers(raw, questions):
    """Validate the 'custom' object of a check-in. questions = cfg['custom'].
    Returns a list of (question_id, answer_text), or None if anything present is invalid.
    Unknown or inactive question ids are ignored; a missing answer is allowed."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return None
    out = []
    for q in questions:
        if str(q["id"]) not in raw:
            continue
        v = raw[str(q["id"])]
        if q["kind"] == "choice":
            if v not in q["options"]:
                return None
            out.append((q["id"], v))
        elif q["kind"] == "multi":
            if (not isinstance(v, list) or not v or len(set(v)) != len(v)
                    or any(x not in q["options"] for x in v)):
                return None
            out.extend((q["id"], x) for x in v)
        else:  # scale
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 10:
                return None
            out.append((q["id"], str(v)))
    return out
