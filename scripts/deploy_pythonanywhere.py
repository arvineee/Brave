"""Upload the git-tracked files to PythonAnywhere through its API, reload the web app and
smoke-test it. Works on every PythonAnywhere plan (no SSH needed).

    python scripts/deploy_pythonanywhere.py --dry-run     list what would be uploaded

Env: PA_USERNAME, PA_TOKEN, PA_DOMAIN, PA_APP_DIR, optional PA_HOST, SITE_URL.
The database, LICENSE_SECRET and SITE_URL on the server are never touched: only tracked files
are sent, and the secret lives in the server's WSGI file.
"""
import os
import re
import subprocess
import sys
import time

SKIP_PREFIXES = (".github/", "scripts/", "tests/")
SKIP_FILES = {"test_app.py", ".gitignore"}


def tracked_files():
    out = subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True).stdout
    return [f for f in out.splitlines()
            if f not in SKIP_FILES and not f.startswith(SKIP_PREFIXES)]


def requirements_changed():
    r = subprocess.run(["git", "diff", "--name-only", "HEAD~1", "HEAD", "--", "requirements.txt"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing {name}")
    return v


def main():
    files = tracked_files()
    if "--dry-run" in sys.argv:
        print("\n".join(files))
        print(f"{len(files)} files")
        return

    import requests

    user, token = need("PA_USERNAME"), need("PA_TOKEN")
    domain, app_dir = need("PA_DOMAIN"), need("PA_APP_DIR").strip("/")
    host = os.environ.get("PA_HOST", "").strip() or "www.pythonanywhere.com"
    api = f"https://{host}/api/v0/user/{user}"
    headers = {"Authorization": f"Token {token}"}

    def wait_time(r, attempt):
        """Seconds to wait before retrying: honour PythonAnywhere's own hint on 429."""
        if r is not None and r.status_code == 429:
            hint = r.headers.get("Retry-After", "")
            if not hint.isdigit():
                m = re.search(r"available in (\d+) second", r.text)
                hint = m.group(1) if m else ""
            if hint:
                return min(int(hint) + 2, 120)
            return 30
        return min(2 ** (attempt + 1), 30)

    def call(method, url, make_kw=lambda: {}, attempts=8):
        r = None
        for attempt in range(attempts):
            try:
                r = requests.request(method, url, headers=headers, timeout=60, **make_kw())
            except requests.RequestException as e:
                r = None
                print(f"  {method} {url} error: {e}")
            if r is not None and r.status_code < 500 and r.status_code != 429:
                break
            if attempt < attempts - 1:
                wait = wait_time(r, attempt)
                print(f"  retrying in {wait}s (attempt {attempt + 1}/{attempts}, "
                      f"{r.status_code if r is not None else 'no response'})", flush=True)
                time.sleep(wait)
        if r is None or not r.ok:
            sys.exit(f"{method} {url} failed: "
                     f"{r.status_code if r is not None else 'no response'} "
                     f"{r.text[:200] if r is not None else ''}")
        return r

    for f in files:
        with open(f, "rb") as fh:
            data = fh.read()  # bytes, so every retry re-sends the full file
        call("POST", f"{api}/files/path/home/{user}/{app_dir}/{f}",
             make_kw=lambda data=data, f=f: {"files": {"content": (os.path.basename(f), data)}})
        print("uploaded", f, flush=True)
        time.sleep(0.5)  # gentle pacing; the files API is rate limited

    call("POST", f"{api}/webapps/{domain}/reload/")
    print("reloaded", domain)

    if requirements_changed():
        print("::warning::requirements.txt changed. Run `pip install -r requirements.txt` "
              "in a PythonAnywhere console, then reload again.")

    # Smoke test: robots.txt stays open even when the trial lock is active.
    url = os.environ.get("SITE_URL", f"https://{domain}").rstrip("/") + "/robots.txt"
    for _ in range(6):
        time.sleep(5)
        try:
            if requests.get(url, timeout=20).status_code == 200:
                print("smoke test OK:", url)
                return
        except requests.RequestException:
            pass
    sys.exit(f"Smoke test failed: {url} did not return 200 after reload")


if __name__ == "__main__":
    main()
