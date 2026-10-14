# Mapenzi ya 30: #IAMBRAVE wellness check-in (Flask)

Pages: `/` home, `/checkin` the 10-question form, `/response?score=1..5` personalised
response (missing/invalid score shows 3). Crisis resources (Befrienders Kenya
0722 178 177, campus counsellor prompt) are in the page on every route.
Mobile-first, designed from 360px; no cookies, no tracking, no external requests.

## Run locally
    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000  (SQLite file in instance/)

## Production (PostgreSQL)
    export DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
    gunicorn app:app
Tables are created automatically on first start (`postgres://` URLs are accepted too).

## Data (what is stored)
* `checkins`: anonymous. Date, Kenya-time hour of day (0-23), score, multiple-choice answers.
  No name, contact, IP, user agent, exact time or free text.
* `custom_answers`: answers to extra questions staff added in the dashboard (multiple choice
  only, tied to the anonymous check-in, never to a person).
* `support_requests`: only if the student typed a contact AND ticked consent. Contact,
  support type, channel, date, and a "contacted" flag set by staff. Not linked to any check-in.
* `site_settings`, `custom_questions`: staff-edited wording, helpline, contacts, social links
  and extra questions (no student data).
* The two reflection boxes (questions 5 and 6) are only used on the phone to catch
  self-harm words; they are never sent or stored.

Staff dashboard: see "Admin dashboard" below. Command-line reports (run on the server):
    python admin.py stats
    python admin.py requests
    python admin.py purge --days 30     # delete old support requests

## Admin dashboard (`/admin`)
Switched off until both variables are set in the server environment (PythonAnywhere: the WSGI
file). Without them every `/admin` URL returns 404.

    python admin.py hashpw      # asks for a password, prints ADMIN_PASSWORD_HASH='...'
    python -c "import secrets; print(secrets.token_hex(32))"   # use as ADMIN_SECRET_KEY

Set `ADMIN_PASSWORD_HASH` and `ADMIN_SECRET_KEY`, reload, then open `/admin`. For local
development over plain http also set `ADMIN_COOKIE_SECURE=0`.

What staff can do
* **Overview**: submissions this week/month against the same days of the previous period,
  breakdown by level, trend (month so far vs the same days last month, last 8 weeks), crisis
  (level 5) counts and trend, time-of-day and day-of-week patterns, results of extra questions.
  Counts are submissions, not students: check-ins are anonymous.
* **Responses**: anonymous check-ins with day, part of day, level and answers; CSV download.
* **Support requests**: contacts students agreed to share; mark contacted, delete.
* **Questions**: reword the ten questions and most answer options (scores are fixed, so the
  clinical rubric stays as signed off), and add, edit, hide, reorder or delete extra
  *unscored* questions (pick one, pick any, or a 0-10 scale).
* **Contacts & socials**: crisis helpline (shown on every page), Brave email and phone, social
  links in the footer.

Security: login cookie limited to `/admin` (HttpOnly, SameSite=Strict, Secure on https), 8-hour
sessions, CSRF token on every change, 10 wrong passwords in 10 minutes locks login for a while,
admin pages are `noindex` and never cached. Public pages still set no cookies.

Not available on purpose: what students type in the two reflection boxes. It is scored on the
phone and never sent, which is what the home page promises students. Showing it would need an
explicit opt-in, new wording on the site and a data-protection and clinical review first.

Upgrading an existing site: nothing to run. On first start the app adds the `hour` and
`handled` columns and creates the new tables. Check-ins from before the upgrade have no time
recorded and are left out of the time-of-day chart.

If the settings table is ever unreachable the public pages fall back to the wording and the
helpline in `content.py`, so crisis resources always render.

## Scoring (draft, needs Brave's clinical sign-off)
Done in the browser (`static/scoring.js`); option points live in `content.py`.
energy 0-3, sleep 0-3, support wanted 0-3, stressors 0-2 (capped), connection 0-4.
Total 0-2 => 1, 3-5 => 2, 6-8 => 3, 9-11 => 4, 12+ => 5. Self-harm words in the
reflection boxes force 5.

## Branding / copy
Logo: `static/brave-logo.svg`, `static/brave-mark.svg` and `templates/_logo.html`
(a placeholder mark: replace with Brave's official artwork when available).
All text is in `content.py`.

## Test
    pip install pytest && pytest

## 40-day trial and licence key
* Set `LICENSE_SECRET` in the server environment (PythonAnywhere: WSGI file or `.env`). Never
  commit it. The trial clock starts the first time the app runs with the secret set, so set it
  when you hand the site over. Without the secret the app is locked (fails closed).
* After `TRIAL_DAYS` (default 40) every page except `/locked`, static files and the
  score-5 crisis response redirects to `/locked`. Crisis resources are always on that page.
* If the site already has check-ins when you add this, run `python admin.py starttrial` once
  (a missing row with existing check-ins is otherwise treated as tampering).
* `python admin.py license` shows days left. `python admin.py genkey` prints the key for this
  install: give it to the client when they pay, they type it on `/locked`.
* The `license` row is HMAC-signed with the secret and protected by DB triggers (no delete,
  no changing the start date, no un-activating). Hand-editing it locks the site.
* Limits: this deters casual tampering; anyone who can edit the code or the server environment
  can still remove the gate, and a DB superuser can drop the triggers (the signature still holds).

## SEO
* Indexable: `/` and `/checkin` (unique title, description, canonical, Open Graph, Twitter card;
  JSON-LD Organization + WebSite on the home page). Private pages (`/response`, `/locked`,
  `/unlock`, `/api/*`) send `noindex` via meta tag and `X-Robots-Tag`.
* `/robots.txt` and `/sitemap.xml` are generated, and stay reachable even when the trial has locked.
* Set `SITE_URL` (e.g. `https://yourname.pythonanywhere.com`) in the environment so canonical
  links and the sitemap always use the real domain.
* After going live: add the site in Google Search Console, submit `/sitemap.xml`, and check the
  home page in the Rich Results Test and a social share preview.

## CI/CD (GitHub Actions to PythonAnywhere)
`.github/workflows/ci-cd.yml`: every push and pull request runs the syntax check and `pytest`.
A push to `main`/`master` that passes then uploads the tracked files through the PythonAnywhere
API, reloads the web app and checks `/robots.txt` returns 200.

One-time setup
1. PythonAnywhere: **Account, API token**, create a token.
2. GitHub repo, **Settings, Secrets and variables, Actions**:
   * Secrets: `PA_USERNAME`, `PA_TOKEN`, `PA_DOMAIN` (e.g. `yourname.pythonanywhere.com`)
   * Variables: `PA_APP_DIR` (folder under `/home/<user>/`), optional `PA_HOST` (`eu.pythonanywhere.com` if your account is on the EU server)
3. Optional: **Settings, Environments, production**, add a required reviewer for manual approval.

Notes
* `LICENSE_SECRET`, `SITE_URL` and the database stay on the server; the pipeline never sends them.
* Deleted files are not removed on the server. New dependencies need `pip install -r requirements.txt` in a PythonAnywhere console (the job prints a warning when `requirements.txt` changes).
* Test locally without uploading: `python scripts/deploy_pythonanywhere.py --dry-run`.
