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
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from labportal import RunBase, RunRegistry, exposition, install_runs_api, metric_line, metrics_generated, run_metrics
from pydantic import BaseModel, Field

import customers as C
import auth as A
import backup as B
import changes as CH
import confighist as CFG
import ikeauth as IKE
import cportal as CP
import maint as M
import chaos as X
import drift as D
import sla as SLA
import capacity as CAP
from state import HOST_PASS, HOST_USER, State, _ssh, ios, vtysh, vyos_op, vyos_show

LAB = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB / "tools"))
import labsecrets as SEC  # noqa: E402
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
    "rm_vm": "Power off and delete the router and its host",
    "rm_labconf": "Remove from lab.conf and re-render",
    "rm_provider": "Release the provider port (address removed, port disabled)",
    "rm_nautobot": "Remove the customer from Nautobot",
    "mod_validate": "Validate the changes against the running lab",
    "mod_labconf": "Record the changes in lab.conf and customers.json, and re-render",
    "mod_forget": "Terraform forgets the old router (its resources leave the state; the hubs are not touched)",
    "mod_router": "Rebuild the router: delete the old VM, boot the new one with the same identity, day-0",
    "mod_host": "Rebuild the LAN host on the new LAN",
    "mod_rewire": "Re-wire the router's port 4 (the second provider): save, power off, redefine, boot",
    "mod_nautobot": "Nautobot source of truth: update the customer",
    "drift": "Compare every router's running configuration with the model (Nautobot, Network-as-Code, the renders)",
    "fixcli": "The CLI templates: undo lines the model does not have, and mark drifted templates to be written again",
    "rs_validate": "Validate the backup and plan the restore against the running lab",
    "rs_remove": "Take away what the backup does not have (customers, routers to rebuild, hosts to re-address)",
    "rs_files": "Put the backup's intent back (lab.conf, customers.json, applications.json) and re-render",
    "rs_build": "Build what the backup has and the lab does not: boot the VMs, day-0",
    "rs_nautobot": "Nautobot source of truth: seed from the restored intent",
    "fo_prepare": "Check the lab is healthy; every host starts pinging every other host and every hub's LAN (5 per second)",
    "fo_inject": "Put the fault in",
    "fo_hold": "Hold the fault, watching the control plane",
    "fo_restore": "Take the fault out",
    "fo_recover": "Wait for the lab to be healthy again",
    "fo_report": "Collect every flow's replies and measure the outages",
    "cfg_before": "Snapshot every router's configuration (before)",
    "cfg_after": "Snapshot every router's configuration (after) and compare",
    "sec_newkey": "Choose a new random pre-shared key (kept in secrets/, never in git or the log)",
    "sec_reauth": "Re-authenticate every IKE session, one router at a time",
    "sec_verify": "Verify every IKE session: it authenticated with the pre-shared key, and after the rotation",
    "render": "Render the configuration from lab.conf",
    "plan": "terraform plan: what Network-as-Code would change",
    "check": "Compare Nautobot's rendering with lab.conf's",
}
TAGS = [{"name": "monitoring", "description": "Prometheus: /metrics and /api/sd, scraped by the NMS."},
        {"name": "state", "description": "The cloud, the model and the live state of every router."},
        {"name": "provisioning", "description": "Suggest, validate and plan a customer; plan a removal."},
        {"name": "runs", "description": "Pipeline runs: add, modify or remove a customer, deploy, plan, test, drift, restore."},
        {"name": "backup", "description": "Back up the whole lab state as one download; upload one to restore it."},
        {"name": "resilience", "description": "Simulated failures, and failover measured flow by flow."},
        {"name": "change control", "description": "Change requests, four-eyes approval, change windows."},
        {"name": "customer portal", "description": "A customer's own view: a secret link (read-only), or a customer account (also self-service)."},
        {"name": "auth", "description": "Sign in, accounts and roles."}]
app = FastAPI(title="c8000v-dmvpn-lab Provisioning Portal API", version="1.0", openapi_tags=TAGS,
              docs_url="/docs", redoc_url="/redoc",
              description="REST API behind the C8000v DMVPN lab's portal. Every change goes **lab.conf → rendered "
                          "configuration → VMs → Network-as-Code → provider → Nautobot → verification → tests**; runs "
                          "are asynchronous (`POST /api/runs`, poll `GET /api/runs/{id}`). UI: [/](/)")
registry = RunRegistry(RUNS_DIR)
state = State()


# ---- logins and roles (auth.py) ---------------------------------------------------------------------------------------
OPEN = {"/", "/api/version", "/api/sd", "/metrics", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/docs", "/redoc",
        "/openapi.json", "/favicon.ico"}
OPEN_PREFIX = ("/static/", "/c/", "/api/c/", "/docs/")


def _local(request):
    """The lab host itself (the lab hub polls GET /api/runs from here)."""
    ip = request.client.host if request.client else ""
    return ip in ("127.0.0.1", "::1", os.environ.get("LAB_HOST_IP", "10.5.0.1"), os.environ.get("LAB_PUBLIC_HOST", "192.168.50.231"))


def _need(method, path):
    """The roles a request needs (any of them), or None for any signed-in staff account."""
    if path.startswith("/api/auth/users"):
        return ("admin",)
    if method in ("GET", "HEAD"):
        return None
    if path == "/api/auth/password":
        return None
    if path in ("/api/policy", "/api/capacity/policy"):
        return ("admin",)
    if re.fullmatch(r"/api/changes/[^/]+/(approve|reject)", path):
        return ("approver",)
    if re.fullmatch(r"/api/changes/[^/]+/cancel", path):
        return ("operator", "approver")
    if path == "/api/customers/validate" or re.fullmatch(r"/api/customers/[^/]+/modify/validate", path):
        return ("viewer", "operator", "approver")
    return ("operator",)


@app.middleware("http")
async def _auth(request: Request, call_next):
    path, method = request.url.path, request.method
    user = A.from_cookie(request.cookies.get(A.COOKIE))
    request.state.user = user
    if path in OPEN or path.startswith(OPEN_PREFIX):
        return await call_next(request)
    if method == "GET" and path == "/api/runs" and user is None and _local(request):
        return await call_next(request)
    if not (path.startswith("/api/") or path.startswith("/results/")):
        return await call_next(request)
    if user is None:
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    if "customer" in user["roles"]:
        return JSONResponse({"detail": "a customer account sees its own service only (/api/c/me/…)"}, status_code=403)
    need = _need(method, path)
    if need is not None and not A.has(user, *need):
        return JSONResponse({"detail": f"{user['username']} may not do this: it needs the {' or '.join(need)} role"}, status_code=403)
    return await call_next(request)


def _user(request) -> dict:
    return getattr(request.state, "user", None) or {}


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login", tags=["auth"], summary="Sign in (sets the session cookie)")
def api_login(body: Login, response: Response):
    u = A.authenticate(body.username.strip().lower(), body.password)
    if u is None:
        time.sleep(1)                                   # every failed guess costs a second
        raise HTTPException(401, "wrong username or password")
    response.set_cookie(A.COOKIE, A.make_cookie(body.username.strip().lower(), u), httponly=True, samesite="lax",
                        max_age=A.SESSION_H * 3600)
    return A.public(body.username.strip().lower(), u)


