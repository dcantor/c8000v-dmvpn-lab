"""Records the demo walkthrough: the portal (staff, then a customer), Nautobot and Grafana, each as a browser video with
captions, a visible pointer and highlight rings drawn into the page. make_video.py turns the recordings and the deck's
slides into the MP4.

Nothing here starts a job: it opens pages and dialogs, reads plans, traces a path (which clears one NHRP shortcut; phase 3
rebuilds it) and runs the customer's own diagnostics (pings and HTTP checks).

    webapp/.venv/bin/python docs/presentation/demo.py            # all segments, or: portal customer nautobot grafana
    webapp/.venv/bin/python docs/presentation/demo.py --narrated # paced to the voice-over, into recordings/narrated/
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

D = Path(__file__).resolve().parent
LAB = D.parents[1]
REC = D / "recordings"
REC.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8094"
PUB = os.environ.get("LAB_PUBLIC", "192.168.50.231")
GRAFANA, NAUTOBOT = f"http://{PUB}:3001", f"http://{PUB}:8080"
NB_STATE = os.environ.get("NB_STATE", str(Path.home() / ".cache/c8d/nautobot-state.json"))
W, H = 1920, 1080
NARRATED = "--narrated" in sys.argv           # pace every scene to its voice-over line (narration.py) and log when each starts
SEGS = [a for a in sys.argv[1:] if not a.startswith("--")] or ["portal", "customer", "nautobot", "grafana"]
if NARRATED:
    REC = REC / "narrated"
    REC.mkdir(exist_ok=True)
    sys.path.insert(0, str(D))
    import narration
    VOICE = narration.durations()
    missing = set(narration.LINES) - set(VOICE)
    if missing:
        sys.exit(f"run narration.py first: no voice for {', '.join(sorted(missing))}")
seed = {u["username"]: u["password"] for u in json.loads((LAB / "webapp/users.seed.json").read_text())["users"]}
cust_pw = json.loads((LAB / "webapp/auth/test-accounts.json").read_text())["prairie"]["password"]

# the overlay every recorded page gets: a caption bar, a pointer that follows the mouse, rings around what is described
OVERLAY = r"""
(() => {
  const css = `
  #__cap { position:fixed; left:50%; bottom:34px; transform:translateX(-50%); z-index:2147483646; max-width:1500px;
    background:rgba(11,27,46,.92); color:#fff; font:500 27px/1.35 'Segoe UI',Ubuntu,'Liberation Sans',sans-serif; padding:16px 30px 18px;
    border-radius:14px; box-shadow:0 10px 30px rgba(0,0,0,.35); opacity:0; transition:opacity .35s; pointer-events:none; text-align:center }
  #__cap .k { display:block; font:700 15px/1.2 'Segoe UI',Ubuntu,'Liberation Sans',sans-serif; letter-spacing:.12em; color:#5ec8db;
    text-transform:uppercase; margin-bottom:6px }
  #__ptr { position:fixed; left:0; top:0; width:30px; height:30px; z-index:2147483647; pointer-events:none; transition:transform .02s;
    filter:drop-shadow(0 2px 3px rgba(0,0,0,.45)) }
  .__ring { position:fixed; z-index:2147483645; pointer-events:none; border:4px solid #ea580c; border-radius:12px;
    box-shadow:0 0 0 4px rgba(234,88,12,.18); animation:__pulse 1.4s ease-in-out infinite }
  @keyframes __pulse { 50% { box-shadow:0 0 0 12px rgba(234,88,12,.08) } }
  .__rip { position:fixed; z-index:2147483646; width:16px; height:16px; margin:-8px 0 0 -8px; border-radius:50%;
    border:3px solid #ea580c; pointer-events:none; animation:__rip .5s ease-out forwards }
  @keyframes __rip { to { transform:scale(3.2); opacity:0 } }`;
  let x = -50, y = -50;
  const ready = () => {
    if (document.getElementById('__cap') || !document.body) return;
    const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    const cap = document.createElement('div'); cap.id = '__cap'; document.body.appendChild(cap);
    const p = document.createElement('div'); p.id = '__ptr';
    p.innerHTML = '<svg viewBox="0 0 24 24" width="30" height="30"><path d="M4 2l15 11-6.5 1.2 3.8 7.3-2.9 1.5-3.8-7.4L4 20z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    p.style.transform = `translate(${x}px,${y}px)`; document.body.appendChild(p);
  };
  document.addEventListener('DOMContentLoaded', ready); setInterval(ready, 500);
  addEventListener('mousemove', e => { x = e.clientX; y = e.clientY; const p = document.getElementById('__ptr');
    if (p) p.style.transform = `translate(${x - 3}px,${y - 2}px)`; }, true);
  addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = '__rip';
    r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.body.appendChild(r); setTimeout(() => r.remove(), 600); }, true);
  const rings = [];
  const place = () => rings.forEach(([el, r]) => { const b = el.getBoundingClientRect();
    Object.assign(r.style, { left: b.left - 7 + 'px', top: b.top - 7 + 'px', width: b.width + 6 + 'px', height: b.height + 6 + 'px' }); });
  setInterval(place, 100);
  window.__demo = {
    cap(k, t) { ready(); const c = document.getElementById('__cap'); c.innerHTML = (k ? `<span class="k">${k}</span>` : '') + t; c.style.opacity = t ? 1 : 0; },
    ring(sel) { ready(); const el = typeof sel === 'string' ? document.querySelector(sel) : sel; if (!el) return false;
      const r = document.createElement('div'); r.className = '__ring'; document.body.appendChild(r); rings.push([el, r]); place(); return true; },
    unring() { rings.splice(0).forEach(([, r]) => r.remove()); },
  };
})();
"""
marks = None                                                     # read in main, after REC is final


class Seg:
    """One recorded browser context: its video, and when (seconds into it) the demo proper starts."""

    def __init__(self, browser, name, storage_state=None, zoom=None):
        self.name, self.t0 = name, time.time()
        self.ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1, storage_state=storage_state,
                                       record_video_dir=str(REC / f"_{name}"), record_video_size={"width": W, "height": H})
        self.ctx.add_init_script(OVERLAY)
        self.ctx.add_init_script("try { localStorage.setItem('c8d-theme', 'light'); } catch (e) {}")
        self.p = self.ctx.new_page()
        self.start = None
        self.events, self.until = [], 0.0                          # [(seconds into the video, narration key)]; when the voice ends

    def begin(self):
        self.start = time.time() - self.t0

    def speak(self, key):
        """Narrated: wait for the previous line to finish, then start this one (make_video.py places its audio here)."""
        if not NARRATED:
            return
        time.sleep(max(0.0, self.until - time.time()))
        self.events.append([round(time.time() - self.t0, 2), key])
        self.until = time.time() + VOICE[key]["secs"] + 0.45

    def cap(self, kicker, text, hold=0.0, say=None):
        if say:
            self.speak(say)
        self.p.evaluate("([k, t]) => window.__demo.cap(k, t)", [kicker, text]); time.sleep(hold)

    def ring(self, sel, hold=0.0):
        loc = self.p.locator(sel)                                   # Playwright selectors (:has-text, text-is) as well as CSS
        ok = loc.count() and loc.first.evaluate("e => window.__demo.ring(e)")
        if not ok:
            print(f"  {self.name}: nothing to ring at {sel}", file=sys.stderr)
        time.sleep(hold)

    def unring(self):
        self.p.evaluate("window.__demo.unring()")

    def point(self, sel, click=False, steps=28, wait=True):
        if click and wait and NARRATED:                             # don't move on while the voice still describes this screen
            time.sleep(max(0.0, self.until - time.time() - 0.3))
        loc = self.p.locator(sel).first
        loc.scroll_into_view_if_needed(timeout=5000)
        b = loc.bounding_box()
        x, y = b["x"] + min(b["width"] / 2, 60), b["y"] + b["height"] / 2
        self.p.mouse.move(x, y, steps=steps); time.sleep(0.35)
        if click:
            self.p.mouse.click(x, y); time.sleep(0.4)

    def follow(self, sel):
        """Point at a link, then open it in this tab (some Nautobot links open a new one)."""
        self.point(sel)
        href = self.p.locator(sel).first.get_attribute("href")
        self.p.mouse.down(); self.p.mouse.up()
        self.p.goto(href if href.startswith("http") else NAUTOBOT + href); self.p.wait_for_load_state("networkidle"); time.sleep(1.2)

    def scroll(self, dy, steps=12, pause=0.05):
        """Scroll the page itself (the mouse wheel would scroll whatever panel is under the pointer)."""
        self.p.evaluate("dy => (document.scrollingElement || document.body).scrollBy({ top: dy, behavior: 'smooth' })", dy)
        time.sleep(steps * pause + 0.8)

    def scroll_to(self, sel, offset=90):
        self.p.locator(sel).first.evaluate("(e, o) => window.scrollTo({ top: e.getBoundingClientRect().top + scrollY - o, behavior: 'smooth' })", offset)
        time.sleep(1.2)

    def close(self):
        video = self.p.video
        time.sleep(max(0.0, self.until - time.time()) + 0.3)       # let the last line finish
        self.cap("", "")
        time.sleep(0.6)
        self.ctx.close()
        src = Path(video.path())
        dst = REC / f"{self.name}.webm"
        src.replace(dst)
        shutil.rmtree(src.parent)                                   # any popup's video with it
        marks[self.name] = {"start": round(self.start or 0, 2), "file": dst.name, "events": self.events}
        print(f"{self.name}: {dst.name}, demo starts at {marks[self.name]['start']} s")


def login(ctx, user, pw):
    r = ctx.request.post(f"{URL}/api/auth/login", data={"username": user, "password": pw})
    assert r.ok, r.text()


def wait_state(p):
    p.wait_for_function("() => typeof ST !== 'undefined' && ST && ST.nodes", timeout=120_000)


def portal(b):
    s = Seg(b, "portal"); login(s.ctx, "admin", seed["admin"]); p = s.p
    p.goto(URL + "/#cloud"); wait_state(p); p.evaluate("show('cloud')"); time.sleep(2)
    p.mouse.move(W / 2, H / 2); s.begin()
    s.cap("The cloud", "Every hub and customer router, read live: registrations, IPsec, BGP and the provider", 1.5, say="p_cloud")
    s.ring("#kpis", 3); s.unring(); s.ring("#cloud", 3.5); s.unring()
    s.scroll_to("#health")
    s.cap("Health and drift", "What the model expects, checked — and every router compared with the configuration lab.conf renders", 1, say="p_drift")
    s.ring("#health"); s.ring("#drift-card", 4); s.unring()

    p.evaluate("window.scrollTo({top:0, behavior:'smooth'})"); time.sleep(0.8)
    s.cap("Network map", "Hubs, both providers, every customer and LAN host — drawn from the routers' live state", say="p_map")
    s.point("nav a[data-view=map]", click=True, wait=False); time.sleep(4)
    s.point("#map g.node[data-node=cust2]", click=True); time.sleep(1)
    s.cap("Customer details", "The customer company, its applications and their live status, checked every minute", 0.5, say="p_details")
    s.ring("#mapinfo", 4); s.unring()
    s.point("#map g.node[data-node=host-cust4]", click=True); time.sleep(1)
    p.select_option("#trace-to", "host-cust5"); time.sleep(0.5)
    s.cap("DMVPN phase 3", "Watch the shortcut form: the first packets go through a hub, then NHRP builds a direct tunnel", 0.3, say="p_phase3a")
    s.point("#mapinfo [data-watch]", click=True, wait=False)
    p.wait_for_function("() => { const t = document.getElementById('mapcmd-when').innerText; return t && !/tracing/.test(t); }", timeout=120_000)
    time.sleep(1); s.scroll_to("#map", 70)
    s.cap("DMVPN phase 3", "The traced path: through a hub first, then customer-to-customer over the new shortcut (purple)", 5, say="p_phase3b")

    p.evaluate("window.scrollTo({top:0, behavior:'smooth'})"); time.sleep(0.8)
    s.point("nav a[data-view=sla]", click=True)
    p.evaluate("$('sla-cust').value = 'cust2'; $('sla-win').value = '24h'; renderSla()")
    p.wait_for_selector("#sla-body .slakpis", timeout=60_000); time.sleep(1)
    s.cap("Service levels", "Per customer: availability, latency, loss and tunnel uptime against the SLA — with a monthly PDF", 0.5, say="p_sla")
    s.ring(".slakpis", 3); s.unring(); s.ring("#sla-body svg", 3); s.unring()

    s.point("nav a[data-view=resilience]", click=True); time.sleep(1.5)
    s.cap("Resilience", "Fail a hub, a provider or a circuit on purpose — every host pings every other while it is down", 1, say="p_res1")
    s.ring("#fo-list", 3.5); s.unring()
    s.point("#fo-list [data-fo]", click=True); time.sleep(2); s.scroll_to("#fo-detail")
    s.cap("Resilience", "One experiment, flow by flow: who failed over, who was cut off, and for how long", 4, say="p_res2")

    p.evaluate("window.scrollTo({top:0, behavior:'smooth'})"); time.sleep(0.8)
    s.point("nav a[data-view=provision]", click=True); time.sleep(1.5)
    s.cap("Provision", "Every change is a job: pick a task, see its plan, and it runs — with approval where the policy asks", 1, say="p_prov")
    s.ring("#tasks", 3); s.unring()
    s.point("#tasks button.task[data-i='0']", click=True); p.wait_for_selector("#wiz[open]"); time.sleep(1.5)
    s.cap("Add a customer", "Router type, preferred hub, addresses, ports, the tunnel index — all allocated and checked free", 1, say="p_add1")
    s.ring("#w-plat"); s.ring("#w-name"); s.ring("#w-tidx", 2.5)
    s.unring(); s.point("#w-plat"); p.select_option("#w-plat", "vyos"); time.sleep(0.6)
    s.point("#w-dual", click=True, wait=False); time.sleep(3)
    p.evaluate("(() => { const d = document.getElementById('wiz'), e = document.getElementById('w-plan'); d.scrollTo({ top: e.offsetTop - 200, behavior: 'smooth' }); })()")
    time.sleep(1.2)
    s.cap("Add a customer", "The plan before anything runs: a VyOS router, dual-homed onto both providers — no hub changes", 0.5, say="p_add2")
    s.ring("#w-plan", 4); s.unring()
    s.point("#wiz button[onclick='wiz.close()']", click=True); time.sleep(0.8)

    s.cap("Modify a customer", "Company, applications, preferred hub, a second provider, even the router type", say="p_mod")
    s.point("#tasks button.task[data-i='1']", click=True, wait=False); p.wait_for_selector("#mod[open]"); time.sleep(2)
    p.select_option("#m-cust", "cust2"); p.evaluate("loadModify()"); time.sleep(3)
    s.point("#m-dual", click=True, wait=False); time.sleep(3)
    s.cap("Modify a customer", "The plan: every router the change touches, and whether any is rebuilt", say="p_mplan")
    s.ring("#m-plan", 3.5); s.unring()
    s.point("#mod button[onclick='mod.close()']", click=True); time.sleep(0.8)
    s.scroll_to("#sec-card")
    s.cap("Security", "Rotate the DMVPN pre-shared key: a random key on every router, each IKE session re-made and verified", 0.5, say="p_psk")
    s.ring("#sec-card", 4); s.unring()

    p.evaluate("window.scrollTo({top:0, behavior:'smooth'})"); time.sleep(0.8)
    s.point("nav a[data-view=runs]", click=True); time.sleep(1.5)
    s.cap("Change control", "Four eyes: someone other than the requester approves — within change windows, if you want them", 1, say="p_cr")
    s.ring("#cr-t", 3); s.unring()
    s.cap("Change control", "The policy: what needs approval, four eyes, emergencies, change windows", say="p_policy")
    s.point("#pol-toggle", click=True, wait=False); time.sleep(1)
    s.ring("#pol", 3.5); s.unring(); s.point("#pol-toggle", click=True); time.sleep(0.5)
    job = p.evaluate("fetch('/api/runs').then(r => r.json()).then(l => l.find(x => x.mode === 'rotatepsk' && x.status === 'success').id)")
    s.scroll_to("#runs", 120)
    s.cap("Jobs", "Every job with its progress, duration and tests", 1, say="p_jobs")
    s.point(f"#runs tr[onclick*='{job}']", click=True); time.sleep(2); s.scroll_to("#run-title", 70)
    s.cap("Audit", "Who asked, who approved, every step — and exactly what changed on each router", 1, say="p_audit1")
    s.ring("#run-body ol", 3); s.unring()
    s.point("#run-body button[onclick^='showJobConfig']", click=True); time.sleep(2); s.scroll_to("#job-cfg", 140)
    s.cap("Audit", "The configuration diff per router: the key changed everywhere — and is never shown", 4.5, say="p_audit2")
    s.close()


def customer(b):
    s = Seg(b, "customer"); login(s.ctx, "prairie", cust_pw); p = s.p
    p.goto(URL + "/"); wait_state(p); time.sleep(3)
    p.mouse.move(W / 2, H / 2); s.begin()
    s.cap("The customer's view", "Prairie Grain Logistics signs in and sees only its own service", 1.5, say="c_view")
    s.ring("#notices", 2.5); s.unring(); s.ring("#map", 2.5); s.unring(); s.ring("#mapinfo", 3); s.unring()
    s.point("nav a[data-view=requests]", click=True); time.sleep(1.5)
    s.cap("Self-service", "Ask for a change — applications, preferred hub, a second provider — and follow its approval", 1, say="c_req")
    s.ring("#cq-form", 3); s.unring(); s.ring("#cq-t", 2.5); s.unring()
    s.point("nav a[data-view=diag]", click=True); time.sleep(1)
    s.cap("Diagnostics", "Test every application from the customer's own LAN host, right now", 0.5, say="c_diag")
    s.point("#dg-apps", click=True, wait=False)
    p.wait_for_function("() => /five pings/.test(document.getElementById('dg-out').innerText)", timeout=120_000)
    time.sleep(0.8); s.ring("#dg-out", 4.5); s.unring()
    s.close()


def nautobot(b):
    if not Path(NB_STATE).exists():
        print("nautobot: no session (run nb_session.py); skipped", file=sys.stderr)
        return
    s = Seg(b, "nautobot", storage_state=NB_STATE); p = s.p

    def go(path):
        p.goto(NAUTOBOT + path); p.wait_for_load_state("networkidle"); time.sleep(1.5)

    go("/tenancy/tenants/?tenant_group=c8000v-dmvpn-lab+customers")
    p.mouse.move(W / 2, H / 2); s.begin()
    s.cap("Nautobot: the source of truth", "Each customer is a tenant: the companies behind the lab's sites", 2.5, say="n_tenants")
    s.follow("a:text-is('Prairie Grain Logistics')")
    s.cap("Nautobot: the customer", "Its details as custom fields, and the applications it subscribes to", 0.5, say="n_tenant")
    s.ring(".card:has(> .card-header:has-text('Custom Fields'))", 2); s.ring(".card:has(> .card-header:has-text('Relationships'))", 3); s.unring()
    s.cap("Nautobot: the VPN", "A hub, and the interfaces that carry the VPN", say="n_hub")
    go("/dcim/devices/?q=c8d-hub-east")
    s.point("table a:text-is('c8d-hub-east')")                   # straight to its interfaces
    href = p.locator("table a:text-is('c8d-hub-east')").first.get_attribute("href")
    p.mouse.down(); p.mouse.up()
    p.goto(NAUTOBOT + href.rstrip("/") + "/interfaces/"); p.wait_for_load_state("networkidle"); time.sleep(1)
    s.cap("Nautobot: the VPN", "The hub's two DMVPN clouds as interfaces: Tunnel0 over mpls, Tunnel1 over mpls2", 0.5, say="n_ifaces")
    s.ring("tr:has(a:text-is('Tunnel0'))"); s.ring("tr:has(a:text-is('Tunnel1'))", 3.5); s.unring()
    go("/ipam/prefixes/?location=c8000v-dmvpn-lab&sort=-prefix")
    s.cap("Nautobot: addressing", "Every prefix with its role — the overlays, the site LANs, the access links", 0.5, say="n_prefix")
    s.ring("tr:has(td:has-text('172.28.0.0/24'))"); s.ring("tr:has(td:has-text('172.29.0.0/24'))", 3.5); s.unring()
    go("/plugins/bgp/peerings/?q=c8d")
    s.cap("Nautobot: routing", "Every BGP session of the design, hub to hub, hub to customer, site to provider", 3.5, say="n_bgp")
    go("/extras/config-contexts/?q=c8000v-dmvpn-lab")
    s.follow("table a:text-is('c8000v-dmvpn-lab')")
    s.scroll_to(".card:has(> .card-header:has-text('Toggle Data'))", 80)
    s.cap("Nautobot: the service", "The DMVPN service as a config context — rendered into every router, byte for byte (no secrets)", 4.5, say="n_ctx")
    s.close()


def grafana(b):
    s = Seg(b, "grafana"); p = s.p
    p.goto(f"{GRAFANA}/d/c8000v-dmvpn-lab-overview?orgId=1&from=now-24h&to=now&kiosk&theme=light")
    p.wait_for_selector(".react-grid-item:has(h2:text-is('Health'))"); time.sleep(8)
    p.mouse.move(W / 2, H / 2); s.begin()
    pan = lambda t: f".react-grid-item:has(h2:text-is('{t}'))"
    s.cap("Monitoring: Grafana", "The DMVPN cloud over 24 hours — purple marks are the portal's own jobs and failover tests", 1.5, say="g_cloud")
    s.ring(pan("Registrations (customers x hubs)"), 2); s.unring()
    s.ring(pan("Customer registered with every hub (NHRP NHS up = expected)"), 3.5); s.unring()
    s.cap("Monitoring: Grafana", "Overlay BGP, router CPU and the provider's own telemetry", 1, say="g_provider")
    s.scroll(700, 16)
    s.ring(pan("Access links: traffic per port (bit/s)"), 3); s.unring()
    s.cap("Monitoring: Grafana", "Router syslog in VictoriaLogs, next to the metrics", 1, say="g_logs")
    s.scroll(620, 14)
    s.ring(pan("C8000v syslog (newest first)"), 3.5); s.unring()
    s.cap("Monitoring: Grafana", "The lab host, every VM and LAN host — and the outcome of the portal's jobs", say="g_lab")
    s.scroll(1400, 24)
    p.wait_for_selector(pan("Portal runs: last outcome per mode"), timeout=15_000); time.sleep(1)
    s.ring(pan("Portal runs: last outcome per mode"), 4); s.unring()
    s.close()


marks = json.loads((REC / "marks.json").read_text()) if (REC / "marks.json").exists() else {}
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    for seg in SEGS:
        {"portal": portal, "customer": customer, "nautobot": nautobot, "grafana": grafana}[seg](b)
        (REC / "marks.json").write_text(json.dumps(marks, indent=1))
    b.close()
