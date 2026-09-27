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
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from labportal import RunBase, RunRegistry, exposition, install_runs_api, metric_line, metrics_generated, run_metrics
from pydantic import BaseModel, Field

import customers as C
import backup as B
import drift as D
import sla as SLA
from state import HOST_PASS, HOST_USER, State, _ssh, ios, vtysh, vyos_op, vyos_show

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
    "rm_vm": "Power off and delete the router and its host",
    "rm_labconf": "Remove from lab.conf and re-render",
    "rm_provider": "Release the provider port (address removed, port disabled)",
    "rm_nautobot": "Remove the customer from Nautobot",
    "mod_validate": "Validate the changes against the running lab",
    "mod_labconf": "Record the changes in lab.conf and customers.json, and re-render",
    "mod_forget": "Terraform forgets the old router (its resources leave the state; the hubs are not touched)",
    "mod_router": "Rebuild the router: delete the old VM, boot the new one with the same identity, day-0",
    "mod_host": "Rebuild the LAN host on the new LAN",
    "mod_nautobot": "Nautobot source of truth: update the customer",
    "drift": "Compare every router's running configuration with the model (Nautobot, Network-as-Code, the renders)",
    "fixcli": "The CLI templates: undo lines the model does not have, and mark drifted templates to be written again",
    "rs_validate": "Validate the backup and plan the restore against the running lab",
    "rs_remove": "Take away what the backup does not have (customers, routers to rebuild, hosts to re-address)",
    "rs_files": "Put the backup's intent back (lab.conf, customers.json, applications.json) and re-render",
    "rs_build": "Build what the backup has and the lab does not: boot the VMs, day-0",
    "rs_nautobot": "Nautobot source of truth: seed from the restored intent",
    "render": "Render the configuration from lab.conf",
    "plan": "terraform plan: what Network-as-Code would change",
    "check": "Compare Nautobot's rendering with lab.conf's",
}
TAGS = [{"name": "monitoring", "description": "Prometheus: /metrics and /api/sd, scraped by the NMS."},
        {"name": "state", "description": "The cloud, the model and the live state of every router."},
        {"name": "provisioning", "description": "Suggest, validate and plan a customer; plan a removal."},
        {"name": "runs", "description": "Pipeline runs: add, modify or remove a customer, deploy, plan, test, drift, restore."},
        {"name": "backup", "description": "Back up the whole lab state as one download; upload one to restore it."}]
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
    customer: dict = Field(default_factory=dict, description="the company: company, industry, address, phone, contact, email, account")
    platform: str = Field("c8000v", description="the customer router: c8000v (Catalyst 8000v) or vyos")
    prefer_hub: str | None = Field(None, description="the hub its traffic prefers (BGP local-preference 200), or none")


class CustomerChange(BaseModel):
    """The new values for an existing customer; anything left out stays as it is."""
    region: str | None = None
    lan: str | None = Field(None, examples=["192.168.81.0/24"])
    platform: str | None = Field(None, description="c8000v or vyos: a different one rebuilds the router")
    prefer_hub: str | None = Field(None, description="a hub's name, or empty for none")
    customer: dict | None = Field(None, description="company fields and/or `applications`")


class RunRequest(BaseModel):
    mode: str = Field(examples=["customer"], description="customer | remove | modify | deploy | plan | test | drift | fixdrift | restore")
    customer: CustomerSpec | None = None
    name: str | None = Field(None, description="remove / modify: the customer; restore: the uploaded backup's id")
    changes: CustomerChange | None = Field(None, description="modify: the new values")
    options: dict = Field(default_factory=dict, description="{test: bool (default true), suites: [..]}")


