#!/usr/bin/env python3
"""The c8000v-dmvpn-lab provisioning portal.

The UI (static/index.html) shows the cloud with live state — which customers are registered with which hubs, which
IPsec sessions are up, which customer-to-customer shortcuts have formed, and the provider's eBGP sessions — and
drives the pipelines:

    add a customer     lab.conf + render -> the C8000v and its host -> day-0 (license reload) -> Network-as-Code
                       (only the new router gains resources) -> the provider's new link -> Nautobot -> verify -> tests
    remove a customer  the reverse: Terraform forgets it, the VMs go, lab.conf, the provider port, Nautobot
    deploy             re-render, NAC apply, provider push, Nautobot seed and compare
    plan               terraform plan + Nautobot render --check; changes nothing
    test               the Robot suites

Adding a customer never touches a hub: it registers with NHRP and arrives on the hubs' BGP listen range. Runs execute
one at a time; their state is mirrored to runs/<id>.json. Start with ./lab.sh webapp."""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from labportal import RunBase, RunRegistry, exposition, install_runs_api, metric_line, metrics_generated, run_metrics
from pydantic import BaseModel, Field

import customers as C
from state import State, ios

LAB = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
RUNS_DIR.mkdir(exist_ok=True)
RESULTS = LAB / "results"
PY = str(LAB / "tests" / ".venv" / "bin" / "python")

STEP_TITLES = {
    "validate": "Validate the allocation against the running lab",
    "labconf": "Register in lab.conf and render the configuration",
    "vm": "Create and boot the C8000v and its LAN host",
    "bootstrap": "Day-0 over the serial console (license level, reload), wait for RESTCONF",
    "nac": "Network-as-Code: terraform apply (resources on the new router only)",
    "provider": "The provider: address the new access link; the customer arrives on its listen range",
    "nautobot": "Nautobot source of truth (seed)",
    "verify": "Verify: registered with every hub, IPsec and BGP up, hosts reach each other, Nautobot == lab.conf",
    "test": "Robot Framework suites",
    "rm_validate": "Validate the removal",
    "rm_nac": "Terraform forgets the router (its resources leave the state; the hubs are not touched)",
    "rm_vm": "Power off and delete the C8000v and its host",
    "rm_labconf": "Remove from lab.conf and re-render",
    "rm_provider": "Release the provider port (address removed, port disabled)",
    "rm_nautobot": "Remove the customer from Nautobot",
    "render": "Render the configuration from lab.conf",
    "plan": "terraform plan: what Network-as-Code would change",
    "check": "Compare Nautobot's rendering with lab.conf's",
}
TAGS = [{"name": "monitoring", "description": "Prometheus: /metrics and /api/sd, scraped by the NMS."},
        {"name": "state", "description": "The cloud, the model and the live state of every router."},
        {"name": "provisioning", "description": "Suggest, validate and plan a customer; plan a removal."},
        {"name": "runs", "description": "Pipeline runs: add a customer, remove one, deploy, plan, test."}]
app = FastAPI(title="c8000v-dmvpn-lab Provisioning Portal API", version="1.0", openapi_tags=TAGS,
              docs_url="/docs", redoc_url="/redoc",
              description="REST API behind the C8000v DMVPN lab's portal. Every change goes **lab.conf → rendered "
                          "configuration → VMs → Network-as-Code → provider → Nautobot → verification → tests**; runs "
                          "are asynchronous (`POST /api/runs`, poll `GET /api/runs/{id}`). UI: [/](/)")
registry = RunRegistry(RUNS_DIR)
state = State()


class CustomerSpec(BaseModel):
    name: str = Field(examples=["cust4"])
    host: str = Field(examples=["host-cust4"])
    region: str = Field(examples=["East"])
    mgmt_ip: str
    host_mgmt: str
    t_idx: int
    tunnel_ip: str
    nbma: str
    router_id: str
    lan: str
    lan_port: str = "GigabitEthernet3"
    provider: str
    provider_port: str
    wan_prefix: str
    idx: int
    console: int
    host_idx: int
    host_console: int


class RunRequest(BaseModel):
    mode: str = Field(examples=["customer"], description="customer | remove | deploy | plan | test")
    customer: CustomerSpec | None = None
    name: str | None = Field(None, description="remove: the customer to remove")
    options: dict = Field(default_factory=dict, description="{test: bool (default true), suites: [..]}")


