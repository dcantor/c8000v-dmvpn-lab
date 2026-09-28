"""Images of the value stream map for the README: the map and its summary, for a C8000v and a VyOS customer.

    webapp/.venv/bin/python docs/value-stream/capture.py
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

D = Path(__file__).resolve().parent

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    p = b.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=2, color_scheme="light")
    p.goto((D / "add-customer.html").as_uri()); p.wait_for_selector("#vsm rect"); time.sleep(2)
    for plat in ("c8000v", "vyos"):
        p.click(f"#p-{plat}"); time.sleep(0.8)
        p.locator("#vsm").screenshot(path=str(D / f"map-{plat}.png"))
        p.locator(".kpis").screenshot(path=str(D / f"summary-{plat}.png"))
        print(plat)
    b.close()
