"""Sign in to Nautobot once, for the Nautobot screenshots: asks for the username and password in the terminal, signs in
with a headless browser, and keeps the session in ~/.cache/c8d/nautobot-state.json (outside the repo). capture.py's
`nautobot` part reads it.

    webapp/.venv/bin/python docs/presentation/nb_session.py
"""
import getpass
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PUB = os.environ.get("LAB_PUBLIC", "192.168.50.231")
NAUTOBOT = f"http://{PUB}:8080"
STATE = Path(os.environ.get("NB_STATE", Path.home() / ".cache/c8d/nautobot-state.json"))

user = input("Nautobot username: ").strip()
pw = getpass.getpass("Nautobot password: ")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    ctx = b.new_context()
    page = ctx.new_page()
    page.goto(f"{NAUTOBOT}/login/?next=/")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", pw)
    page.locator("form button[type=submit], form input[type=submit]").first.click()
    page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        sys.exit("sign-in failed: check the username and password")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(STATE))
    STATE.chmod(0o600)
    b.close()
print(f"signed in; session kept in {STATE}")
