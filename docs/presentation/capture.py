"""Screenshots of the portal for the deck, with the boxes of the elements each slide points at (shots/boxes.json)."""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

LAB = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "shots"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8094"
W, H = 1600, 1000
seed = {u["username"]: u["password"] for u in json.loads((LAB / "webapp/users.seed.json").read_text())["users"]}
cust_pw = json.loads((LAB / "webapp/auth/test-accounts.json").read_text())["prairie"]["password"]
boxes = {}


def ctx_for(browser, user, pw):
    ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
    ctx.add_init_script("try { localStorage.setItem('c8d-theme', 'light'); } catch (e) {}")
    r = ctx.request.post(f"{URL}/api/auth/login", data={"username": user, "password": pw})
    assert r.ok, r.text()
    return ctx


def shot(page, name, callouts, scroll_to=None, top=None):
    """callouts: [(selector, label)] — the box of the first match, relative to the screenshot."""
    if scroll_to:
        page.eval_on_selector(scroll_to, "e => e.scrollIntoView({block: 'start'})")
        page.evaluate("window.scrollBy(0, -12)")
    if top is not None:
        page.evaluate(f"window.scrollTo(0, {top})")
    time.sleep(0.8)
    out = []
    for sel, label in callouts:
        b = (page.locator(sel).first.bounding_box(timeout=3000) if page.locator(sel).count() else None)
        if b is None:
            print(f"  {name}: {sel} not visible", file=sys.stderr)
            continue
        x, y = max(0, b["x"]), max(0, b["y"])
        w, h = min(W - x, b["width"]), min(H - y, b["height"] - (y - b["y"]))
        if h <= 4 or y >= H:
            print(f"  {name}: {sel} outside the view", file=sys.stderr)
            continue
        out.append({"label": label, "box": [x / W, y / H, w / W, h / H]})
    page.screenshot(path=str(OUT / f"{name}.png"))
    boxes[name] = out
    print(f"{name}: {len(out)} callouts")