@app.post("/api/auth/logout", tags=["auth"], summary="Sign out")
def api_logout(response: Response):
    response.delete_cookie(A.COOKIE)
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"], summary="Who is signed in (401 if nobody)")
def api_me(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(401, "not signed in")
    return {**u, "defaults": A.seeded_defaults() if A.has(u, "admin") else []}


class PasswordChange(BaseModel):
    old: str
    new: str


@app.post("/api/auth/password", tags=["auth"], summary="Change your own password")
def api_password(body: PasswordChange, request: Request, response: Response):
    u = _user(request)
    if not u or u["username"] == A.RENDERER:
        raise HTTPException(401, "not signed in")
    if A.authenticate(u["username"], body.old) is None:
        raise HTTPException(403, "the current password is wrong")
    try:
        A.update(u["username"], password=body.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    response.set_cookie(A.COOKIE, A.make_cookie(u["username"], A.users()[u["username"]]), httponly=True, samesite="lax",
                        max_age=A.SESSION_H * 3600)
    return {"ok": True}


class NewUser(BaseModel):
    username: str
    name: str = ""
    roles: list[str]
    password: str
    customer: str | None = None


class UserPatch(BaseModel):
    name: str | None = None
    roles: list[str] | None = None
    password: str | None = None
    disabled: bool | None = None


@app.get("/api/auth/users", tags=["auth"], summary="Every account (admin)")
def api_users():
    return [A.public(n, u) for n, u in sorted(A.users().items())]


@app.post("/api/auth/users", tags=["auth"], summary="Make an account (admin)")
def api_user_create(body: NewUser):
    if body.customer and C.current(body.customer) is None:
        raise HTTPException(400, f"{body.customer} is not a customer of this lab")
    try:
        return A.create(body.username.strip().lower(), body.name, body.roles, body.password, body.customer)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/auth/users/{username}", tags=["auth"], summary="Change an account (admin)")
def api_user_update(username: str, body: UserPatch):
    try:
        return A.update(username, **body.model_dump())
    except KeyError:
        raise HTTPException(404, f"no account {username}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/auth/users/{username}", tags=["auth"], summary="Delete an account (admin)")
def api_user_delete(username: str, request: Request):
    if username == _user(request).get("username"):
        raise HTTPException(400, "you cannot delete yourself")
    try:
        A.delete(username)
    except KeyError:
        raise HTTPException(404, f"no account {username}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


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
    customer: dict = Field(default_factory=dict, description="the company: company, industry, address, phone, contact, email, account")
    platform: str = Field("c8000v", description="the customer router: c8000v (Catalyst 8000v) or vyos")
    prefer_hub: str | None = Field(None, description="the hub its traffic prefers (BGP local-preference 200), or none")
    dual_homed: bool = Field(False, description="a second link, into the second provider (port 4), and a Tunnel1 in the second cloud")
    provider2: str | None = None
    provider2_port: str | None = None
    wan2_prefix: str | None = None


class CustomerChange(BaseModel):
    """The new values for an existing customer; anything left out stays as it is."""
    region: str | None = None
    lan: str | None = Field(None, examples=["192.168.81.0/24"])
    platform: str | None = Field(None, description="c8000v or vyos: a different one rebuilds the router")
    prefer_hub: str | None = Field(None, description="a hub's name, or empty for none")
    dual_homed: bool | None = Field(None, description="true: add a link into the second provider; false: take it away")
    customer: dict | None = Field(None, description="company fields and/or `applications`")


class RunRequest(BaseModel):
    mode: str = Field(examples=["customer"], description="customer | remove | modify | deploy | plan | test | drift | fixdrift | restore")
    customer: CustomerSpec | None = None
    name: str | None = Field(None, description="remove / modify: the customer; restore: the uploaded backup's id")
    changes: CustomerChange | None = Field(None, description="modify: the new values")
    fault: dict | None = Field(None, description="failover: {kind, target, hold (seconds)} — see GET /api/faults")
    requested_by: str | None = Field(None, description="who asks for it (change control: an approver must be someone else)")
    reason: str | None = Field(None, description="why (shown on the change request)")
    options: dict = Field(default_factory=dict, description="{test: bool (default true), suites: [..]}")


class Run(RunBase):
    LAB = "c8000v-dmvpn-lab"
    STEP_TITLES = STEP_TITLES
    EXTRA = {"customer": "customer", "spec": "spec", "removal": "removal", "modification": "modification", "drift": "drift",
             "restore": "restore", "change": "change", "failover": "failover", "started_by": "started_by",
             "config_changes": "config_changes", "security": "security"}
    CHANGING = ("customer", "remove", "modify", "deploy", "fixdrift", "restore", "rotatepsk")   # jobs that change routers
    MAINTENANCE = ("remove", "modify", "deploy", "fixdrift", "restore", "failover", "rotatepsk")   # jobs that disturb the service

    def execute(self):
        """A disruptive job runs under a maintenance record: its routers' alerts are muted, its customers are told."""
        self.maint_id = None
        if self.mode in self.MAINTENANCE:
            try:
                routers, custs = _job_scope(self)
                self.maint_id = M.open_(C.facts(), "job", self.id, f"{self.mode} {self.customer or ''}".strip()
                                        + (f" ({self.change})" if self.change else ""), routers, custs, self.started_by or "?", self.change)["id"]
            except Exception:                      # noqa: BLE001 — a record that cannot open must not stop the work
                pass
        super().execute()

    # the steps that read differently for a VyOS router (the defaults above describe a Catalyst 8000v)
    VYOS_TITLES = {
        "vm": "Create and boot the VyOS router and its LAN host",
        "bootstrap": "Day-0 over the serial console (the whole VyOS configuration, the IKE hook), wait for SSH",
        "nac": "Network-as-Code: terraform apply (nothing for a VyOS router; the C8000vs are re-asserted)",
        "rm_nac": "Terraform forgets the router (a VyOS router was never in its state)",
        "rm_vm": "Power off and delete the VyOS router and its host",
    }

    def __init__(self, mode, spec, options, resume_of=None):
        self.spec = spec
        self.removal = (resume_of or {}).get("removal")
        self.customer = (spec or {}).get("name")
        self.modification = (resume_of or {}).get("modification")
        self.drift = None
        self.change = (resume_of or {}).get("change")        # the change request that started it, if any
        self.started_by = (resume_of or {}).get("started_by")
        self.security = (resume_of or {}).get("security")
        self.config_changes = (resume_of or {}).get("config_changes")
        self.failover = (resume_of or {}).get("failover")
        self.restore = (resume_of or {}).get("restore")
        if mode == "restore" and not self.restore:
            man, files = B.load_upload(spec["name"])
            problems, rplan = B.plan(C.facts()["inv"], files)
            if problems:
                raise ValueError("; ".join(problems))
            self.restore = {"upload": spec["name"], "manifest": {k: man[k] for k in ("lab", "version", "created", "created_iso", "customers")},
                            "plan": rplan}
        if mode == "restore":
            self.customer = f"backup of {self.restore['manifest']['created_iso'][:16].replace('T', ' ')}"
        if mode == "modify" and not self.modification:
            problems, self.modification = C.modify_plan(spec["name"], spec.get("changes") or {})
            if problems:
                raise ValueError("; ".join(problems))
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)
        self.retitle()

    def platform(self):
        if self.mode == "customer":
            return (self.spec or {}).get("platform", "c8000v")
        if self.mode == "remove":
            n = C.facts()["nodes"].get((self.spec or {}).get("name") or "")
            return (self.removal or {}).get("platform") or (n or {}).get("platform")
        if self.mode == "modify":
            return self.modification["new"]["platform"]
        return None

    def retitle(self):
        """Step titles name the router the run is building or removing."""
        plat = self.platform()
        for st in self.steps:
            if plat == "vyos" and st["name"] in self.VYOS_TITLES:
                st["title"] = self.VYOS_TITLES[st["name"]]
            elif plat == "c8000v" and st["name"] == "rm_vm":
                st["title"] = "Power off and delete the C8000v and its host"

    def plan(self):
        steps = self._plan()
        if self.mode in self.CHANGING:              # the configuration before, and after (before the tests)
            i = steps.index("test") if "test" in steps else len(steps)
            steps = ["cfg_before"] + steps[:i] + ["cfg_after"] + steps[i:]
        return steps

    def _plan(self):
        if self.mode == "test":
            return ["test"]
        if self.mode == "plan":
            return ["plan", "check"]
        if self.mode == "drift":
            return ["drift"]
        if self.mode == "rotatepsk":
            return ["sec_newkey", "render", "nac", "provider", "sec_reauth", "sec_verify", "verify"]
        if self.mode == "failover":
            return ["fo_prepare", "fo_inject", "fo_hold", "fo_restore", "fo_recover", "fo_report"]
        if self.mode == "restore":
            rp = self.restore["plan"]
            takes = rp["removed"] or any(c["rebuild"] or c["relan"] for c in rp["changed"])
            builds = rp["added"] or any(c["rebuild"] or c["relan"] for c in rp["changed"])
            steps = ["rs_validate"] + (["rs_remove"] if takes else []) + ["rs_files"] + (["rs_build"] if builds else []) \
                + ["nac", "provider", "rs_nautobot", "verify"]
            return steps + (["test"] if self.options.get("test", True) else [])
        if self.mode == "fixdrift":
            return ["render", "fixcli", "nac", "provider", "nautobot", "check", "drift"]
        if self.mode == "deploy":
            steps = ["render", "nac", "provider", "nautobot", "check"]
        elif self.mode == "remove":
            steps = ["rm_validate", "rm_nac", "rm_vm", "rm_labconf", "nac", "rm_provider", "rm_nautobot", "nautobot", "verify"]
        elif self.mode == "modify":
            m = self.modification
            steps = ["mod_validate", "mod_labconf"]
            if m["rebuild"]:
                steps += (["mod_forget"] if m["old"]["platform"] == "c8000v" else []) + ["mod_router"]
            if m["relan"]:
                steps.append("mod_host")
            if m.get("rewire"):
                steps.append("mod_rewire")
            if m["routers"]:
                steps += ["nac", "provider"]
            steps += ["mod_nautobot", "verify"]
        else:
            steps = ["validate", "labconf", "vm", "bootstrap", "nac", "provider", "nautobot", "verify"]
        if self.options.get("test", self.mode != "plan"):
            steps.append("test")
        return steps

    def do_cfg_before(self, s):
        errs = CFG.snapshot(C.facts()["inv"], self.id, "before")
        s["summary"] = "every router saved" + (f"; not read: {', '.join(errs)}" if errs else "")

    def do_cfg_after(self, s):
        errs = CFG.snapshot(C.facts()["inv"], self.id, "after")
        d = CFG.compare(self.id)
        self.config_changes = {r: {k: v[k] for k in ("added", "removed", "new", "gone")} for r, v in d.items()}
        s["summary"] = ("; ".join(f"{r} +{v['added']}/−{v['removed']}" for r, v in d.items()) or "no router's configuration changed") + (
            f"; not read: {', '.join(errs)}" if errs else "")

    def after(self):
        state._cache.clear()
        if getattr(self, "maint_id", None):
            M.close(mid=self.maint_id, reason=f"job {self.status}")
        if self.mode in self.CHANGING and self.config_changes is None:     # failed before cfg_after: compare what is there
            try:
                if any(st["name"] == "cfg_before" and st["status"] == "success" for st in self.steps):
                    CFG.snapshot(C.facts()["inv"], self.id, "after")
                    d = CFG.compare(self.id)
                    self.config_changes = {r: {k: v[k] for k in ("added", "removed", "new", "gone")} for r, v in d.items()}
                    self.persist()
            except Exception:                      # noqa: BLE001
                pass
        fo = self.failover or {}
        if self.mode == "failover" and fo.get("fault_id") and any(x["id"] == fo["fault_id"] for x in X.active()):
            try:                                   # a failed or interrupted experiment never leaves its fault in
                X.restore(C.facts(), fo["fault_id"], reason=f"run {self.id} ended ({self.status})")
            except Exception:                      # noqa: BLE001
                pass
        if self.change:
            try:
                CH.mark(self.change, "done" if self.status == "success" else "failed", f"run {self.id} {self.status}")
            except Exception:                      # noqa: BLE001
                pass

    # ---- add a customer ---------------------------------------------------------------------------------------
    def do_validate(self, s):
        problems = C.validate(self.spec)
        if problems:
            raise RuntimeError("; ".join(problems))
        s["summary"] = (f"{self.spec['name']} for {self.spec['customer']['company']}: {self.spec['lan']} behind {self.spec['host']}, "
                        f"{self.spec['wan_prefix']} on {self.spec['provider']} {self.spec['provider_port']}")

    def do_labconf(self, s):
        C.apply_to_labconf(self.spec)
        self.say(f"lab.conf: {self.spec['name']} ({self.spec['mgmt_ip']}, index {self.spec['t_idx']}) and {self.spec['host']}")
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        s["summary"] = (f"{self.spec['name']} ({self.spec['customer']['company']}) and {self.spec['host']} registered; "
                        "day-0, provider and NAC model re-rendered")

    def do_vm(self, s):
        self.say("the provider keeps its definition: every one of its ports already exists in the VM, wired or not")
        self.sh([LAB / "lab.sh", "up", self.spec["name"], self.spec["host"]])
        s["summary"] = f"{self.spec['name']} and {self.spec['host']} started"

    def do_bootstrap(self, s):
        self.sh([LAB / "lab.sh", "bootstrap", self.spec["name"], self.spec["host"]], timeout=3600)
        s["summary"] = ("day-0 applied, crypto licence active, RESTCONF answering" if self.spec.get("platform", "c8000v") == "c8000v"
                        else "VyOS configured over the serial console (tunnel, NHRP, IPsec, BGP), SSH answering")

    def do_nac(self, s):
        # after a removal this changes no router — the departed one is already out of the state — but it rewrites
        # Terraform's local copy of the model, which would otherwise read as drift in the next plan
        self.sh([LAB / "lab.sh", "nac", "init", "-input=false", "-no-color"])
        replace = [f"-replace={a}" for a in getattr(self, "_replace", [])]    # fix drift: templates Terraform cannot see
        self.sh([LAB / "lab.sh", "nac", "apply", "-auto-approve", "-parallelism=1", "-input=false", "-no-color", *replace],
                timeout=3600)
        s["summary"] = ("applied and saved" if (self.spec or {}).get("platform", "c8000v") == "c8000v" or self.mode != "customer"
                        else "applied and saved (the new router is VyOS: Network-as-Code has nothing to add for it)")

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

    # ---- modify a customer ------------------------------------------------------------------------------------
    def do_mod_validate(self, s):
        m = self.modification
        problems, again = C.modify_plan(m["name"], {**{k: m["new"][k] for k in C.MODIFIABLE}, "customer": m["new"]["customer"]})
        if problems:
            raise RuntimeError("; ".join(problems))
        if again["changes"] != m["changes"]:
            raise RuntimeError("the customer changed since the run was planned — start it again")
        s["summary"] = f"{m['name']}: " + "; ".join(f"{c['what']} {c['old']} → {c['new']}" for c in m["changes"])[:400]

    def do_mod_labconf(self, s):
        m = self.modification
        C.apply_modify(m)
        self.say(f"lab.conf / customers.json: {m['name']} — " + ", ".join(c["what"] for c in m["changes"]))
        if m["rebuild"]:               # the old platform's rendered day-0 would otherwise linger next to the new one
            stale = LAB / "nodes" / m["name"] / ("iosxe_config.txt" if m["old"]["platform"] == "c8000v" else "vyos_config.txt")
            stale.unlink(missing_ok=True)
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        s["summary"] = "recorded and re-rendered"

    def do_mod_forget(self, s):
        name = self.modification["name"]
        out = subprocess.run([str(LAB / "lab.sh"), "nac", "state", "list"], capture_output=True, text=True).stdout
        mine = [l.strip() for l in out.splitlines() if f'["{name}/' in l or f'["{name}"]' in l]
        self.say(f"{len(mine)} resources belong to {name}")
        for i in range(0, len(mine), 40):
            self.sh([LAB / "lab.sh", "nac", "state", "rm", *mine[i:i + 40]])
        s["summary"] = f"{len(mine)} resources forgotten"

    def do_mod_router(self, s):
        m = self.modification
        name = m["name"]
        self.sh([LAB / "lab.sh", "clean", name])
        self.sh([LAB / "lab.sh", "up", name])
        self.sh([LAB / "lab.sh", "bootstrap", name], timeout=3600)
        s["summary"] = f"{name} rebuilt as {C.PLATFORMS[m['new']['platform']]}, day-0 applied"

    def do_mod_host(self, s):
        host = self.modification["old"]["host"]
        self.sh([LAB / "lab.sh", "clean", host])
        self.sh([LAB / "lab.sh", "up", host])
        self.sh([LAB / "lab.sh", "wait", host], timeout=1800)
        s["summary"] = f"{host} rebuilt on {self.modification['new']['lan']}"

    def do_mod_rewire(self, s):
        """The router is the second end of its link into the second provider (the provider anchors it), so wiring or
        unwiring port 4 changes the router's own VM definition: its configuration is saved, it is powered off and
        redefined from lab.conf, and it boots with the configuration it had."""
        name = self.modification["name"]
        self.sh([LAB / "lab.sh", "down", name])
        self.sh([LAB / "lab.sh", "rebuild", name])
        self.sh([LAB / "lab.sh", "up", name])
        self.sh([LAB / "lab.sh", "wait", name], timeout=1800)
        s["summary"] = f"{name} restarted with port 4 " + ("wired into " + self.modification["new"]["provider2"]
                                                            if self.modification["new"]["dual_homed"] else "unwired")

    def do_mod_nautobot(self, s):
        m, o = self.modification, self.modification["old"]
        if m["rebuild"] or m["relan"] or m.get("rewire"):
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", o["name"], "--host", o["host"], *_wans(o), "--lan", o["lan"]])
        self.sh([LAB / "lab.sh", "nautobot", "seed"])
        s["summary"] = "re-modelled and seeded" if m["rebuild"] or m["relan"] or m.get("rewire") else "seeded"

    # ---- IKE: rotating the pre-shared key --------------------------------------------------------------------------
    def do_sec_newkey(self, s):
        old = SEC.psk_info()
        new = SEC.set_psk(SEC.new_psk())
        self.security = {"rotated_at": new["since"], "old": old["fingerprint"], "new": new["fingerprint"]}
        self.say(f"a new pre-shared key: fingerprint …{new['fingerprint']} (was …{old['fingerprint']}, {old['source']})")
        s["summary"] = f"new key …{new['fingerprint']} (the key itself is in secrets/dmvpn_psk only)"

    def do_sec_reauth(self, s):
        IKE.reauth(C.facts()["inv"], state, self.say)
        s["summary"] = "every router's IKE sessions set up again"

    def do_sec_verify(self, s):
        want = "psk"
        young = time.time() - self.security["rotated_at"]             # every SA must be younger than the new key
        rep = IKE.verify(C.facts()["inv"], want=want, younger_than=young)
        bad = {r: v for r, v in rep.items() if v.get("error") or v["wrong"] or v["old"] or not v["sas"]}
        for r, v in sorted(rep.items()):
            self.say(f"{r}: {v['sas']} IKE SAs {v.get('by_auth', {})}" + (f" — {len(v['wrong'])} not {want}" if v["wrong"] else "")
                     + (f" — {len(v['old'])} older than the key" if v["old"] else "") + (f" — {v['error']}" if v.get("error") else ""))
        self.security = {**(self.security or {}), "verified": {r: {k: v.get(k) for k in ("sas", "by_auth")} for r, v in rep.items()}}
        if bad:
            raise RuntimeError("not every IKE SA is as it should be: " + ", ".join(sorted(bad)))
        s["summary"] = f"{sum(v['sas'] for v in rep.values())} IKE SAs on {len(rep)} routers, all authenticated with the new pre-shared key"

    # ---- failover timing ---------------------------------------------------------------------------------------
    RECOVER_S = 240

    def do_fo_prepare(self, s):
        fl = self.spec["fault"]
        snap = state.get(refresh=True)
        if not snap["health"]["ok"]:
            raise RuntimeError("the lab is not healthy before the test — the numbers would mean nothing: "
                               + "; ".join(snap["health"]["problems"][:4]))
        if X.active():
            raise RuntimeError("a simulated failure is already in: restore it first")
        f = C.facts()
        tag = self.id[-4:] + self.id[11:19].replace("-", "")
        starts = X.start_pings(f, tag, fl["hold"] + self.RECOVER_S + 60)
        self.failover = {"kind": fl["kind"], "title": X.KINDS[fl["kind"]]["title"], "target": fl["target"], "hold": fl["hold"],
                         "tag": tag, "starts": starts, "flows": len(X.flows(f)), "timeline": []}
        time.sleep(10)                              # a baseline before the fault
        s["summary"] = f"{len(X.flows(f))} flows pinging from {len(starts)} hosts"

    def _mark(self, what):
        self.failover["timeline"].append({"t": time.time(), "what": what})
        self.say(what)

    def do_fo_inject(self, s):
        fo = self.failover
        item = X.apply(C.facts(), fo["kind"], fo["target"], by="failover run", note=self.id)
        fo["fault_id"], fo["t_inject"] = item["id"], time.time()
        state._cache.clear()
        self._mark(f"fault in: {item['done']}")
        s["summary"] = item["done"]

    def do_fo_hold(self, s):
        fo = self.failover
        end = fo["t_inject"] + fo["hold"]
        while time.time() < end:
            snap = state.get(refresh=True)
            probs = snap["health"]["problems"]
            self._mark(f"+{time.time() - fo['t_inject']:.0f}s: {len(probs)} problem(s)" + (": " + "; ".join(probs[:3]) if probs else ""))
            time.sleep(max(0, min(10, end - time.time())))
        s["summary"] = f"held {fo['hold']} s"

    def do_fo_restore(self, s):
        fo = self.failover
        rec = X.restore(C.facts(), fo["fault_id"], reason=f"failover run {self.id}")
        fo["t_restore"] = time.time()
        state._cache.clear()
        self._mark(f"fault out: {rec['undone']}")
        s["summary"] = rec["undone"]

    def do_fo_recover(self, s):
        fo = self.failover
        deadline = time.time() + self.RECOVER_S
        while True:
            snap = state.get(refresh=True)
            if snap["health"]["ok"]:
                fo["t_healthy"] = time.time()
                break
            if time.time() > deadline:
                raise RuntimeError("not healthy again after 4 minutes: " + "; ".join(snap["health"]["problems"][:4]))
            self._mark(f"recovering: " + "; ".join(snap["health"]["problems"][:3]))
            time.sleep(10)
        self._mark(f"healthy again {fo['t_healthy'] - fo['t_restore']:.0f} s after the fault came out")
        time.sleep(10)                              # the flows' tail
        s["summary"] = f"healthy {fo['t_healthy'] - fo['t_restore']:.0f} s after the restore"

    def do_fo_report(self, s):
        fo = self.failover
        f = C.facts()
        t_end = time.time()
        seqs = X.collect(f, fo["tag"])
        res = X.analyse(f, seqs, fo["starts"], fo["t_inject"], fo["t_restore"], t_end)
        summ = X.summary(res)
        fo["summary"] = summ
        rep = {"run": self.id, "kind": fo["kind"], "title": fo["title"], "target": fo["target"], "hold": fo["hold"],
               "t_inject": fo["t_inject"], "t_restore": fo["t_restore"], "t_healthy": fo.get("t_healthy"),
               "recovery_s": round(fo["t_healthy"] - fo["t_restore"], 1) if fo.get("t_healthy") else None,
               "expect": X.KINDS[fo["kind"]]["expect"], "summary": summ, "timeline": fo["timeline"], "flows": res,
               "dual_homed": [c for c in f["customers"] if f["nodes"][c].get("wan2")]}
        X.save_result(rep)
        v = summ["by_verdict"]
        for r in res:
            if r["verdict"] != "unaffected":
                self.say(f"  {r['src']:>11} → {r['dst']:<11} {r['verdict']:<15} worst {r.get('worst', 0):5.1f} s"
                         + (f", on restore {r['worst_restore']:.1f} s" if r.get("worst_restore") else ""))
        s["summary"] = (f"{v.get('unaffected', 0)} unaffected, {v.get('failed over', 0)} failed over"
                        + (f" (worst {summ['failover_worst']:.1f} s)" if summ["failover_worst"] is not None else "")
                        + f", {v.get('cut off', 0)} cut off, {v.get('hit on restore', 0)} hit on restore; of {summ['flows']} flows")

    # ---- configuration drift -----------------------------------------------------------------------------------
    def do_fixcli(self, s):
        """Terraform writes a CLI template but never reads it back, so an apply does not repair one. From the latest
        drift report: a line the router has under Tunnel0 and the model does not is removed (`no <line>`), and every
        template with a line missing is written again (`terraform apply -replace` of that template's resource)."""
        rep = D.latest() or {"nodes": {}}
        nodes = C.facts()["nodes"]
        self._replace, undone = [], []
        for name, r in sorted(rep["nodes"].items()):
            if r.get("platform") != "c8000v" or name not in nodes:
                continue
            tpl = {i["where"] for i in r["items"] if i.get("where", "").startswith(("tunnel0_", "tunnel1_", "bgp_hub_", "vips_"))}
            self._replace += [f'module.iosxe.iosxe_cli.cli_0["{name}/{t}"]' for t in sorted(tpl)]
            for pre, itf in (("tunnel0_", "Tunnel0"), ("tunnel1_", "Tunnel1"), ("vips_", "Loopback10")):
                extra = [i["line"] for i in r["items"] if i["kind"] == "extra" and i.get("where", "").startswith(pre)]
                if extra:
                    from netmiko import ConnectHandler
                    c = ConnectHandler(device_type="cisco_xe", host=nodes[name]["mgmt_ip"], username="admin", password="admin", fast_cli=False)
                    try:
                        self.say(c.send_config_set([f"interface {itf}", *[f"no {l}" for l in extra]]))
                    finally:
                        c.disconnect()
                    undone.append(f"{name} {itf}: {len(extra)} line(s) removed")
        for a in self._replace:
            self.say(f"will write again: {a}")
        s["summary"] = "; ".join(undone + [f"{len(self._replace)} template(s) to write again"]) if (undone or self._replace) else "no template drift"

    def do_drift(self, s):
        rep = D.check(C.facts()["inv"], say=self.say)
        for name, r in sorted(rep["nodes"].items()):
            if r["status"] != "ok":
                self.say(f"{name}: {r['status']}" + (f" — {r['error']}" if r["error"] else ""))
                for it in r["items"][:20]:
                    self.say(f"   {it['kind']:8s} {it.get('where', '') + ': ' if it.get('where') else ''}{it['line']}")
        for e in rep["errors"] + ([rep["model"]["error"]] if rep["model"]["error"] else []):
            self.say("!! " + e)
        self.drift = {"ok": rep["ok"], "drifted": rep["drifted"], "generated": rep["generated"]}
        s["summary"] = ("no drift: every router runs what the model says" if rep["ok"] else
                        (f"drift on {', '.join(rep['drifted'])}" if rep["drifted"] else "the check had errors (see the log)"))
        if self.mode == "fixdrift" and not rep["ok"]:
            raise RuntimeError("drift remains after the fix: " + s["summary"])

    # ---- restore a backup -------------------------------------------------------------------------------------
    def do_rs_validate(self, s):
        man, files = B.load_upload(self.restore["upload"])
        problems, again = B.plan(C.facts()["inv"], files)
        if problems:
            raise RuntimeError("; ".join(problems))
        rp = self.restore["plan"]
        if (again["removed"], again["added"], [c["name"] for c in again["changed"]]) != (rp["removed"], rp["added"], [c["name"] for c in rp["changed"]]):
            raise RuntimeError("the lab changed since the restore was planned — upload the backup again")
        s["summary"] = (f"backup of {man['created_iso']} (v{man['version']}): remove {', '.join(rp['removed']) or 'nothing'}, "
                        f"add {', '.join(rp['added']) or 'nothing'}, change {', '.join(c['name'] for c in rp['changed']) or 'nothing'}")

    def _forget(self, name):
        out = subprocess.run([str(LAB / "lab.sh"), "nac", "state", "list"], capture_output=True, text=True).stdout
        mine = [l.strip() for l in out.splitlines() if f'["{name}/' in l or f'["{name}"]' in l]
        for i in range(0, len(mine), 40):
            self.sh([LAB / "lab.sh", "nac", "state", "rm", *mine[i:i + 40]])
        return len(mine)

    def do_rs_remove(self, s):
        import shutil
        rp, done = self.restore["plan"], []
        for c in rp["removed"]:
            cur = C.current(c)
            if cur["platform"] == "c8000v":
                self.say(f"{c}: {self._forget(c)} Terraform resources forgotten")
            self.sh([LAB / "lab.sh", "clean", c, cur["host"]])
            for n in (c, cur["host"]):
                shutil.rmtree(LAB / "nodes" / n, ignore_errors=True)
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", c, "--host", cur["host"], *_wans(cur), "--lan", cur["lan"]])
            done.append(f"{c} removed")
        for ch in rp["changed"]:
            if not (ch["rebuild"] or ch["relan"]):
                continue
            c, cur = ch["name"], C.current(ch["name"])
            if ch["rebuild"]:
                if cur["platform"] == "c8000v":
                    self.say(f"{c}: {self._forget(c)} Terraform resources forgotten")
                self.sh([LAB / "lab.sh", "clean", c])
                for f in ("iosxe_config.txt", "vyos_config.txt"):
                    (LAB / "nodes" / c / f).unlink(missing_ok=True)
            if ch["relan"]:
                self.sh([LAB / "lab.sh", "clean", cur["host"]])
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", c, "--host", cur["host"], *_wans(cur), "--lan", cur["lan"]])
            done.append(f"{c}: " + " and ".join(x for x, y in (("router taken down", ch["rebuild"]), ("host taken down", ch["relan"])) if y))
        s["summary"] = "; ".join(done) or "nothing"

    def do_rs_files(self, s):
        man, files = B.load_upload(self.restore["upload"])
        for f in B.INTENT:
            if f"intent/{f}" in files:
                (LAB / f).write_bytes(files[f"intent/{f}"])
                self.say(f"restored {f}")
        self.sh(["python3", LAB / "tools" / "gen_configs.py"])
        differ = [k for k, v in files.items() if k.startswith("renders/") and (LAB / k[8:]).exists() and (LAB / k[8:]).read_bytes() != v]
        for k in differ:
            self.say(f"note: {k[8:]} renders differently from the backup's copy (the renderer changed since v{man['version']})")
        s["summary"] = "intent restored and re-rendered" + (f"; {len(differ)} file(s) render differently from the backup's copy" if differ else "; the renders match the backup's")

    def do_rs_build(self, s):
        rp = self.restore["plan"]
        f = C.facts()
        nodes = []
        for c in rp["added"]:
            nodes += [c, f["nodes"][c]["host"]]
        for ch in rp["changed"]:
            if ch["rebuild"]:
                nodes.append(ch["name"])
            if ch["relan"]:
                nodes.append(f["nodes"][ch["name"]]["host"])
        self.sh([LAB / "lab.sh", "up", *nodes])
        self.sh([LAB / "lab.sh", "bootstrap", *nodes], timeout=3600)
        s["summary"] = ", ".join(nodes) + " built and bootstrapped"

    def do_rs_nautobot(self, s):
        self.sh([LAB / "lab.sh", "nautobot", "seed"])
        s["summary"] = "seeded from the restored intent"

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
        r = self.removal
        s["summary"] = f"{r['provider']} {r['provider_port']} released" + (
            f"; {r['provider2']} {r['provider2_port']} released" if r.get("dual_homed") else "")

    def do_rm_nautobot(self, s):
        r = self.removal
        self.sh([LAB / "lab.sh", "nautobot", "remove-customer", r["name"], "--host", r["host"], *_wans(r),
                 "--lan", r["lan"]])
        CP.forget(r["name"])                          # its customer-portal link dies with it
        s["summary"] = f"{r['name']} and {r['host']} removed from the model"


def _wans(c):
    """remove-customer's --wan arguments: the first provider's /30, and the second's for a dual-homed customer."""
    return ["--wan", c["wan_prefix"]] + (["--wan", c["wan2_prefix"]] if c.get("wan2_prefix") else [])


def resume_factory(rec):
    if rec["mode"] == "failover":
        raise RuntimeError("a failover measurement is an experiment: start a new one instead of resuming")
    return Run(rec["mode"], rec.get("spec"), rec.get("options", {}), resume_of=rec)


# ---- API ---------------------------------------------------------------------------------------------------------
@app.get("/api/version", tags=["state"], summary="The lab's version (VERSION; CHANGELOG.md has what changed)")
def api_version():
    return {"version": (LAB / "VERSION").read_text().strip(), "changelog": "https://github.com/dcantor/c8000v-dmvpn-lab/blob/main/CHANGELOG.md"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(HERE / "static" / "index.html"), headers={"Cache-Control": "no-store"})   # a UI change shows on the next load


@app.get("/api/state", tags=["state"], summary="The cloud: the model, and what every router in it is doing")
def api_state(refresh: bool = False, live: bool = True):
    snap = state.get(refresh=refresh, live=live)
    return {**snap, "faults": X.active(), "maintenance": M.active(), "app_status": prober.app_status(),
            "changes_open": sum(1 for c in CH.all_requests(200) if c["status"] in ("pending", "scheduled"))}


@app.get("/api/customers/suggest", tags=["provisioning"], summary="The next customer, every value allocated")
def api_suggest(region: str | None = None):
    try:
        return C.suggest(region=region)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/customers/validate", tags=["provisioning"], summary="Check a customer against the running lab")
def api_validate(spec: CustomerSpec):
    d = spec.model_dump()
    problems, plan = C.validate(d), C.plan(d)
    try:                                                         # what one more customer does to capacity
        note, prob = CAP.plan_note(_capacity(max_age=300), d.get("platform", "c8000v"), bool(d.get("dual_homed")))
        plan["capacity"] = note
        problems += [prob] if prob else []
    except Exception:                                            # noqa: BLE001 — capacity never blocks a plan by failing
        pass
    return {"problems": problems, "plan": plan}


@app.get("/api/customers/{name}/removal", tags=["provisioning"], summary="What removing a customer takes away")
def api_removal(name: str):
    problems, plan = C.removal_plan(name)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    return plan


@app.get("/api/customers/{name}", tags=["provisioning"], summary="A customer as it is now: what Modify edits")
def api_customer(name: str):
    cur = C.current(name)
    if cur is None:
        raise HTTPException(404, f"{name} is not a customer of this lab")
    return cur


@app.post("/api/customers/{name}/modify/validate", tags=["provisioning"], summary="Check changes to a customer and plan them")
def api_modify_validate(name: str, changes: CustomerChange):
    problems, plan = C.modify_plan(name, changes.model_dump(exclude_none=True))
    if plan is None:
        raise HTTPException(404, problems[0])
    return {"problems": problems, "plan": plan}


# ---- resilience: simulated failures, failover measurements ---------------------------------------------------------
class FaultRequest(BaseModel):
    kind: str = Field(examples=["hub-down"], description="hub-down | hub-wan | provider-down | site-wan | tunnel-down")
    target: str = Field(examples=["hub-east"], description="a hub, a provider, a customer, or a customer link (cust1:wan / cust1:wan2)")
    requested_by: str | None = None
    reason: str | None = None


@app.get("/api/faults", tags=["resilience"], summary="What can fail, what is failed now, and what failed before")
def api_faults():
    f = C.facts()
    return {"catalog": X.catalog(f), "active": X.active(), "history": X.history(), "max_minutes": X.FAULT_MAX_MIN}


@app.post("/api/faults", tags=["resilience"], summary="Simulate a failure (or file a change request for one)",
          responses={202: {"description": "change control covers failures: a change request was filed"}})
def api_fault(req: FaultRequest, request: Request):
    req.requested_by = _user(request).get("username") or req.requested_by
    f = C.facts()
    try:
        X._describe(f, req.kind, req.target)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if CH.covered("fault"):
        try:
            cr = CH.create("fault", {"kind": req.kind, "target": req.target}, f"simulate: {X.KINDS[req.kind]['title']} on {req.target}",
                           req.requested_by, req.reason or "", _fault_affects(req.kind, req.target))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"change": cr}, status_code=202)
    try:
        item = X.apply(f, req.kind, req.target, by=req.requested_by or "operator", note=req.reason or "")
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:                                          # noqa: BLE001
        raise HTTPException(502, f"{e.__class__.__name__}: {e}")
    _fault_maint(item, req.requested_by)
    state._cache.clear()
    return item