class Run(RunBase):
    LAB = "c8000v-dmvpn-lab"
    STEP_TITLES = STEP_TITLES
    EXTRA = {"customer": "customer", "spec": "spec", "removal": "removal"}

    def __init__(self, mode, spec, options, resume_of=None):
        self.spec = spec
        self.removal = (resume_of or {}).get("removal")
        self.customer = (spec or {}).get("name")
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)

    def plan(self):
        if self.mode == "test":
            return ["test"]
        if self.mode == "plan":
            return ["plan", "check"]
        if self.mode == "deploy":
            steps = ["render", "nac", "provider", "nautobot", "check"]
        elif self.mode == "remove":
            steps = ["rm_validate", "rm_nac", "rm_vm", "rm_labconf", "nac", "rm_provider", "rm_nautobot", "nautobot", "verify"]
        else:
            steps = ["validate", "labconf", "vm", "bootstrap", "nac", "provider", "nautobot", "verify"]
        if self.options.get("test", self.mode != "plan"):
            steps.append("test")
        return steps

    def after(self):
        state._cache.clear()

    # ---- add a customer ---------------------------------------------------------------------------------------
    def do_validate(self, s):
        problems = C.validate(self.spec)
        if problems:
            raise RuntimeError("; ".join(problems))
        s["summary"] = (f"{self.spec['name']}: {self.spec['lan']} behind {self.spec['host']}, "
                        f"{self.spec['wan_prefix']} on {self.spec['provider']} {self.spec['provider_port']}")

    def do_labconf(self, s):
        C.apply_to_labconf(self.spec)
        self.say(f"lab.conf: {self.spec['name']} ({self.spec['mgmt_ip']}, index {self.spec['t_idx']}) and {self.spec['host']}")
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        s["summary"] = f"{self.spec['name']} and {self.spec['host']} registered; day-0, provider and NAC model re-rendered"

    def do_vm(self, s):
        self.say("the provider keeps its definition: every one of its ports already exists in the VM, wired or not")
        self.sh([LAB / "lab.sh", "up", self.spec["name"], self.spec["host"]])
        s["summary"] = f"{self.spec['name']} and {self.spec['host']} started"

    def do_bootstrap(self, s):
        self.sh([LAB / "lab.sh", "bootstrap", self.spec["name"], self.spec["host"]], timeout=3600)
        s["summary"] = "day-0 applied, crypto licence active, RESTCONF answering"

    def do_nac(self, s):
        # after a removal this changes no router — the departed one is already out of the state — but it rewrites
        # Terraform's local copy of the model, which would otherwise read as drift in the next plan
        self.sh([LAB / "lab.sh", "nac", "init", "-input=false", "-no-color"])
        self.sh([LAB / "lab.sh", "nac", "apply", "-auto-approve", "-parallelism=1", "-input=false", "-no-color"],
                timeout=3600)
        s["summary"] = "applied and saved"

    def do_provider(self, s):
        self.sh([LAB / "lab.sh", "configure"])
        s["summary"] = "provider re-pushed (idempotent; a changed port is reconciled)"

    def do_nautobot(self, s):
        self.sh([LAB / "lab.sh", "nautobot", "seed"])
        s["summary"] = "seeded"

    def do_verify(self, s):
        name = (self.spec or {}).get("name") if self.mode == "customer" else None
        self.say("waiting for NHRP registration, IPsec and BGP to settle")
        deadline = time.time() + 240
        while True:
            snap = state.get(refresh=True)
            if snap["health"]["ok"] or time.time() > deadline:
                break
            self.say("  not yet: " + "; ".join(snap["health"]["problems"][:4]))
            time.sleep(20)
        if not snap["health"]["ok"]:
            raise RuntimeError("; ".join(snap["health"]["problems"]))
        self.sh([PY, LAB / "tools" / "host_cmd.py", "matrix"])
        self.sh([LAB / "lab.sh", "nautobot", "render", "--check"])
        s["summary"] = (f"{name}: registered with every hub, IPsec and BGP up; " if name else "the cloud is healthy; ") \
            + "every host reaches every other; Nautobot == lab.conf"

    def do_test(self, s):
        suites = self.options.get("suites") or ["all"]
        args = [f"suites/{x}.robot" for x in suites] if suites != ["all"] else []
        self.record_tests(RESULTS, s, self.sh([LAB / "tests" / "run.sh", *args], check=False, timeout=5400))

    # ---- deploy / plan ----------------------------------------------------------------------------------------
    def do_render(self, s):
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        s["summary"] = "configuration re-rendered from lab.conf"

    def do_plan(self, s):
        rc = self.sh([LAB / "lab.sh", "nac", "plan", "-detailed-exitcode", "-no-color", "-input=false", "-lock=false"],
                     check=False, timeout=1800)
        if rc == 1:
            raise RuntimeError("terraform plan failed")
        s["summary"] = "no changes: the routers match the model" if rc == 0 else "Terraform would change the routers (see the log)"

    def do_check(self, s):
        self.sh([LAB / "lab.sh", "nautobot", "render", "--check"])
        s["summary"] = "Nautobot == lab.conf"

    # ---- remove a customer ------------------------------------------------------------------------------------
    def do_rm_validate(self, s):
        problems, plan = C.removal_plan(self.spec["name"])
        if problems:
            raise RuntimeError("; ".join(problems))
        self.removal = plan
        s["summary"] = f"{plan['name']}: {plan['lan']}, {plan['host']}, {plan['provider']} {plan['provider_port']}"

    def do_rm_nac(self, s):
        """The router is about to be deleted, so there is nothing to un-configure on it: Terraform simply forgets
        its resources. Nothing else in the state mentions it — the hubs accepted it on a listen range."""
        import subprocess
        out = subprocess.run([str(LAB / "lab.sh"), "nac", "state", "list"], capture_output=True, text=True).stdout
        mine = [l.strip() for l in out.splitlines() if f'["{self.removal["name"]}/' in l or f'["{self.removal["name"]}"]' in l]
        self.say(f"{len(mine)} resources belong to {self.removal['name']}")
        for i in range(0, len(mine), 40):
            self.sh([LAB / "lab.sh", "nac", "state", "rm", *mine[i:i + 40]])
        s["summary"] = f"{len(mine)} resources forgotten"

    def do_rm_vm(self, s):
        names = [self.removal["name"], self.removal["host"]]
        self.sh([LAB / "lab.sh", "clean", *names])
        import shutil
        for n in names:
            shutil.rmtree(LAB / "nodes" / n, ignore_errors=True)
        s["summary"] = ", ".join(names) + " powered off and deleted"

    def do_rm_labconf(self, s):
        C.remove_from_labconf(self.removal)
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        s["summary"] = "lab.conf and the rendered configuration"

    def do_rm_provider(self, s):
        """The rendered provider config now says the port is unwired; the push reconciles — the old address is
        deleted and the port disabled. There is no route to withdraw by hand: the customer's address was learned
        over eBGP and went with the session."""
        self.sh([LAB / "lab.sh", "configure"])
        s["summary"] = f"{self.removal['provider']} {self.removal['provider_port']} released"

    def do_rm_nautobot(self, s):
        r = self.removal
        self.sh([LAB / "lab.sh", "nautobot", "remove-customer", r["name"], "--host", r["host"], "--wan", r["wan_prefix"],
                 "--lan", r["lan"]])
        s["summary"] = f"{r['name']} and {r['host']} removed from the model"


