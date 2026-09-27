"""Configuration drift: does every router run what the model says it should?

The model is lab.conf rendered by tools/render.py, and Nautobot must render the same thing. The check has three parts:

  the model       `lab.sh nautobot render --check`: Nautobot's rendering against lab.conf's, file by file
  C8000v          `terraform plan` of the Network-as-Code model: every resource Terraform would create, change or
                  destroy is drift on the router it belongs to. The CLI templates (Tunnel0, a hub's BGP listen range)
                  are write-only to Terraform, so their lines are compared with `show running-config` directly.
  VyOS            the rendered `set` lines against `show configuration commands`: a line the model has and the router
                  does not is missing; a line the router has in a section the model owns (policy, BGP, NHRP, static
                  routes, IPsec, tunnel and dummy interfaces, data-port addresses) and the model does not is extra.

Nothing is changed. The report is kept in webapp/drift/latest.json; the portal shows it on the map and exports it."""
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import vyos_ssh  # noqa: E402

from state import ios  # noqa: E402

OUT = Path(__file__).resolve().parent / "drift"
OUT.mkdir(exist_ok=True)
LATEST = OUT / "latest.json"

# sections of a VyOS router the model owns completely (a line there that the model lacks is drift)
VYOS_OWNED = ("set policy ", "set protocols bgp ", "set protocols nhrp ", "set protocols static ", "set vpn ipsec ",
              "set interfaces tunnel ", "set interfaces dummy ")
VYOS_OWNED_RE = re.compile(r"^set interfaces ethernet eth([1-9]\d*) (address|disable)\b")   # the data ports' addressing
# rendered lines a router never shows back: a hashed password, and valueless nodes that only appear through children
VYOS_IGNORE = (re.compile(r"^set system login user \S+ authentication plaintext-password"),)
# IOS defaults: rendered for the reader, but `show running-config` does not print a default
IOS_DEFAULTS = {"ip nhrp shortcut", "ip nhrp map multicast dynamic"}


def _norm(line):
    return re.sub(r"\s+", " ", line.replace("'", "").strip())


def vyos_drift(node, ip, rendered_file):
    want = [_norm(l) for l in Path(rendered_file).read_text().splitlines() if l.startswith("set ")]
    running_text = vyos_ssh.op(ip, "show configuration commands", timeout=120)
    if "set system host-name" not in running_text:
        raise RuntimeError("could not read the running configuration")
    have = [_norm(l) for l in running_text.splitlines() if l.startswith("set ")]
    have_set, want_set = set(have), set(want)
    items = []
    for w in want:
        if any(rx.match(w) for rx in VYOS_IGNORE):
            continue
        if w not in have_set and not any(h.startswith(w + " ") for h in have):
            items.append({"kind": "missing", "line": w})
    for h in have:
        if (h.startswith(VYOS_OWNED) or VYOS_OWNED_RE.match(h)) and h not in want_set:
            items.append({"kind": "extra", "line": h})
    return items


def c8k_template_drift(ip, templates):
    """The CLI templates' lines against the running configuration of the sections they write."""
    out = ios(ip, "show running-config interface Tunnel0", "show running-config | section router bgp")
    tun = out["show running-config interface Tunnel0"]
    bgp = out["show running-config | section router bgp"]
    items = []
    for name, content in templates.items():
        running = tun if name.startswith("tunnel0_") else bgp
        have = {l.strip() for l in running.splitlines()}
        want = [l.strip() for l in content.splitlines() if l.strip()]
        for w in want:
            if w not in have and w not in IOS_DEFAULTS:
                items.append({"kind": "missing", "line": w, "where": name})
        if name.startswith("tunnel0_"):              # Tunnel0 is the template's alone: anything else there is drift
            wset = set(want)
            for l in running.splitlines():
                if l.startswith(" ") and l.strip() not in wset:
                    items.append({"kind": "extra", "line": l.strip(), "where": name})
    return items


PLAN_RE = re.compile(r"^\s*# (module\.\S+?)\[\"([^\"]+)\"\] (will be (created|updated in-place|destroyed)|must be replaced)", re.M)