def _fault_maint(item, by, change=None):
    M.open_(C.facts(), "fault", item["id"], f"simulated failure: {item['title']} on {item['target']}", item["nodes"],
            _fault_affects(item["kind"], item["target"]), by or "?", change)


@app.delete("/api/faults/{fid}", tags=["resilience"], summary="Take a simulated failure out (never needs approval)")
def api_fault_restore(fid: str):
    try:
        rec = X.restore(C.facts(), fid)
        M.close(ref=fid, reason="restored")
    except KeyError:
        raise HTTPException(404, f"no active fault {fid}")
    except Exception as e:                                          # noqa: BLE001
        raise HTTPException(502, f"{e.__class__.__name__}: {e}")
    state._cache.clear()
    return rec


@app.post("/api/faults/restore-all", tags=["resilience"], summary="Take every simulated failure out")
def api_fault_restore_all():
    out = X.restore_all(C.facts())
    for x in out:
        M.close(ref=x["id"], reason="restored")
    state._cache.clear()
    return out


@app.get("/api/failover", tags=["resilience"], summary="Failover measurements, newest first")
def api_failover_list():
    return X.results()


@app.get("/api/failover/{run_id}", tags=["resilience"], summary="One failover measurement, flow by flow")
def api_failover(run_id: str):
    r = X.result(run_id)
    if r is None:
        raise HTTPException(404, "no such measurement")
    return r