def resume_factory(rec):
    return Run(rec["mode"], rec.get("spec"), rec.get("options", {}), resume_of=rec)


# ---- API ---------------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(HERE / "static" / "index.html"))


@app.get("/api/state", tags=["state"], summary="The cloud: the model, and what every router in it is doing")
def api_state(refresh: bool = False, live: bool = True):
    return state.get(refresh=refresh, live=live)


@app.get("/api/customers/suggest", tags=["provisioning"], summary="The next customer, every value allocated")
def api_suggest(region: str | None = None):
    try:
        return C.suggest(region=region)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/customers/validate", tags=["provisioning"], summary="Check a customer against the running lab")
def api_validate(spec: CustomerSpec):
    d = spec.model_dump()
    return {"problems": C.validate(d), "plan": C.plan(d)}


@app.get("/api/customers/{name}/removal", tags=["provisioning"], summary="What removing a customer takes away")
def api_removal(name: str):
    problems, plan = C.removal_plan(name)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    return plan


@app.get("/api/query/{node}", tags=["state"], summary="One read-only `show` command on one C8000v")
def api_query(node: str, command: str):
    nodes = C.facts()["nodes"]
    if node not in nodes or nodes[node]["role"] not in ("hub", "spoke"):
        raise HTTPException(404, f"{node} is not a C8000v of this lab")
    if not command.startswith("show ") or any(ch in command for ch in "\n\r"):
        raise HTTPException(400, "only a single `show ...` command is allowed")
    return {"node": node, "command": command, "output": ios(nodes[node]["mgmt_ip"], command)[command]}


