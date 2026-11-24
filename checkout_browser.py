"""Dedicated local Chrome; disconnecting the script leaves its window open."""
import json
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / ".private" / "checkout-chrome"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

def selected_profile():
    config = ROOT / '.private' / 'direct-browser-profile.json'
    if not config.exists():
        return PROFILE
    name = json.loads(config.read_text())['profile']
    if name not in ('checkout-chrome', 'checkout-chrome-clean', 'checkout-chrome-manual-login'):
        raise ValueError('Unapproved browser profile')
    return ROOT / '.private' / name


def endpoint():
    try:
        port = int((selected_profile() / "DevToolsActivePort").read_text().splitlines()[0])
        url = f"http://127.0.0.1:{port}"
        with urlopen(url + "/json/version", timeout=1) as response:
            data = json.load(response)
        if not data.get("webSocketDebuggerUrl", "").startswith(f"ws://127.0.0.1:{port}/"):
            return None
        return url
    except (OSError, ValueError, KeyError):
        return None


def launch():
    profile = selected_profile()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.parent.chmod(0o700)
    profile.chmod(0o700)
    current = endpoint()
    if current:
        return current
    from task_browser import stop_if_manual
    stop_if_manual()
    if not CHROME.exists():
        raise RuntimeError("Google Chrome is not installed at the configured path")
    # A separate profile: never attach to the user's ordinary Chrome session.
    with (profile.parent / "chrome-launch.log").open("ab") as log:
        subprocess.Popen([str(CHROME), f"--user-data-dir={profile}",
                          "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                          "--no-first-run", "--no-default-browser-check", "about:blank"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = endpoint()
        if current:
            return current
        time.sleep(0.25)
    raise RuntimeError("Dedicated Chrome did not start; inspect .private/chrome-launch.log")


def connect(playwright):
    browser = playwright.chromium.connect_over_cdp(launch(), no_defaults=True)
    context = browser.contexts[0]
    return browser, context
