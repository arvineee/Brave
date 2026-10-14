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
* `checkins`: anonymous. Date, score, multiple-choice answers. No name, contact, IP,
  user agent or free text.
* `support_requests`: only if the student typed a contact AND ticked consent. Contact,
  support type, channel, date. Not linked to any check-in.
* The two reflection boxes (questions 5 and 6) are only used on the phone to catch
  self-harm words; they are never sent or stored.

Staff reports (run on the server):
    python admin.py stats
    python admin.py requests
    python admin.py purge --days 30     # delete old support requests

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