@app.post("/api/runs", tags=["runs"], summary="Start a run")
def api_run(req: RunRequest):
    if req.mode not in ("customer", "remove", "deploy", "plan", "test"):
        raise HTTPException(400, f"unknown mode {req.mode}")
    spec = req.customer.model_dump() if req.customer else ({"name": req.name} if req.name else None)
    if req.mode == "customer" and not spec:
        raise HTTPException(400, "mode customer needs a customer spec")
    if req.mode == "remove" and not (spec or {}).get("name"):
        raise HTTPException(400, "mode remove needs a name")
    return registry.start(Run(req.mode, spec, req.options))



# ---- monitoring (Prometheus on the NMS: lab-portal/monitoring) ------------------------------------------------------
LAB_NAME = "c8000v-dmvpn-lab"
LAB_HOST_IP = os.environ.get("LAB_HOST_IP", "10.5.0.1")        # this host on the lab's OOB network, where the NMS reaches us
WEBAPP_PORT = os.environ.get("WEBAPP_PORT", "8094")
_snap = {"snap": None}


def _refresher():
    """Keep the live state warm: collecting it is ~10 s of SSH, far too long for a scrape. Only while the lab runs."""
    while True:
        try:
            if any(st == "running" for st in _vm_states().values()):
                _snap["snap"] = state.get(refresh=True)
            else:
                _snap["snap"] = None
        except Exception:                                       # noqa: BLE001 — never let the refresher die
            pass
        time.sleep(60)


@app.on_event("startup")
def _start_refresher():
    threading.Thread(target=_refresher, daemon=True).start()


def _vm_states():
    """node -> libvirt state, for this lab's VMs (a stopped lab is a normal state, not an error)."""
    f = C.facts()
    try:
        running = set(subprocess.run(["sg", "libvirt", "-c", "virsh -q -c qemu:///system list --name"],
                                     capture_output=True, text=True, timeout=20).stdout.split())
    except Exception:                                           # noqa: BLE001
        running = set()
    return {name: ("running" if n["domain"] in running else "shut off") for name, n in f["nodes"].items()}


def _g(name, help_):
    return [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]


@app.get("/metrics", tags=["monitoring"], summary="Prometheus metrics: VMs, the cloud per router, the provider, hosts, runs",
         response_class=PlainTextResponse)