# ---- change control --------------------------------------------------------------------------------------------------
class Decision(BaseModel):
    by: str = Field("", description="ignored: the signed-in account decides (and must not be the requester when four-eyes is on)")
    comment: str = ""
    emergency: bool = Field(False, description="approve and start now, outside the change window (needs a comment)")


@app.get("/api/changes", tags=["change control"], summary="Change requests, newest first")
def api_changes():
    return {"requests": CH.all_requests(), "policy": CH.policy(), "window_open": CH.window_open(), "next_window": CH.next_window()}


@app.get("/api/changes/{cid}", tags=["change control"], summary="One change request")
def api_change(cid: str):
    cr = CH.get(cid)
    if cr is None:
        raise HTTPException(404, f"no change request {cid}")
    return cr


def _decide(cid, d: Decision, approve, request=None):
    d.by = _user(request).get("username") or d.by if request is not None else d.by
    try:
        cr = CH.decide(cid, d.by, approve, d.comment, d.emergency)
    except KeyError:
        raise HTTPException(404, f"no change request {cid}")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if cr["status"] == "approved":
        cr = _start_change(cr)
    return cr


@app.post("/api/changes/{cid}/approve", tags=["change control"], summary="Approve: it starts now, or in the next change window")
def api_change_approve(cid: str, d: Decision, request: Request):
    return _decide(cid, d, True, request)


@app.post("/api/changes/{cid}/reject", tags=["change control"], summary="Reject a change request")
def api_change_reject(cid: str, d: Decision, request: Request):
    return _decide(cid, d, False, request)