def wait_state(page):
    page.wait_for_function("() => typeof ST !== 'undefined' && ST && ST.cloud && Object.keys(ST.cloud).length > 0", timeout=120_000)


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    # ---- sign in (no session) ----
    anon = b.new_context(viewport={"width": W, "height": H})
    anon.add_init_script("try { localStorage.setItem('c8d-theme', 'light'); } catch (e) {}")
    p = anon.new_page(); p.goto(URL + "/"); p.wait_for_selector("#login[open]")
    shot(p, "01_login", [("#login", "Sign in: every account has a role")])
    anon.close()

    ctx = ctx_for(b, "admin", seed["admin"])
    p = ctx.new_page()
    p.goto(URL + "/#cloud"); wait_state(p)
    p.evaluate("show('cloud')")
    shot(p, "02_cloud", [("#kpis", "Live KPIs"), ("#cloud", "Every router, read live"), ("#prov", "Both providers"),
                         ("#who", "Signed-in account and role")])
    p.evaluate("loadDrift().then(renderDrift)"); time.sleep(2)
    shot(p, "03_drift", [("#health", "Health: everything the model expects"), ("#drift-card", "Configuration drift vs the model")],
         scroll_to="#health")

    p.evaluate("show('map')"); p.evaluate("selectNode('cust2')"); time.sleep(1)
    shot(p, "04_map", [("#map", "Topology drawn from live state"), (".maptools", "Layers"), (".export", "Export SVG / PNG"),
                       ("#mapinfo", "Details: company, applications, live state")], top=0)

    p.evaluate("selectNode('host-cust4')"); time.sleep(1)
    p.evaluate("tracePath('host-cust4', 'host-cust5', true)")
    p.wait_for_function("() => !/tracing/.test(document.getElementById('mapcmd-when').innerText) && document.getElementById('mapcmd-when').innerText.length > 0", timeout=120_000)
    time.sleep(1)
    shot(p, "05_path", [("#map", "First via a hub, then the direct shortcut"), (".pathctl", "Trace / watch the shortcut form"),
                        ("#mapcmd", "Every trace, hop by hop")], scroll_to="#map")

    p.evaluate("show('sla')"); time.sleep(1)
    p.evaluate("$('sla-cust').value = 'cust2'; $('sla-win').value = '24h'; renderSla()")
    p.wait_for_selector("#sla-body .slakpis", timeout=60_000); time.sleep(1.5)
    shot(p, "06_sla", [(".slakpis", "Availability, latency, loss vs targets"), ("#sla-body svg", "Latency to each hub over time"),
                       (".slactl", "Customer, window, monthly PDF")], top=0)

    p.evaluate("show('resilience')"); time.sleep(2)
    fo = p.evaluate("fetch('/api/failover').then(r => r.json()).then(l => (l.find(x => x.kind === 'provider-down') || l[0]).run)")
    shot(p, "07_resilience", [("#f-kind", "Choose a failure"), ("#f-measure", "Measure failover"), ("#fo-list", "Every measurement")], top=0)
    p.evaluate(f"showFailover('{fo}')"); time.sleep(2)
    shot(p, "08_failover", [("#fo-detail h3", "One experiment"), ("#fo-detail table", "Each flow: verdict and outage timeline")],
         scroll_to="#fo-detail")

    p.evaluate("show('provision')"); time.sleep(2)
    shot(p, "09_provision", [("#tasks", "Every change is a job"), ("#sec-card", "Pre-shared key: rotate on demand")], top=0)

    p.evaluate("openModify()"); time.sleep(3)
    p.evaluate("$('m-cust').value = 'cust2'; loadModify()"); time.sleep(3)
    p.evaluate("$('m-dual').checked = true; checkModify()"); time.sleep(3)
    shot(p, "10_modify", [("#m-plat", "Router type"), ("#m-pref", "Preferred hub"), ("#m-dual-box", "Second provider"),
                          ("#m-plan", "The plan, before anything runs")], top=0)
    p.evaluate("mod.close()")

    p.evaluate("show('runs')"); time.sleep(2)
    job = p.evaluate("fetch('/api/runs').then(r => r.json()).then(l => (l.find(x => x.mode === 'rotatepsk' && x.status === 'success') || l[0]).id)")
    p.evaluate(f"watch('{job}')"); time.sleep(3)
    shot(p, "11_changes", [("#cr-t", "Change requests: four eyes"), ("#mt-active", "Maintenance")], top=0)
    p.evaluate(f"showJobConfig('{job}')"); time.sleep(3)
    shot(p, "12_job", [("#run-title", "Who started it, who approved it"), ("#run-body .run-prog", "Progress and duration"),
                       ("#run-body table", "Every step, with its result"), ("#job-cfg", "What changed on each router")],
         scroll_to="#run-title")

    p.evaluate("show('tools')"); time.sleep(3)
    shot(p, "13_tools", [("#users-card", "Accounts and roles (admin)"), ("#tools-list", "Every tool, with its login")], top=0)
    ctx.close()

    # ---- the customer's own view ----
    c = ctx_for(b, "prairie", cust_pw)
    p = c.new_page(); p.goto(URL + "/")
    p.wait_for_function("() => typeof ST !== 'undefined' && ST && ST.nodes", timeout=120_000); time.sleep(3)
    shot(p, "14_customer", [("#notices", "Service status, incidents, maintenance"), ("#map", "Only its own site"),
                            ("#mapinfo", "Its site and applications, live"), ("nav", "Its own pages")], top=0)
    p.evaluate("show('requests')"); time.sleep(3)
    shot(p, "15_requests", [("#cq-form", "Ask for a change"), ("#cq-t", "Progress of its requests")], top=0)
    p.evaluate("show('diag')"); time.sleep(1); p.evaluate("$('dg-apps').click()")
    p.wait_for_function("() => /five pings/.test(document.getElementById('dg-out').innerText)", timeout=120_000)
    shot(p, "16_diag", [(".diag .actions", "Tests from its own LAN host"), ("#dg-out", "Every application, every hub")], top=0)
    c.close()
    b.close()

(OUT / "boxes.json").write_text(json.dumps(boxes, indent=1))