def prometheus_metrics():
    """IOS-XE has no Prometheus exporter, so the C8000vs are measured here, from the portal's live state (show dmvpn,
    show crypto session, show ip bgp summary, show processes cpu over SSH, refreshed every minute in the background).
    The VyOS provider and the Alpine hosts are scraped directly (see /api/sd); the provider also pushes Telegraf."""
    L = LAB_NAME
    out = metrics_generated(L)
    roles = {n: x["role"] for n, x in C.facts()["nodes"].items()}
    out += _g("lab_vm_running", "1 if the lab VM is running (virsh)")
    out += [metric_line("lab_vm_running", {"lab": L, "node": n, "role": roles[n]}, int(st == "running")) for n, st in _vm_states().items()]
    snap = _snap["snap"]
    if snap:
        out += _g("lab_router_reachable", "1 if the portal could read the router's live state over SSH")
        out += _g("lab_dmvpn_nhs_up", "Hubs a customer is registered with (NHRP NHS up)")
        out += _g("lab_dmvpn_nhs_expected", "Hubs a customer should be registered with")
        out += _g("lab_dmvpn_registrations", "Customers registered with the hub (NHRP dynamic entries up)")
        out += _g("lab_dmvpn_shortcuts", "Customer-to-customer shortcut tunnels NHRP has resolved (phase 3)")
        out += _g("lab_ipsec_sessions_up", "IPsec sessions on Tunnel0 that are UP-ACTIVE")
        out += _g("lab_bgp_overlay_up", "Overlay iBGP sessions Established")
        out += _g("lab_bgp_overlay_sessions", "Overlay iBGP sessions configured or dynamically accepted")
        out += _g("lab_bgp_underlay_up", "eBGP sessions with the provider Established")
        out += _g("lab_router_cpu_pct", "IOS-XE CPU utilisation, one-minute average")
        hubs = snap["hubs"]
        for r, st in (snap.get("cloud") or {}).items():
            lab = {"lab": L, "router": r, "role": st["role"], "region": snap["nodes"][r].get("region") or ""}
            out.append(metric_line("lab_router_reachable", lab, int(not st.get("error"))))
            if st.get("error"):
                continue
            if st["role"] == "hub":
                out.append(metric_line("lab_dmvpn_registrations", lab, len(st.get("registered") or [])))
            else:
                out.append(metric_line("lab_dmvpn_nhs_up", lab, len(st.get("nhs_up") or [])))
                out.append(metric_line("lab_dmvpn_nhs_expected", lab, len(hubs)))
                out.append(metric_line("lab_dmvpn_shortcuts", lab, len(st.get("shortcuts") or [])))
            out.append(metric_line("lab_ipsec_sessions_up", lab, st.get("sa", 0)))
            out.append(metric_line("lab_bgp_overlay_up", lab, st.get("overlay_up", 0)))
            out.append(metric_line("lab_bgp_overlay_sessions", lab, st.get("overlay", 0)))
            out.append(metric_line("lab_bgp_underlay_up", lab, st.get("underlay_up", 0)))
            if st.get("cpu_1m") is not None:
                out.append(metric_line("lab_router_cpu_pct", lab, st["cpu_1m"]))
        out += _g("lab_provider_customers_up", "Sites with an Established eBGP session to the provider")
        out += _g("lab_provider_customers_expected", "Sites the model attaches to the provider")
        for p_, st in (snap.get("provider_state") or {}).items():
            out.append(metric_line("lab_provider_customers_up", {"lab": L, "provider": p_}, st.get("customers_up", 0)))
            out.append(metric_line("lab_provider_customers_expected", {"lab": L, "provider": p_}, len(hubs) + len(snap["customers"])))
        out += _g("lab_host_up", "1 if the LAN host answers SSH")
        out += [metric_line("lab_host_up", {"lab": L, "host": h}, int(ok)) for h, ok in (snap.get("hosts_up") or {}).items()]
        h = snap.get("health") or {}
        out += _g("lab_health_ok", "1 if everything the model expects is true")
        out.append(metric_line("lab_health_ok", {"lab": L}, int(bool(h.get("ok")))))
        out += _g("lab_health_problems", "Problems the portal's health check reports")
        out.append(metric_line("lab_health_problems", {"lab": L}, len(h.get("problems") or [])))
        out += _g("lab_collector_last_refresh_seconds", "When the live state behind these gauges was collected")
        out.append(metric_line("lab_collector_last_refresh_seconds", {"lab": L}, int(snap["generated"])))
    return PlainTextResponse(exposition(out + run_metrics(L, registry.list())), media_type="text/plain; version=0.0.4")


@app.get("/api/sd", tags=["monitoring"], summary="Prometheus HTTP service discovery: the portal, the provider's exporters, the hosts")
def prometheus_sd():
    f = C.facts()
    sd = [{"targets": [f"{LAB_HOST_IP}:{WEBAPP_PORT}"], "labels": {"lab": LAB_NAME, "job": "portal", "role": "portal", "node": "portal"}}]
    for name, n in f["nodes"].items():
        if n["role"] == "provider":
            sd.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {"lab": LAB_NAME, "job": "node", "role": "provider", "node": name, "dc": "provider"}})
            sd.append({"targets": [f"{n['mgmt_ip']}:9342"], "labels": {"lab": LAB_NAME, "job": "frr", "role": "provider", "node": name, "dc": "provider"}})
        elif n["role"] == "host":
            dc = f["nodes"][n["router"]].get("region") or ""
            sd.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {"lab": LAB_NAME, "job": "node", "role": "host", "node": name, "dc": dc}})
    return sd


install_runs_api(app, registry, resume_factory)
app.mount("/results", StaticFiles(directory=str(RESULTS)), name="results")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
