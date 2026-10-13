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
