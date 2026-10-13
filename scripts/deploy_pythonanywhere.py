"""Upload the git-tracked files to PythonAnywhere through its API, reload the web app and
smoke-test it. Works on every PythonAnywhere plan (no SSH needed).

    python scripts/deploy_pythonanywhere.py --dry-run     list what would be uploaded

Env: PA_USERNAME, PA_TOKEN, PA_DOMAIN, PA_APP_DIR, optional PA_HOST, SITE_URL.
The database, LICENSE_SECRET and SITE_URL on the server are never touched: only tracked files
are sent, and the secret lives in the server's WSGI file.
"""
import os
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

    def call(method, url, **kw):
        for attempt in range(3):
            r = requests.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code < 500 and r.status_code != 429:
                break
            time.sleep(2 * (attempt + 1))
        if not r.ok:
            sys.exit(f"{method} {url} failed: {r.status_code} {r.text[:200]}")
        return r

    for f in files:
        with open(f, "rb") as fh:
            call("POST", f"{api}/files/path/home/{user}/{app_dir}/{f}", files={"content": fh})
        print("uploaded", f)

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
