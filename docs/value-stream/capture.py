"""Images and tables for the value stream docs, drawn from the interactive pages:
- add-customer.html → map-<platform>.png, summary-<platform>.png (README.md)
- detailed.html     → detailed-strip-<platform>.png, detailed-summary-<platform>.png, and the task tables in DETAILED.md

    webapp/.venv/bin/python docs/value-stream/capture.py
"""
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

D = Path(__file__).resolve().parent
PLATS = {"c8000v": "Catalyst 8000v customer", "vyos": "VyOS customer"}


def tables(p):
    """The detailed page's per-step task tables, as Markdown."""
    return p.evaluate("""() => [...document.querySelectorAll('#detail .stage')].map(st => {
        const h = st.querySelector('h3').innerText.replace(/\\s+/g, ' ').trim(), tot = st.querySelector('.tot').innerText;
        const rows = [...st.querySelectorAll('tbody tr')].map(tr => {
          const c = [...tr.cells]; const task = c[0].querySelector('b').innerText, d = c[0].querySelector('div');
          return `| ${task}${d ? ' — ' + d.innerText : ''} | ${c[1].innerText} | ${c[3].innerText} | ${c[4].innerText} | ${c[5].innerText} |`; });
        return `#### ${h}\\n\\n${tot}\\n\\n| Task | Kind | Now | Future | Source |\\n|---|---|---:|---:|---|\\n${rows.join('\\n')}\\n`;
      }).join('\\n')""")


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    p = b.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=2, color_scheme="light")
    p.goto((D / "add-customer.html").as_uri()); p.wait_for_selector("#vsm rect"); time.sleep(2)
    for plat in PLATS:
        p.click(f"#p-{plat}"); time.sleep(0.8)
        p.locator("#vsm").screenshot(path=str(D / f"map-{plat}.png"))
        p.locator(".kpis").screenshot(path=str(D / f"summary-{plat}.png"))
    p.goto((D / "detailed.html").as_uri()); p.wait_for_selector("#strip i"); time.sleep(2)
    md = {}
    for plat in PLATS:
        p.click(f"#p-{plat}"); time.sleep(0.8)
        p.locator(".kpis").screenshot(path=str(D / f"detailed-summary-{plat}.png"))
        p.locator("section.panel").first.screenshot(path=str(D / f"detailed-strip-{plat}.png"))
        md[plat] = tables(p)
        md["imp:" + plat] = p.evaluate("""() => '| # | Improvement | Saves | Basis |\\n|---|---|---:|---|\\n' +
          [...document.querySelectorAll('#imp tr')].map(r => `| ${r.cells[0].innerText} | ${r.cells[1].innerText} | ${r.cells[3].innerText} | ${r.cells[4].innerText} |`).join('\\n')""")
    b.close()

doc = (D / "DETAILED.md").read_text()
for key, text in md.items():
    tag = key if key.startswith("imp:") else "tasks:" + key
    doc = re.sub(rf"(<!-- {tag} -->\n).*?(<!-- /{tag} -->)", lambda m: m[1] + text.rstrip() + "\n" + m[2], doc, flags=re.S)
(D / "DETAILED.md").write_text(doc)
print("images and DETAILED.md tables written")