@app.post("/api/changes/{cid}/cancel", tags=["change control"], summary="Withdraw a pending or scheduled change request")
def api_change_cancel(cid: str, d: Decision, request: Request):
    u = _user(request)
    cr = CH.get(cid)
    if cr and u and cr["requested_by"] != u["username"] and not A.has(u, "approver"):
        raise HTTPException(403, "only the requester or an approver can withdraw a change request")
    try:
        return CH.cancel(cid, u.get("username") or d.by)
    except KeyError:
        raise HTTPException(404, f"no change request {cid}")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/policy", tags=["change control"], summary="The change policy: what needs approval, the change windows")
def api_policy():
    return {**CH.policy(), "coverable": CH.COVERABLE, "window_open": CH.window_open(), "next_window": CH.next_window()}


@app.put("/api/policy", tags=["change control"], summary="Change the change policy")
def api_policy_put(p: dict):
    keep = {k: p[k] for k in ("approval", "windows", "emergency", "expire_hours") if k in p}
    merged = {**CH.policy(), **keep}
    probs = CH.validate_policy(merged)
    if probs:
        raise HTTPException(400, "; ".join(probs))
    CH.save_policy(merged)
    return api_policy()


def _changes_scheduler():
    """Every half minute: start scheduled changes whose window has opened, expire requests nobody decided on, and put
    back simulated failures that have been in too long."""
    while True:
        try:
            ready, old = CH.due()
            for cr in ready:
                _start_change(cr)
            for cr in old:
                CH.mark(cr["id"], "expired", f"nobody decided within {CH.policy()['expire_hours']} h")
            X.expire(C.facts())
            live_faults = {x["id"] for x in X.active()}
            live_jobs = {r.id for r in registry.runs.values() if r.status in ("running", "queued")}
            for m in M.active():
                if (m["kind"] == "fault" and m["ref"] not in live_faults) or (m["kind"] == "job" and m["ref"] not in live_jobs):
                    M.close(mid=m["id"], reason="its fault or job ended")
        except Exception:                                           # noqa: BLE001
            pass
        time.sleep(30)


# ---- the customer portal -------------------------------------------------------------------------------------------
@app.get("/api/customers/{name}/portal-link", tags=["customer portal"], summary="A customer's own read-only link (made on first use)")
def api_portal_link(name: str):
    if C.current(name) is None:
        raise HTTPException(404, f"{name} is not a customer of this lab")
    t = CP.token_for(name)
    return {"customer": name, "path": f"/c/{t}", "url": f"http://{PUBLIC_HOST}:{WEBAPP_PORT}/c/{t}"}


@app.post("/api/customers/{name}/portal-link/rotate", tags=["customer portal"], summary="Give a customer a new link; the old one stops working")
def api_portal_rotate(name: str):
    if C.current(name) is None:
        raise HTTPException(404, f"{name} is not a customer of this lab")
    t = CP.token_for(name, rotate=True)
    return {"customer": name, "path": f"/c/{t}", "url": f"http://{PUBLIC_HOST}:{WEBAPP_PORT}/c/{t}"}


def _cust(token, request=None):
    """The customer a link or a session speaks for: /api/c/<secret token>/… or, signed in as a customer, /api/c/me/…"""
    if token == "me":
        u = _user(request) if request is not None else {}
        if not u or "customer" not in (u.get("roles") or []):
            raise HTTPException(401, "sign in with a customer account")
        c = u.get("customer")
    else:
        c = CP.customer_of(token)
    if c is None or C.current(c) is None:
        raise HTTPException(404, "this link is not (or no longer) valid")
    return c


@app.get("/c/{token}", include_in_schema=False)
def customer_page(token: str):
    _cust(token)
    return FileResponse(str(HERE / "static" / "index.html"), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/api/c/{token}/state", tags=["customer portal"], summary="The service as one customer sees it")
def api_c_state(token: str, request: Request, refresh: bool = False):
    c = _cust(token, request)
    v = CP.view(state.get(refresh=refresh), c, X.active(), CH.all_requests(200), X.KINDS, M.active())
    v["app_status"] = {c: prober.app_status(c)}
    return v


@app.get("/api/c/{token}/sla", tags=["customer portal"], summary="The customer's service levels")
def api_c_sla(token: str, request: Request, window: str = "30d"):
    return api_customer_sla(_cust(token, request), window)


@app.get("/api/c/{token}/map.pdf", tags=["customer portal"], summary="The customer's network view and monthly report, as a PDF",
         response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def api_c_pdf(token: str, request: Request):
    return api_customer_map_pdf(_cust(token, request))


# ---- security: IKE authentication ---------------------------------------------------------------------------------
@app.get("/api/security", tags=["provisioning"], summary="The IKE pre-shared key: where it comes from, its age, its fingerprint")
def api_security():
    """Never the key itself: where it comes from, when it was set, and the last characters of its SHA-256."""
    last = next((r for r in registry.list(100) if r["mode"] == "rotatepsk" and r["status"] == "success"), None)
    return {"psk": SEC.psk_info(), "routers": C.facts()["hubs"] + C.facts()["customers"],
            "last_rotation": {k: last.get(k) for k in ("id", "finished", "started_by")} if last else None}



# ---- capacity ------------------------------------------------------------------------------------------------------
_cap = {"last": None}


def _lab_conf_sizes():
    text = (LAB / "lab.conf").read_text()
    return {k: int(re.search(rf"^{k}=(\d+)", text, re.M)[1]) for k in ("C8000V_RAM_MIB", "VYOS_RAM_MIB", "HOST_RAM_MIB")}


def _capacity(max_age=90):
    last = _cap["last"]
    if last and time.time() - last["generated"] < max_age:
        return last
    _cap["last"] = CAP.compute(C.facts(), _snap["snap"] or state.get(), _lab_conf_sizes())
    return _cap["last"]


def _capacity_refresher():
    """Every two minutes while the lab runs, so /metrics has capacity without a scrape waiting on SSH."""
    time.sleep(90)
    while True:
        try:
            if any(st == "running" for st in _vm_states().values()):
                _capacity(max_age=0)
        except Exception:                                       # noqa: BLE001
            pass
        time.sleep(120)


@app.get("/api/capacity", tags=["state"], summary="Capacity: how loaded each hub is, and how much room the lab has for more customers")
def api_capacity(refresh: bool = False):
    return _capacity(max_age=0 if refresh else 90)


@app.get("/api/capacity/policy", tags=["state"], summary="The capacity planning figures and thresholds")
def api_capacity_policy():
    return CAP.policy()


@app.put("/api/capacity/policy", tags=["state"], summary="Change the capacity planning figures and thresholds (admin)")
def api_capacity_policy_put(p: dict):
    merged = {**CAP.policy(), **{k: p[k] for k in CAP.DEFAULT if k in p}}
    probs = CAP.validate_policy(merged)
    if probs:
        raise HTTPException(400, "; ".join(probs))
    CAP.save_policy(merged)
    _cap["last"] = None
    return CAP.policy()


# ---- maintenance ----------------------------------------------------------------------------------------------------
class MaintenanceRequest(BaseModel):
    summary: str = Field(examples=["replacing hub-west's uplink"])
    routers: list[str] = Field(default_factory=list, description="routers worked on (their alerts are muted)")
    customers: list[str] = Field(default_factory=list, description="customers affected, or [\"all\"]")


@app.get("/api/maintenance", tags=["change control"], summary="Maintenance under way, and recent")
def api_maintenance():
    return {"active": M.active(), "history": M.history()}


@app.post("/api/maintenance", tags=["change control"], summary="Declare maintenance by hand (alerts muted, customers told)")
def api_maintenance_open(req: MaintenanceRequest, request: Request):
    f = C.facts()
    bad = [r for r in req.routers if r not in f["nodes"]] + [c for c in req.customers if c != "all" and c not in f["customers"]]
    if bad or not (req.routers or req.customers) or not req.summary.strip():
        raise HTTPException(400, "say what, and name routers or customers of this lab" + (f" (not in the lab: {', '.join(bad)})" if bad else ""))
    return M.open_(f, "manual", None, req.summary.strip(), req.routers, req.customers, _user(request).get("username", "?"))


@app.delete("/api/maintenance/{mid}", tags=["change control"], summary="End a maintenance")
def api_maintenance_close(mid: str):
    if not M.close(mid=mid, reason="ended by hand"):
        raise HTTPException(404, f"no maintenance {mid} under way")
    return {"ok": True}


# ---- configuration history ------------------------------------------------------------------------------------------
@app.get("/api/runs/{run_id}/config", tags=["runs"], summary="What a job changed in each router's configuration (unified diffs)")
def api_run_config(run_id: str):
    d = CFG.diff(run_id)
    if d is None:
        raise HTTPException(404, "no configuration snapshots for this job (it changes no router, or is still running)")
    return d


@app.get("/api/config-history", tags=["state"], summary="Jobs that changed configurations, newest first (optionally one router's)")
def api_config_history(router: str | None = None):
    return CFG.history(router)


@app.get("/api/config-history/{job}/{router}", tags=["state"], summary="A router's configuration before or after a job",
         response_class=PlainTextResponse)
def api_config_snapshot(job: str, router: str, which: str = Query("after", description="before | after")):
    t = CFG.config(job, router, which)
    if t is None:
        raise HTTPException(404, "no such snapshot")
    return PlainTextResponse(t)


# ---- customer self-service -----------------------------------------------------------------------------------------
class CustomerRequest(BaseModel):
    applications: list[str] | None = Field(None, description="the applications to subscribe to (the whole list)")
    prefer_hub: str | None = Field(None, description="a hub, or empty for none")
    dual_homed: bool | None = Field(None, description="ask for (or give up) a second provider")
    reason: str = ""


@app.post("/api/c/{token}/requests", tags=["customer portal"], summary="A customer asks for a change (a change request for the operators)")
def api_c_request(token: str, body: CustomerRequest, request: Request):
    """Only a signed-in customer account (token `me`) can ask. What it may change: its applications, its preferred hub,
    dual-homing. The request always waits for an approver, whatever the change policy — it is a customer asking."""
    if token != "me":
        raise HTTPException(401, "sign in with your customer account to ask for changes")
    c, u = _cust(token, request), _user(request)
    ch = {}
    if body.applications is not None:
        ch["customer"] = {"applications": sorted(set(body.applications))}
    if body.prefer_hub is not None:
        ch["prefer_hub"] = body.prefer_hub
    if body.dual_homed is not None:
        ch["dual_homed"] = body.dual_homed
    if not ch:
        raise HTTPException(400, "nothing asked for")
    req = RunRequest(mode="modify", name=c, changes=CustomerChange(**ch), options={"test": False}, requested_by=u["username"],
                     reason=body.reason)
    run = _new_run(req)                                   # validates, and plans the change
    summary, affects = _change_summary(req, run)
    return CH.create("modify", req.model_dump(exclude_none=True), summary, u["username"], body.reason, affects, source="customer")


@app.get("/api/c/{token}/catalogue", tags=["customer portal"], summary="Every application on offer (to subscribe to)")
def api_c_catalogue(token: str, request: Request):
    _cust(token, request)
    return [{k: a.get(k) for k in ("id", "name", "description", "url", "protocol", "port", "hubs")} for a in C.facts()["inv"].get("applications") or []]


@app.get("/api/c/{token}/requests", tags=["customer portal"], summary="The customer's own requests")
def api_c_requests(token: str, request: Request):
    c = _cust(token, request)
    return [{k: cr.get(k) for k in ("id", "summary", "status", "requested_by", "requested_at", "decided_at", "comment", "reason", "run_id")}
            for cr in CH.all_requests(200) if cr.get("source") == "customer" and (cr.get("request") or {}).get("name") == c]


class Diagnostic(BaseModel):
    kind: str = Field("apps", description="apps: every subscribed application at every hub · hubs: every hub's LAN · trace: a path")
    app: str | None = None
    hub: str | None = None


@app.post("/api/c/{token}/diagnostics", tags=["customer portal"], summary="Test the service from the customer's own LAN host")
def api_c_diag(token: str, body: Diagnostic, request: Request):
    """Pings (and HTTPS connects) from the customer's LAN host to its own applications and the hubs, or a traceroute
    to one application's VIP. Only its own applications and the hubs: never another customer's site."""
    c = _cust(token, request)
    f = C.facts()
    inv, nodes = f["inv"], f["nodes"]
    host = nodes[nodes[c]["host"]]
    if body.kind == "trace":
        app_ = next((a for a in inv.get("applications") or [] if a["id"] == body.app), None)
        subs = set((nodes[c].get("customer") or {}).get("applications") or [])
        if not app_ or body.app not in subs or body.hub not in app_.get("vips", {}):
            raise HTTPException(404, "not one of your applications at that hub")
        v = app_["vips"][body.hub]
        out = _ssh(host["mgmt_ip"], f"traceroute -n -q 2 -w 2 -m 8 {v} 2>&1", HOST_USER, HOST_PASS, timeout=60)
        own = _owners(nodes)
        hops = []
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+)\s+(.*)$", line)
            if m:
                ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", m[2])
                n_ = own.get(ip[1], (None, None))[0] if ip else None
                hops.append({"hop": int(m[1]), "ip": ip[1] if ip else None, "node": n_ if n_ in (c, body.hub) or (n_ and nodes[n_]["role"] in ("hub", "provider")) else ("another site" if n_ else None)})
        return {"kind": "trace", "target": f"{body.app} at {body.hub} ({v})", "hops": hops}
    hubs = {h: str(__import__("ipaddress").ip_network(nodes[h]["lan"]).network_address + 1) for h in f["hubs"]}
    vips = SLA.Prober.app_targets(inv, c) if body.kind == "apps" else {}
    p = SLA.Prober()
    res = p.probe_host(c, host["mgmt_ip"], hubs if body.kind == "hubs" else {}, vips)
    return {"kind": body.kind, "hubs": res if body.kind == "hubs" else {}, "apps": p.app_status(c), "at": time.time()}


@app.get("/api/drift", tags=["state"], summary="The latest configuration drift report")
def api_drift():
    """Written by a `drift` run (Provision → Check for drift, or the map), and every DRIFT_INTERVAL_H hours by the portal
    itself while no run is in progress. `null` until the first check."""
    rep = D.latest()
    if rep:
        rep["interval_h"] = DRIFT_INTERVAL_H
    return rep


@app.get("/api/backup", tags=["backup"], summary="Download a backup of the whole lab state (.tar.gz)",
         response_class=Response, responses={200: {"content": {"application/gzip": {}}}})
def api_backup(running: bool = Query(True, description="also keep every router's running configuration (evidence)")):
    """The intent (lab.conf, customers.json, applications.json), every rendered file, this lab's Nautobot model, the
    routers' running configurations and the Terraform state, with a manifest of SHA-256s. See backup.py."""
    f = C.facts()
    data, man = B.create(f["inv"], (LAB / "VERSION").read_text().strip(), with_running=running)
    name = f"{LAB_NAME}-backup-{time.strftime('%Y%m%d-%H%M')}.tar.gz"
    return Response(data, media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                                   "X-Backup-Files": str(len(man["files"]))})