class Run(RunBase):
    LAB = "c8000v-dmvpn-lab"
    STEP_TITLES = STEP_TITLES
    EXTRA = {"customer": "customer", "spec": "spec", "removal": "removal", "modification": "modification", "drift": "drift",
             "restore": "restore"}

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
        if self.mode == "test":
            return ["test"]
        if self.mode == "plan":
            return ["plan", "check"]
        if self.mode == "drift":
            return ["drift"]
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
            if m["routers"]:
                steps += ["nac", "provider"]
            steps += ["mod_nautobot", "verify"]
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

    def do_mod_nautobot(self, s):
        m, o = self.modification, self.modification["old"]
        if m["rebuild"] or m["relan"]:
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", o["name"], "--host", o["host"], "--wan", o["wan_prefix"], "--lan", o["lan"]])
        self.sh([LAB / "lab.sh", "nautobot", "seed"])
        s["summary"] = "re-modelled and seeded" if m["rebuild"] or m["relan"] else "seeded"

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
            tpl = {i["where"] for i in r["items"] if i.get("where", "").startswith(("tunnel0_", "bgp_hub_"))}
            self._replace += [f'module.iosxe.iosxe_cli.cli_0["{name}/{t}"]' for t in sorted(tpl)]
            extra = [i["line"] for i in r["items"] if i["kind"] == "extra" and i.get("where", "").startswith("tunnel0_")]
            if extra:
                from netmiko import ConnectHandler
                c = ConnectHandler(device_type="cisco_xe", host=nodes[name]["mgmt_ip"], username="admin", password="admin", fast_cli=False)
                try:
                    self.say(c.send_config_set(["interface Tunnel0", *[f"no {l}" for l in extra]]))
                finally:
                    c.disconnect()
                undone.append(f"{name}: {len(extra)} line(s) removed")
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
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", c, "--host", cur["host"], "--wan", cur["wan_prefix"], "--lan", cur["lan"]])
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
            self.sh([LAB / "lab.sh", "nautobot", "remove-customer", c, "--host", cur["host"], "--wan", cur["wan_prefix"], "--lan", cur["lan"]])
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
        s["summary"] = f"{self.removal['provider']} {self.removal['provider_port']} released"

    def do_rm_nautobot(self, s):
        r = self.removal
        self.sh([LAB / "lab.sh", "nautobot", "remove-customer", r["name"], "--host", r["host"], "--wan", r["wan_prefix"],
                 "--lan", r["lan"]])
        s["summary"] = f"{r['name']} and {r['host']} removed from the model"


def resume_factory(rec):
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
    return {"node": node, "command": command, "output": out}


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
                page = browser.new_page(viewport={"width": 1400, "height": 1000})
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
    return {"node": node, "role": n["role"], "command": cmd, "lines": out.count("\n") + 1, "output": out}


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


@app.post("/api/runs", tags=["runs"], summary="Start a run")
def api_run(req: RunRequest):
    if req.mode not in ("customer", "remove", "modify", "deploy", "plan", "test", "drift", "fixdrift", "restore"):
        raise HTTPException(400, f"unknown mode {req.mode}")
    spec = req.customer.model_dump() if req.customer else ({"name": req.name} if req.name else None)
    if req.mode == "modify":
        if not req.name or not req.changes:
            raise HTTPException(400, "mode modify needs a name and changes")
        spec = {"name": req.name, "changes": req.changes.model_dump(exclude_none=True)}
    if req.mode in ("modify", "restore"):
        if not (spec or {}).get("name"):
            raise HTTPException(400, f"mode {req.mode} needs a name")
        try:
            return registry.start(Run(req.mode, spec, req.options))
        except ValueError as e:
            raise HTTPException(400, str(e))
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
    out += prober.metrics(L, metric_line)
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
        {"group": "This lab", "name": "Provisioning portal", "url": f"http://{pub}:{WEBAPP_PORT}", "login": "none",
         "what": "the cloud, the Network map, provisioning runs"},
        {"group": "This lab", "name": "Portal API (Swagger)", "url": f"http://{pub}:{WEBAPP_PORT}/docs", "login": "none",
         "what": "every endpoint the portal offers; /metrics and /api/sd for Prometheus"},
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