def terraform_drift(say):
    """terraform plan against the routers; returns ({device: [items]}, rc)."""
    subprocess.run([str(LAB / "lab.sh"), "nac", "init", "-input=false", "-no-color"], capture_output=True, text=True)
    p = subprocess.run([str(LAB / "lab.sh"), "nac", "plan", "-detailed-exitcode", "-no-color", "-input=false", "-lock=false",
                        "-refresh=true"], capture_output=True, text=True, timeout=1800)
    if p.returncode == 1:
        raise RuntimeError("terraform plan failed: " + (p.stderr or p.stdout)[-600:])
    per = {}
    for m in PLAN_RE.finditer(p.stdout):
        addr, key, verdict = m[1], m[2], m[3]
        device = key.split("/")[0]
        kind = {"will be created": "missing", "will be destroyed": "extra"}.get(verdict, "changed")
        per.setdefault(device, []).append({"kind": kind, "line": f"{addr.split('.')[-1]}[{key}] {verdict}", "where": "terraform"})
    say(f"terraform plan: exit {p.returncode}, {sum(len(v) for v in per.values())} resource(s) would change")
    return per, p.returncode


def model_drift(say):
    """Nautobot's rendering against lab.conf's."""
    p = subprocess.run([str(LAB / "lab.sh"), "nautobot", "render", "--check"], capture_output=True, text=True, timeout=600)
    differs = re.findall(r"^(\S+): DIFFERS", p.stdout, re.M)
    if p.returncode not in (0, 1) or ("Nautobot == lab.conf" not in p.stdout and not differs):
        raise RuntimeError("nautobot render --check failed: " + (p.stderr or p.stdout)[-400:])
    say(f"Nautobot vs lab.conf: {len(differs)} file(s) differ")
    return differs


def check(inv, say=print):
    """The whole check; returns the report (and writes it to drift/latest.json)."""
    t0 = time.time()
    nodes = {n["name"]: n for n in inv["nodes"]}
    routers = [n for n in inv["nodes"] if n["role"] in ("hub", "spoke", "provider")]
    report = {"generated": None, "took": None, "nodes": {}, "model": {"differs": [], "error": None}, "errors": []}
    for n in routers:
        report["nodes"][n["name"]] = {"role": n["role"], "platform": n.get("platform") or "c8000v", "items": [], "error": None,
                                      "checked": []}
    try:
        report["model"]["differs"] = model_drift(say)
    except Exception as e:                                         # noqa: BLE001
        report["model"]["error"] = str(e)
    for rel in report["model"]["differs"]:
        m = re.match(r"nodes/([^/]+)/", rel)
        if m and m[1] in report["nodes"]:
            report["nodes"][m[1]]["items"].append({"kind": "model", "line": f"Nautobot renders {rel} differently from lab.conf", "where": "nautobot"})

    doc = yaml.safe_load((LAB / "nac" / "data" / "devices.nac.yaml").read_text())
    templates = {t["name"]: t["content"] for t in doc["iosxe"].get("templates") or []}

    def one(n):
        name, r = n["name"], report["nodes"][n["name"]]
        try:
            if r["platform"] == "vyos":
                r["items"] += vyos_drift(name, n["mgmt_ip"], LAB / "nodes" / name / "vyos_config.txt")
                r["checked"].append("show configuration commands vs the rendered set lines")
            else:
                mine = {k: v for k, v in templates.items() if k.endswith("_" + name)}
                r["items"] += c8k_template_drift(n["mgmt_ip"], mine)
                r["checked"].append("show running-config vs the CLI templates (" + ", ".join(sorted(mine)) + ")")
        except Exception as e:                                     # noqa: BLE001
            r["error"] = f"{e.__class__.__name__}: {e}"[:300]

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, routers))
    say("read every router's running configuration")
    try:
        per, _ = terraform_drift(say)
        for device, items in per.items():
            if device in report["nodes"]:
                report["nodes"][device]["items"] += items
            else:
                report["errors"].append(f"terraform would change {device}, which is not a router of this lab")
        for n in routers:
            if report["nodes"][n["name"]]["platform"] == "c8000v":
                report["nodes"][n["name"]]["checked"].append("terraform plan (Network-as-Code)")
    except Exception as e:                                         # noqa: BLE001
        report["errors"].append(str(e)[:600])
    for name, r in report["nodes"].items():
        r["status"] = "error" if r["error"] else ("drift" if r["items"] else "ok")
        r["items"] = r["items"][:80]
    report["generated"], report["took"] = time.time(), round(time.time() - t0, 1)
    report["drifted"] = sorted(n for n, r in report["nodes"].items() if r["status"] == "drift")
    report["ok"] = not report["drifted"] and not report["errors"] and not report["model"]["error"] \
        and not report["model"]["differs"] and all(r["status"] == "ok" for r in report["nodes"].values())
    LATEST.write_text(json.dumps(report, indent=1))
    return report


def latest():
    try:
        return json.loads(LATEST.read_text())
    except (OSError, ValueError):
        return None