@app.post("/api/backups", tags=["backup"], summary="Upload a backup and see what restoring it would change")
async def api_backup_upload(request: Request):
    """The request body is the .tar.gz itself (Content-Type: application/gzip). Nothing changes yet: the answer is the
    backup's manifest and the restore plan; `POST /api/runs {"mode": "restore", "name": <id>}` carries it out."""
    data = await request.body()
    if len(data) > 64 * 1024 * 1024:
        raise HTTPException(413, "a backup is a few hundred kB; this is too big")
    try:
        uid, man, files = B.save_upload(data)
        problems, rplan = B.plan(C.facts()["inv"], files)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": uid, "manifest": {k: man.get(k) for k in ("lab", "version", "created", "created_iso", "host", "customers", "hubs", "notes")},
            "files": len(man["files"]), "problems": problems, "plan": rplan}


@app.post("/api/drift/check", tags=["state"], summary="Run the drift check now, outside the run queue, and return the report")
def api_drift_check():
    """The same read-only check a `drift` run makes, answered in the request (15–60 s). It does not queue behind a
    run in progress — the test suites use it from inside one — so it may see a router mid-change."""
    lines = []
    rep = D.check(C.facts()["inv"], say=lines.append)
    rep["log"] = lines
    return rep


@app.get("/api/query/{node}", tags=["state"], summary="One read-only `show` command on one C8000v")
def api_query(node: str, command: str):
    nodes = C.facts()["nodes"]
    if node not in nodes or nodes[node]["role"] not in ("hub", "spoke"):
        raise HTTPException(404, f"{node} is not a router of this lab")
    if not re.fullmatch(r"show [A-Za-z0-9 ._/|-]+", command):
        raise HTTPException(400, "only a single `show ...` command is allowed")
    n = nodes[node]
    if n.get("platform", "c8000v") == "vyos":
        out = vyos_show(n["mgmt_ip"], command)
    else:
        out = ios(n["mgmt_ip"], command)[command]
    return {"node": node, "command": command, "output": SEC.mask(out)}


