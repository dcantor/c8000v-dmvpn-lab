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
boxes = json.loads((OUT / "boxes.json").read_text()) if (OUT / "boxes.json").exists() else {}
PARTS = set(sys.argv[1:]) or {"tour", "more"}


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


def tour(b):
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


def dialog_scroll(page, dlg, sel):
    """Scroll a dialog so `sel` sits near its top."""
    page.evaluate(f"""(() => {{ const d = document.getElementById('{dlg}'), e = document.querySelector('{sel}');
        d.scrollTop += e.getBoundingClientRect().top - d.getBoundingClientRect().top - 60; }})()""")


def more(b):
    """Provision in depth, and the features the tour only touches."""
    ctx = ctx_for(b, "admin", seed["admin"])
    p = ctx.new_page()
    p.goto(URL + "/#provision"); wait_state(p)
    p.evaluate("show('provision')"); time.sleep(2)

    # add a customer: everything allocated, a plan before anything runs
    p.evaluate("openWizard()"); p.wait_for_selector("#wiz[open]"); time.sleep(3)
    shot(p, "20_add", [("#w-plat", "C8000v or VyOS"), ("#w-pref", "Preferred hub"), ("#w-dual-box", "Dual-homed onto provider 2"),
                       ("#w-name", "Name, region, addresses: allocated"), ("#w-tidx", "Tunnel index, provider port, LAN host"),
                       ("#w-co", "The customer company")])
    p.evaluate("$('w-plat').value = 'vyos'; $('w-dual').checked = true; check()"); time.sleep(4)
    dialog_scroll(p, "wiz", "#w-apps")
    shot(p, "21_add_plan", [("#w-apps", "Applications it subscribes to"), ("#w-plan", "The plan: every router it touches"),
                            ("#w-go", "Provision: a change request or a job")])
    p.evaluate("wiz.close()")

    # remove a customer
    p.evaluate("pickCustomer()"); p.wait_for_selector("#rm[open]"); time.sleep(1)
    p.evaluate("$('r-cust').value = 'cust5'; checkRemoval()"); time.sleep(4)
    shot(p, "22_remove", [("#r-cust", "Which customer"), ("#r-co", "What goes with it"), ("#r-plan", "The plan"),
                          ("#rm-go", "Remove: needs approval")])
    dialog_scroll(p, "rm", "#r-plan")
    p.evaluate("rm.close()")

    # restore from a backup: upload one, see what it would change
    bk = sorted((LAB / "webapp/backups").glob("*.tar.gz"))
    p.evaluate("openRestore()"); p.wait_for_selector("#rs[open]")
    p.set_input_files("#rs-file", str(bk[0]))
    p.wait_for_function("() => /Then/.test(document.getElementById('rs-plan').innerText)", timeout=60_000); time.sleep(1)
    shot(p, "23_restore", [("#rs-file", "A backup made by Back up the lab"), ("#rs-man", "What is in it, checksums verified"),
                           ("#rs-plan", "What restoring would change"), ("#rs-go", "Restore: needs approval")])
    p.evaluate("rs.close()")
    for f in set((LAB / "webapp/backups").glob("*.tar.gz")) - set(bk):      # the upload was kept as a new backup: drop the copy
        if f.read_bytes() == bk[0].read_bytes():
            f.unlink()

    # the pre-shared key card
    shot(p, "24_psk", [("#sec-body tr:nth-child(1)", "Only a fingerprint, never the key"), ("#sec-body tr:nth-child(2)", "When, by whom, which job"),
                       ("#sec-body tr:nth-child(3)", "Every router that uses it"), ("#sec-rotate", "Admins only")], scroll_to="#sec-card")

    # jobs: the list, a deploy, a test run
    p.evaluate("show('runs')"); time.sleep(2)
    shot(p, "25_jobs", [("#runs", "One job at a time; progress for each")], scroll_to="#runs")
    runs = p.evaluate("fetch('/api/runs').then(r => r.json())")
    dep = next(r["id"] for r in runs if r["mode"] == "deploy" and r["status"] == "success")
    tst = next(r["id"] for r in runs if r["mode"] == "test" and r["status"] == "success")
    p.evaluate(f"watch('{dep}')"); time.sleep(3)
    shot(p, "26_deploy", [("#run-title", "Deploy the model"), ("#run-body ol", "Render, NAC, providers, VyOS, Nautobot, verify")],
         scroll_to="#run-title")
    p.evaluate(f"watch('{tst}')"); time.sleep(3)
    shot(p, "27_tests", [("#run-title", "Run the tests"), ("#run-body ol", "Each suite, with its result"),
                         ("#run-body a[href*='report']", "The Robot report and log")], scroll_to="#run-title")

    # change policy and maintenance
    p.evaluate("$('pol-toggle').click()"); time.sleep(1.5)
    shot(p, "28_policy", [("#pol", "What needs approval, four eyes, windows, emergencies"), ("#cr-policy", "The policy in one line")], top=0)
    p.evaluate("$('pol-toggle').click()"); time.sleep(0.5)
    shot(p, "29_maint", [("#v-runs > .card:nth-of-type(2)", "Maintenance: declared, or automatic"), ("#mt-go", "Declare it"),
                         ("#runs", "Every job: progress, duration, tests")], scroll_to="#v-runs > .card:nth-of-type(2)")

    # a router's configuration history
    p.evaluate("show('map')"); p.evaluate("selectNode('hub-east')"); time.sleep(1)
    p.evaluate("showRouterHistory('hub-east')"); time.sleep(3)
    shot(p, "30_history", [("#mapinfo", "A router's live details"), ("#mapcmd", "Every job that changed it")], scroll_to="#mapcmd")

    # the API
    p.goto(URL + "/docs"); p.wait_for_selector(".opblock-tag", timeout=60_000); time.sleep(2)
    shot(p, "31_api", [(".information-container", "Everything the portal does, as an API"), (".opblock-tag-section", "Grouped by what it acts on")])
    ctx.close()

    # dark mode
    ctx = b.new_context(viewport={"width": W, "height": H})
    ctx.add_init_script("try { localStorage.setItem('c8d-theme', 'dark'); } catch (e) {}")
    assert ctx.request.post(f"{URL}/api/auth/login", data={"username": "operator", "password": seed["operator"]}).ok
    p = ctx.new_page(); p.goto(URL + "/#map"); wait_state(p); p.evaluate("show('map')"); time.sleep(2)
    shot(p, "32_dark", [("#map", "The same live map, dark"), ("#who", "An operator: no admin tasks")], top=0)
    ctx.close()


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    if "tour" in PARTS:
        tour(b)
    if "more" in PARTS:
        more(b)
    b.close()

(OUT / "boxes.json").write_text(json.dumps(boxes, indent=1))