@app.get("/api/customers/{name}/map.pdf", tags=["provisioning"], summary="A customer's view of the network map, as a PDF",
         response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def api_customer_map_pdf(name: str):
    """Headless Chrome (Playwright, the system google-chrome) opens this portal's own page in print mode
    (`/?print=<customer>`): the Network map focused on the customer — the hubs, the provider, its site and host,
    its shortcuts — coloured from the live state, with the customer's details under it. A4 landscape."""
    nodes = C.facts()["nodes"]
    if name not in nodes or nodes[name]["role"] != "spoke":
        raise HTTPException(404, f"{name} is not a customer of this lab")
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
                ctx.add_cookies([{"name": A.COOKIE, "value": A.renderer_cookie(), "domain": "127.0.0.1", "path": "/"}])
                page = ctx.new_page()
                page.goto(f"http://127.0.0.1:{WEBAPP_PORT}/?print={name}#map", wait_until="domcontentloaded")
                page.wait_for_function("window.__mapReady === true", timeout=120_000)
                pdf = page.pdf(format="A4", landscape=True, print_background=True,
                               margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"})
            finally:
                browser.close()
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(500, f"the PDF could not be rendered: {e.__class__.__name__}: {str(e)[:300]}")
    fname = f"{LAB_NAME}-{name}-network-map-{time.strftime('%Y%m%d-%H%M')}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/customers/{name}/sla", tags=["state"], summary="A customer's service levels over a window, from VictoriaMetrics")
def api_customer_sla(name: str, window: str = Query("30d", description="24h | 7d | 30d | month (this month so far) | YYYY-MM")):
    """Availability (registered with at least one hub) and full registration (all hubs) from lab_dmvpn_nhs_up; latency
    and loss from the portal's own probes (lab_sla_*): every minute the customer's LAN host pings every hub's LAN.
    Each value is over the window; `coverage` says how much of the window was monitored at all."""
    f = C.facts()
    if name not in f["nodes"] or f["nodes"][name]["role"] != "spoke":
        raise HTTPException(404, f"{name} is not a customer of this lab")
    try:
        rep = SLA.report(f"http://{f['inv']['oob']['nms']}:8428", LAB_NAME, name, f["hubs"], window)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(502, f"VictoriaMetrics: {e.__class__.__name__}: {e}")
    rep["company"] = (f["nodes"][name].get("customer") or {}).get("company")
    with prober.lock:
        rep["now"] = prober.latest.get(name)
    return rep


@app.get("/api/hosts/{host}/ping", tags=["state"], summary="Ping one LAN host from another, over the overlay")
def api_host_ping(host: str, target: str, count: int = Query(4, ge=1, le=10)):
    """Both ends must be LAN hosts of this lab; the target's LAN address comes from the inventory, so nothing
    free-form reaches a host's shell. The ping runs on `host` over SSH (lab / lab) and crosses the DMVPN."""
    nodes = C.facts()["nodes"]
    for name in (host, target):
        if name not in nodes or nodes[name]["role"] != "host":
            raise HTTPException(404, f"{name} is not a LAN host of this lab")
    if host == target:
        raise HTTPException(400, "pick another host to ping")
    addr = nodes[target]["lan_ip"].split("/")[0]
    cmd = f"ping -c {count} -W 2 {addr}"
    try:
        out = _ssh(nodes[host]["mgmt_ip"], f"{cmd}; echo __rc=$?", HOST_USER, HOST_PASS, timeout=15 + 3 * count)
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(502, f"{host}: {e.__class__.__name__}: {e}")
    rc = int(out.rsplit("__rc=", 1)[1].strip()) if "__rc=" in out else -1
    return {"host": host, "target": target, "address": addr, "command": cmd, "ok": rc == 0,
            "output": out.rsplit("__rc=", 1)[0].rstrip() + "\n"}


def _owners(nodes):
    """Every address a traceroute can answer from -> (node, what): tunnel, WAN, LAN gateway, a hub's LAN, router-id, host."""
    import ipaddress
    own = {}
    for name, n in nodes.items():
        if n.get("tunnel_ip"):
            own[n["tunnel_ip"]] = (name, "Tunnel0")
        if n.get("nbma"):
            own[n["nbma"]] = (name, "WAN")
        if n.get("router_id"):
            own[n["router_id"]] = (name, "router-id")
        if n.get("lan_port"):
            own[n["lan_port"]["ip"].split("/")[0]] = (name, "LAN")
        elif n.get("role") == "hub" and n.get("lan"):
            own[str(ipaddress.ip_network(n["lan"]).network_address + 1)] = (name, "LAN")
        if n.get("role") == "host":
            own[n["lan_ip"].split("/")[0]] = (name, "host")
        for p_ in n.get("ports") or []:
            if p_.get("ip"):
                own.setdefault(p_["ip"].split("/")[0], (name, p_["name"]))
    return own


def _hosts_pair(host, target):
    nodes = C.facts()["nodes"]
    for name in (host, target):
        if name not in nodes or nodes[name]["role"] != "host":
            raise HTTPException(404, f"{name} is not a LAN host of this lab")
    if host == target:
        raise HTTPException(400, "pick another host")
    return nodes


@app.get("/api/hosts/{host}/traceroute", tags=["state"], summary="Trace the path from one LAN host to another, hop by hop")
def api_host_traceroute(host: str, target: str, warm: bool = Query(False, description="send 10 pings first: traffic that makes the hub redirect and phase 3 build the shortcut")):
    """`traceroute -n` on `host` to the target's LAN address; every hop that answers is named from the inventory. The
    verdict: `via` — the hub the packets crossed (the path before NHRP has resolved a shortcut) — or `direct` (a
    phase 3 shortcut between the two customer routers). A customer router answers from its tunnel address, so a
    hub in the path shows up as its Tunnel0. Two probes per hop: the first reply of a router is sometimes lost."""
    nodes = _hosts_pair(host, target)
    addr = nodes[target]["lan_ip"].split("/")[0]
    cmd = f"traceroute -n -q 2 -w 2 -m 8 {addr}"
    pre = f"ping -c 10 -i 0.2 -W 1 -q {addr} >/dev/null 2>&1; " if warm else ""
    try:
        out = _ssh(nodes[host]["mgmt_ip"], pre + cmd + " 2>&1", HOST_USER, HOST_PASS, timeout=60)
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(502, f"{host}: {e.__class__.__name__}: {e}")
    own = _owners(nodes)
    hops = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", m[2])
        rtt = re.search(r"([\d.]+) ms", m[2])
        node, what = own.get(ip[1], (None, None)) if ip else (None, None)
        hops.append({"hop": int(m[1]), "ip": ip[1] if ip else None, "rtt_ms": float(rtt[1]) if rtt else None,
                     "node": node, "what": what})
    src_router, dst_router = nodes[host]["router"], nodes[target]["router"]
    reached = bool(hops) and hops[-1]["ip"] == addr
    hubs = [h["node"] for h in hops if h["node"] and nodes[h["node"]]["role"] == "hub"]
    # one hop between the two customer routers is a hub; none is the shortcut. A silent middle hop is still counted.
    middle = len(hops) - 3 if reached else None
    via = hubs[0] if hubs else None
    kind = "direct" if reached and middle == 0 else ("hub" if reached and middle and middle > 0 else "unknown")
    path = [host, src_router] + ([via or "?hub"] if kind == "hub" else []) + [dst_router, target]
    return {"host": host, "target": target, "address": addr, "command": (f"ping -c 10 {addr}; " if warm else "") + cmd, "reached": reached, "kind": kind,
            "via": via, "path": path, "hops": hops, "output": out}


@app.post("/api/hosts/{host}/path/reset", tags=["state"], summary="Forget the shortcut between two customers, so the next packets go through a hub")
def api_path_reset(host: str, target: str):
    """Clears the NHRP entries the two customer routers hold for each other — the shortcut: on IOS the peer's LAN prefix
    and its tunnel address — so the next traffic goes
    through a hub again and phase 3 builds the shortcut anew. It changes no configuration and touches no hub; the
    registrations with the hubs stay up. On a VyOS customer, nhrpd can only clear its whole cache of dynamic entries,
    so its other shortcuts are rebuilt too."""
    nodes = _hosts_pair(host, target)
    a, b = nodes[host]["router"], nodes[target]["router"]
    if a == b:
        raise HTTPException(400, "both hosts sit behind the same router")
    done = []
    for me, peer in ((a, b), (b, a)):
        n = nodes[me]
        try:
            if n.get("platform", "c8000v") == "vyos":
                vtysh(n["mgmt_ip"], "clear ip nhrp cache")
                done.append(f"{me}: clear ip nhrp cache (VyOS: every dynamic entry)")
            else:                     # the shortcut is two entries: the peer's LAN prefix, and its tunnel address
                import ipaddress
                lan = ipaddress.ip_network(nodes[peer]["lan"])
                cmds = [f"clear ip nhrp {lan.network_address} {lan.netmask}", f"clear ip nhrp {nodes[peer]['tunnel_ip']}"]
                ios(n["mgmt_ip"], *cmds)
                done += [f"{me}: {c}" for c in cmds]
        except Exception as e:                                    # noqa: BLE001
            raise HTTPException(502, f"{me}: {e.__class__.__name__}: {e}")
    state._cache.clear()
    return {"routers": [a, b], "done": done}


@app.get("/api/config/{node}", tags=["state"], summary="A node's configuration, read live from the device")
def api_config(node: str):
    """C8000v: `show running-config`; the VyOS provider: `show configuration commands` (the set form lab.sh renders);
    an Alpine host has no configuration file of its own, so its network set-up: interfaces, addresses, routes."""
    nodes = C.facts()["nodes"]
    if node not in nodes:
        raise HTTPException(404, f"{node} is not a node of this lab")
    n = nodes[node]
    try:
        if n["role"] in ("hub", "spoke") and n.get("platform", "c8000v") == "c8000v":
            cmd = "show running-config"
            out = ios(n["mgmt_ip"], cmd)[cmd]
        elif n.get("platform") == "vyos":
            cmd = "show configuration commands"
            out = vyos_op(n["mgmt_ip"], cmd)
        else:
            cmd = "network configuration (hostname, /etc/network/interfaces, ip addr, ip route)"
            out = _ssh(n["mgmt_ip"], "echo \"# hostname: $(hostname)\"; echo; echo '# /etc/network/interfaces'; cat /etc/network/interfaces 2>/dev/null;"
                                     " echo; echo '# ip -4 addr'; ip -4 addr; echo; echo '# ip route'; ip route", HOST_USER, HOST_PASS, timeout=30)
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(502, f"{node}: {e.__class__.__name__}: {e}")
    return {"node": node, "role": n["role"], "command": cmd, "lines": out.count("\n") + 1, "output": SEC.mask(out)}


def _run_platform(d):
    return ((d.get("spec") or {}).get("platform") or (d.get("removal") or {}).get("platform")
            or ((d.get("modification") or {}).get("new") or {}).get("platform") or "*")


@app.get("/api/runs/estimates", tags=["runs"], summary="How long each step of each kind of run usually takes")
def api_run_estimates():
    """The median duration of every step, from the finished runs on record: by mode and router type
    (`customer/vyos/bootstrap`), by mode (`customer/*/bootstrap`) and by step alone (`*/*/bootstrap`). Steps carried
    over from an earlier run by a resume are not counted — they took no time in the run that shows them. The portal's
    progress bars add the remaining steps' medians up for an ETA."""
    import json
    import statistics
    samples = {}
    for f in RUNS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        plat = _run_platform(d)
        for st in d.get("steps") or []:
            if st.get("status") != "success" or not st.get("started") or not st.get("finished") \
                    or str(st.get("summary") or "").startswith("(from run"):
                continue
            secs = st["finished"] - st["started"]
            for key in (f"{d['mode']}/{plat}/{st['name']}", f"{d['mode']}/*/{st['name']}", f"*/*/{st['name']}"):
                samples.setdefault(key, []).append(secs)
    return {"steps": {k: round(statistics.median(v), 1) for k, v in sorted(samples.items())},
            "samples": {k: len(v) for k, v in sorted(samples.items())}}


def _change_summary(req, run):
    """One line for a change request, and the customers it touches ("all" for the whole cloud)."""
    m = req.mode
    if m == "modify":
        md = run.modification
        return (f"modify {md['name']}: " + "; ".join(f"{c['what']} {c['old']} → {c['new']}" for c in md["changes"]))[:300], [md["name"]]
    if m == "remove":
        n = C.facts()["nodes"].get(req.name) or {}
        return f"remove {req.name} ({(n.get('customer') or {}).get('company', '')})", [req.name]
    if m == "restore":
        rp = run.restore["plan"]
        return (f"restore the backup of {run.restore['manifest']['created_iso'][:16]}: remove {', '.join(rp['removed']) or 'nothing'}, "
                f"build {', '.join(rp['added']) or 'nothing'}, change {', '.join(c['name'] for c in rp['changed']) or 'nothing'}"), ["all"]
    if m == "failover":
        fl = req.fault or {}
        return f"measure failover: {X.KINDS[fl['kind']]['title']} on {fl['target']} for {fl.get('hold', 60)} s", _fault_affects(fl["kind"], fl["target"])
    return {"deploy": "deploy the model to every router", "fixdrift": "put every router back to the model (fix drift)",
            "rotatepsk": "rotate the IKE pre-shared key on every router"}.get(m, m), ["all"]


def _job_scope(run):
    """(routers, customers) a disruptive job works on — for its maintenance record."""
    m = run.mode
    if m in ("modify", "remove"):
        return [run.spec["name"]], [run.spec["name"]]
    if m == "failover":
        fl = run.spec["fault"]
        return X._describe(C.facts(), fl["kind"], fl["target"])["nodes"], _fault_affects(fl["kind"], fl["target"])
    return ["all"], ["all"]


def _fault_affects(kind, target):
    f = C.facts()
    if kind in ("site-wan", "tunnel-down"):
        return [target.partition(":")[0]]
    if kind == "provider-down" and f.get("provider2") and target in f["provider2"]["nodes"]:
        return [c for c in f["customers"] if f["nodes"][c].get("wan2")]
    return ["all"]


def _new_run(req: RunRequest):
    """RunRequest -> Run (validated), not started."""
    if req.mode not in ("customer", "remove", "modify", "deploy", "plan", "test", "drift", "fixdrift", "restore", "failover",
                        "rotatepsk"):
        raise HTTPException(400, f"unknown mode {req.mode}")
    spec = req.customer.model_dump() if req.customer else ({"name": req.name} if req.name else None)
    if req.mode == "customer" and not spec:
        raise HTTPException(400, "mode customer needs a customer spec")
    if req.mode == "remove" and not (spec or {}).get("name"):
        raise HTTPException(400, "mode remove needs a name")
    if req.mode == "modify":
        if not req.name or not req.changes:
            raise HTTPException(400, "mode modify needs a name and changes")
        spec = {"name": req.name, "changes": req.changes.model_dump(exclude_none=True)}
    if req.mode == "failover":
        fl = req.fault or {}
        try:
            X._describe(C.facts(), fl.get("kind"), fl.get("target"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        spec = {"name": fl["target"], "fault": {"kind": fl["kind"], "target": fl["target"], "hold": max(20, min(int(fl.get("hold", 60)), 600))}}
    if req.mode == "rotatepsk":
        spec = {"name": "pre-shared key"}
    if req.mode in ("modify", "restore") and not (spec or {}).get("name"):
        raise HTTPException(400, f"mode {req.mode} needs a name")
    try:
        return Run(req.mode, spec, req.options)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _start_change(cr):
    """An approved change request: start what it asked for."""
    try:
        if cr["kind"] == "fault":
            item = X.apply(C.facts(), cr["request"]["kind"], cr["request"]["target"], by=cr["requested_by"], note=cr["id"])
            _fault_maint(item, cr["requested_by"], cr["id"])
            state._cache.clear()
            return CH.mark(cr["id"], "started", f"fault {item['id']} applied", fault_id=item["id"])
        run = _new_run(RunRequest(**cr["request"]))
        run.change = cr["id"]
        run.started_by = f"{cr['requested_by']}, approved by {cr.get('decided_by')}"
        registry.start(run)
        return CH.mark(cr["id"], "started", f"run {run.id} started", run_id=run.id)
    except HTTPException as e:
        return CH.mark(cr["id"], "failed", "could not start", error=str(e.detail))
    except Exception as e:                                          # noqa: BLE001
        return CH.mark(cr["id"], "failed", "could not start", error=f"{e.__class__.__name__}: {e}")


@app.post("/api/runs", tags=["runs"], summary="Start a run (or file a change request, when change control covers it)",
          responses={202: {"description": "change control covers this: a change request was filed instead"}})
def api_run(req: RunRequest, request: Request):
    """A run the change policy covers (GET /api/policy) does not start: it becomes a change request, answered with 202
    and `{"change": ...}`; it starts when someone else approves it (inside a change window)."""
    req.requested_by = _user(request).get("username") or req.requested_by        # the signed-in account, never a typed name
    if req.mode == "rotatepsk" and not A.has(_user(request), "admin"):
        raise HTTPException(403, "rotating the pre-shared key is for an admin")
    run = _new_run(req)
    run.started_by = req.requested_by
    if CH.covered(req.mode):
        summary, affects = _change_summary(req, run)
        try:
            cr = CH.create(req.mode, req.model_dump(exclude_none=True), summary, req.requested_by, req.reason or "", affects)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return JSONResponse({"change": cr}, status_code=202)
    return registry.start(run)


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


prober = SLA.Prober()


def _sla_prober():
    """Every minute while the lab runs: each customer's LAN host pings every hub's LAN (sla.py)."""
    while True:
        try:
            if any(st == "running" for st in _vm_states().values()):
                prober.run_once(C.facts()["inv"])
        except Exception:                                       # noqa: BLE001
            pass
        time.sleep(60)


DRIFT_INTERVAL_H = float(os.environ.get("DRIFT_INTERVAL_H", "6"))    # 0 turns the scheduled drift check off


def _drift_scheduler():
    """A drift run every DRIFT_INTERVAL_H hours, while the lab runs and nothing else does; it shows in Runs like any other."""
    time.sleep(300)
    while DRIFT_INTERVAL_H > 0:
        try:
            last = (D.latest() or {}).get("generated") or 0
            if time.time() - last >= DRIFT_INTERVAL_H * 3600 and not registry.busy() \
                    and all(st == "running" for n, st in _vm_states().items()):
                registry.start(Run("drift", None, {"scheduled": True}))
        except Exception:                                       # noqa: BLE001 — never let the scheduler die
            pass
        time.sleep(600)


@app.on_event("startup")
def _start_refresher():
    threading.Thread(target=_refresher, daemon=True).start()
    threading.Thread(target=_drift_scheduler, daemon=True).start()
    threading.Thread(target=_sla_prober, daemon=True).start()
    threading.Thread(target=_changes_scheduler, daemon=True).start()
    threading.Thread(target=_capacity_refresher, daemon=True).start()


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
        out += _g("lab_dmvpn_nhs2_up", "Hubs a dual-homed customer is registered with over the second cloud")
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
                if (st.get("expect") or {}).get("nhs2"):
                    out.append(metric_line("lab_dmvpn_nhs2_up", lab, len(st.get("nhs2_up") or [])))
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
            out.append(metric_line("lab_provider_customers_expected", {"lab": L, "provider": p_},
                                   (st.get("expect") or {}).get("customers", len(hubs) + len(snap["customers"]))))
        out += _g("lab_host_up", "1 if the LAN host answers SSH")
        out += [metric_line("lab_host_up", {"lab": L, "host": h}, int(ok)) for h, ok in (snap.get("hosts_up") or {}).items()]
        h = snap.get("health") or {}
        out += _g("lab_health_ok", "1 if everything the model expects is true")
        out.append(metric_line("lab_health_ok", {"lab": L}, int(bool(h.get("ok")))))
        out += _g("lab_health_problems", "Problems the portal's health check reports")
        out.append(metric_line("lab_health_problems", {"lab": L}, len(h.get("problems") or [])))
        out += _g("lab_collector_last_refresh_seconds", "When the live state behind these gauges was collected")
        out.append(metric_line("lab_collector_last_refresh_seconds", {"lab": L}, int(snap["generated"])))
    cap = _cap["last"]
    if cap:
        out += _g("lab_capacity_used_ratio", "How much of a resource is in use: 0 to 1 (hub spokes, IPsec, CPU, DRAM, WAN throughput; provider ports; host)")
        for h in cap["hubs"]:
            for r in h["resources"]:
                if r["pct"] is not None:
                    out.append(metric_line("lab_capacity_used_ratio", {"lab": L, "node": h["name"], "resource": r["name"]}, round(r["pct"] / 100, 4)))
        for x in cap["providers"]:
            out.append(metric_line("lab_capacity_used_ratio", {"lab": L, "node": x["name"], "resource": "Customer ports"}, round(x["resource"]["pct"] / 100, 4)))
        for r in cap["host"]["resources"]:
            out.append(metric_line("lab_capacity_used_ratio", {"lab": L, "node": "lab-host", "resource": r["name"]}, round(r["pct"] / 100, 4)))
        out += _g("lab_capacity_room_customers", "How many more customers of a platform fit before the first limit")
        for plat in ("c8000v", "vyos"):
            out.append(metric_line("lab_capacity_room_customers", {"lab": L, "platform": plat}, cap["room"][plat]["customers"]))
    out += prober.metrics(L, metric_line)
    out += M.metrics(L, metric_line, C.facts())
    rep = D.latest()
    if rep:
        out += _g("lab_config_drift", "1 if the router's running configuration differs from the model (latest drift check)")
        out += _g("lab_config_drift_lines", "Lines or resources that differ from the model (latest drift check)")
        for r, x in sorted(rep["nodes"].items()):
            lab = {"lab": L, "router": r, "role": x["role"]}
            out.append(metric_line("lab_config_drift", lab, int(x["status"] == "drift")))
            out.append(metric_line("lab_config_drift_lines", lab, len(x["items"])))
        out += _g("lab_config_drift_checked_seconds", "When the latest drift check ran")
        out.append(metric_line("lab_config_drift_checked_seconds", {"lab": L}, int(rep["generated"])))
    return PlainTextResponse(exposition(out + run_metrics(L, registry.list())), media_type="text/plain; version=0.0.4")


# ---- lab tools: where everything is, and how to get in ----------------------------------------------------------------
PUBLIC_HOST = os.environ.get("LAB_PUBLIC_HOST", "192.168.50.231")   # the lab host on the LAN, where the NMS tools are relayed


@app.get("/api/lab-tools", tags=["state"], summary="Every tool of the lab and how to reach every node (addresses, consoles, logins)")
def api_lab_tools():
    """The lab's lab-default credentials are in the repo already (lab.conf, the day-0 configs); nothing here is a secret.
    The Nautobot API token is not shown: `./lab.sh nautobot token` reads it from the NMS."""
    f = C.facts()
    inv, pub = f["inv"], PUBLIC_HOST
    nb = f"http://{pub}:8080"
    tools = [
        {"group": "This lab", "name": "Provisioning portal", "url": f"http://{pub}:{WEBAPP_PORT}",
         "login": "an account: admin / operator / approver / viewer, lab defaults in webapp/users.seed.json",
         "what": "the cloud, the Network map, provisioning jobs; customers sign in to their own view"},
        {"group": "This lab", "name": "Portal API (Swagger)", "url": f"http://{pub}:{WEBAPP_PORT}/docs", "login": "the portal's session (sign in first)",
         "what": "every endpoint the portal offers; /metrics and /api/sd for Prometheus stay open"},
        {"group": "This lab", "name": "Lab hub", "url": f"http://{pub}:8088", "login": "none",
         "what": "every lab on this host, VM power, host CPU / memory"},
        {"group": "This lab", "name": "GitHub repository", "url": "https://github.com/dcantor/c8000v-dmvpn-lab", "login": "public",
         "what": "the code: lab.sh, the renderer, NAC, tests, Nautobot, the portal"},
        {"group": "Source of truth", "name": "Nautobot", "url": nb, "login": "admin / admin",
         "what": "the shared NMS (cat9000v lab); API token: ./lab.sh nautobot token"},
        {"group": "Source of truth", "name": "Nautobot: this lab's devices", "url": f"{nb}/dcim/devices/?q={inv['nodes'][0]['domain'][:4]}",
         "login": "admin / admin", "what": "every VM of the lab, under its c8d- name"},
        {"group": "Source of truth", "name": "Nautobot: customer companies", "url": f"{nb}/tenancy/tenants/?tenant_group={inv['lab']}+customers",
         "login": "admin / admin", "what": "a tenant per customer, with its details and subscriptions"},
        {"group": "Source of truth", "name": "Nautobot: applications (Virtual Servers)", "url": f"{nb}/load-balancers/virtual-servers/",
         "login": "admin / admin", "what": "one per application per hosting hub, with its VIP"},
        {"group": "Monitoring", "name": "Grafana: C8000v DMVPN overview", "url": f"http://{pub}:3001/d/c8000v-dmvpn-lab-overview",
         "login": "anonymous viewer; admin / admin", "what": "the cloud, the provider, router syslog, the host"},
        {"group": "Monitoring", "name": "Prometheus: this lab's targets", "url": f"http://{pub}:9091/targets?search=c8000v",
         "login": "none", "what": "the portal, the provider's exporters, the hosts"},
        {"group": "Monitoring", "name": "Prometheus: alerts", "url": f"http://{pub}:9091/alerts", "login": "none",
         "what": "the Dmvpn* rules and the shared lab rules"},
        {"group": "Monitoring", "name": "VictoriaMetrics (vmui)", "url": f"http://{pub}:8428/vmui/", "login": "none",
         "what": "the metrics store: query lab_dmvpn_*, the provider's Telegraf series"},
        {"group": "Monitoring", "name": "VictoriaLogs (syslog)", "url": f"http://{pub}:9428/select/vmui/", "login": "none",
         "what": "router syslog: facility_keyword:local7 for the C8000vs, hostname:mpls for the provider"},
    ]
    creds = {"c8000v": ("admin", "admin"), "vyos": ("vyos", "vyos"), "alpine": ("lab", "lab")}
    kind = {"c8000v": "Catalyst 8000v", "vyos": "VyOS", "alpine": "Alpine Linux"}
    nodes = []
    for n in inv["nodes"]:
        plat = n.get("platform", "c8000v")
        user, pw = creds[plat]
        extra = []
        if plat == "c8000v":
            extra = [f"RESTCONF https://{n['mgmt_ip']}/restconf (admin / admin)", f"NETCONF {n['mgmt_ip']}:830",
                     "enable secret admin"]
        elif plat == "vyos":
            extra = [f"HTTPS API https://{n['mgmt_ip']} (key c8000v-dmvpn-lab)", f"node-exporter {n['mgmt_ip']}:9100",
                     f"frr-exporter {n['mgmt_ip']}:9342"] + (["NHRP / BGP: sudo vtysh -c 'show ip nhrp nhs'"] if n["role"] == "spoke" else [])
        elif n["role"] == "host":
            extra = [f"node-exporter {n['mgmt_ip']}:9100", "no sudo: user lab only"]
        addrs = {k: v for k, v in (("Tunnel0", n.get("tunnel_ip")), ("NBMA", n.get("nbma")), ("LAN", n.get("lan")),
                                   ("Router-id", n.get("router_id")), ("LAN address", n.get("lan_ip"))) if v}
        nodes.append({"name": n["name"], "role": n["role"], "kind": kind[plat], "vm": n["domain"], "mgmt_ip": n["mgmt_ip"],
                      "user": user, "password": pw, "ssh": f"ssh {user}@{n['mgmt_ip']}", "lab_sh": f"./lab.sh ssh {n['name']}",
                      "console": f"127.0.0.1:{n['console']}", "console_cmd": f"./lab.sh console {n['name']}",
                      "company": (n.get("customer") or {}).get("company"), "addresses": addrs, "extra": extra})
    nms = {"name": "nms (shared)", "role": "nms", "kind": "Ubuntu (cat9000v lab)", "vm": "nms", "mgmt_ip": inv["oob"]["nms"],
           "user": "lab", "password": "SSH key from the lab host", "ssh": "ssh lab@10.0.0.10", "lab_sh": "",
           "console": "127.0.0.1:5003", "console_cmd": "", "company": None,
           "addresses": {"on this lab's network": inv["oob"]["nms"], "home network": "10.0.0.10"},
           "extra": ["Nautobot, Prometheus, Grafana, VictoriaMetrics / Logs (docker compose in /opt/monitoring)"]}
    return {"public_host": pub, "tools": tools, "nodes": nodes + [nms],
            "access": {"lab_host": f"ssh {os.environ.get('USER', 'dcantor')}@{pub}", "lab_dir": str(LAB), "oob": inv["oob"],
                       "note": f"The management addresses ({inv['oob']['prefix']}) and the consoles live on the lab host "
                               f"(bridge {inv['oob']['network']}, host {inv['oob']['gateway']}): log in to the lab host first, "
                               f"then ssh from there or run ./lab.sh in {LAB}."}}


@app.get("/api/sd", tags=["monitoring"], summary="Prometheus HTTP service discovery: the portal, the provider's exporters, the hosts")
def prometheus_sd():
    f = C.facts()
    sd = [{"targets": [f"{LAB_HOST_IP}:{WEBAPP_PORT}"], "labels": {"lab": LAB_NAME, "job": "portal", "role": "portal", "node": "portal"}}]
    for name, n in f["nodes"].items():
        if n.get("platform") == "vyos":
            dc = "provider" if n["role"] == "provider" else (n.get("region") or "")
            sd.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {"lab": LAB_NAME, "job": "node", "role": n["role"], "node": name, "dc": dc}})
            sd.append({"targets": [f"{n['mgmt_ip']}:9342"], "labels": {"lab": LAB_NAME, "job": "frr", "role": n["role"], "node": name, "dc": dc}})
        elif n["role"] == "host":
            dc = f["nodes"][n["router"]].get("region") or ""
            sd.append({"targets": [f"{n['mgmt_ip']}:9100"], "labels": {"lab": LAB_NAME, "job": "node", "role": "host", "node": name, "dc": dc}})
    return sd


install_runs_api(app, registry, resume_factory)
app.mount("/results", StaticFiles(directory=str(RESULTS)), name="results")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
