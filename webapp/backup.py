"""Back up the whole lab state as one download, and restore it.

A backup is a .tar.gz:

  manifest.json          the lab, its version, when, the customers, a SHA-256 for every file
  intent/                lab.conf, customers.json, applications.json — everything the lab is built from
  renders/               every rendered file (the routers' day-0 / VyOS configurations, the NAC model), and the NAC
                         data and Terraform configuration it is applied with
  nautobot/model.json    this lab in Nautobot: the saved GraphQL query's answer (devices, interfaces, addresses,
                         cables, BGP, the config context), the customer tenants with their details, the applications'
                         Virtual Servers and the subscriptions
  running/               every router's running configuration at the time of the backup (evidence; not restored)
  terraform/             the Network-as-Code state (evidence; not restored — Terraform re-reads the routers)

Restoring puts the intent back and rebuilds the lab from it: the renders are regenerated and must match the backup's
byte for byte; customers the backup does not have are removed, customers it has and the lab does not are built,
customers that differ are changed in place (or rebuilt when their router type or LAN differs); then Network-as-Code,
the provider and Nautobot are brought to it, and the lab is verified. Nautobot is not written from the export: seed.py
re-creates the model from the restored intent, and the export is compared with the result."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import requests

LAB = Path(__file__).resolve().parents[1]
STORE = Path(__file__).resolve().parent / "backups"
STORE.mkdir(exist_ok=True)
INTENT = ("lab.conf", "customers.json", "applications.json")
FORMAT = 1


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _renders():
    files = sorted(p for p in (LAB / "nodes").glob("*/*_config.txt"))
    files += sorted((LAB / "nac" / "data").glob("*.yaml")) + [LAB / "nac" / "main.tf", LAB / "nac" / "versions.tf"]
    return [p for p in files if p.exists()]


def nautobot_export(inv):
    token = subprocess.run([str(LAB / "lab.sh"), "nautobot", "token"], capture_output=True, text=True, timeout=60).stdout.strip()
    url = os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080")
    H = {"Authorization": f"Token {token}", "Accept": "application/json"}
    lab = inv["lab"]
    q = requests.get(f"{url}/api/extras/graphql-queries/", params={"name": f"{lab}-model"}, headers=H, timeout=30).json()["results"]
    model = requests.post(f"{url}/api/graphql/", json={"query": q[0]["query"]}, headers=H, timeout=120).json() if q else None
    tenants = requests.get(f"{url}/api/tenancy/tenants/", params={"tenant_group": f"{lab} customers", "limit": 500}, headers=H,
                           timeout=30).json().get("results", [])
    vs = requests.get(f"{url}/api/load-balancers/virtual-servers/", params={"limit": 1000}, headers=H, timeout=30).json().get("results", [])
    vs = [v for v in vs if (v.get("custom_fields") or {}).get("application_id")]
    rel = requests.get(f"{url}/api/extras/relationships/", params={"key": "application_subscriptions"}, headers=H, timeout=30).json()["results"]
    subs = requests.get(f"{url}/api/extras/relationship-associations/", params={"relationship": rel[0]["key"], "limit": 2000},
                        headers=H, timeout=30).json().get("results", []) if rel else []
    keep = lambda o, ks: {k: o.get(k) for k in ks}                               # noqa: E731
    return {"url": url, "graphql": model,
            "tenants": [keep(t, ("id", "name", "description", "custom_fields")) for t in tenants],
            "virtual_servers": [{**keep(v, ("id", "name", "description", "custom_fields")),
                                 "vip": (v.get("vip") or {}).get("address") if isinstance(v.get("vip"), dict) else v.get("vip")} for v in vs],
            "subscriptions": [keep(a, ("source_id", "destination_id")) for a in subs]}


def running_configs(inv):
    import sys
    sys.path.insert(0, str(LAB / "tools"))
    import vyos_ssh
    from state import ios
    out = {}
    for n in inv["nodes"]:
        if n["role"] not in ("hub", "spoke", "provider"):
            continue
        try:
            if (n.get("platform") or "c8000v") == "vyos" or n["role"] == "provider":
                out[n["name"] + ".vyos.txt"] = vyos_ssh.op(n["mgmt_ip"], "show configuration commands")
            else:
                out[n["name"] + ".ios.txt"] = ios(n["mgmt_ip"], "show running-config")["show running-config"]
        except Exception as e:                                        # noqa: BLE001
            out[n["name"] + ".error.txt"] = f"{e.__class__.__name__}: {e}\n"
    return out


def create(inv, version, with_running=True):
    """Build the archive in memory; returns (bytes, manifest)."""
    files = {}
    for f in INTENT:
        if (LAB / f).exists():
            files[f"intent/{f}"] = (LAB / f).read_bytes()
    for p in _renders():
        files["renders/" + str(p.relative_to(LAB))] = p.read_bytes()
    notes = []
    try:
        files["nautobot/model.json"] = json.dumps(nautobot_export(inv), indent=1).encode()
    except Exception as e:                                            # noqa: BLE001
        notes.append(f"Nautobot export failed: {e.__class__.__name__}: {e}")
    if with_running:
        for name, text in running_configs(inv).items():
            files[f"running/{name}"] = text.encode()
    tf = LAB / "nac" / "terraform.tfstate"
    if tf.exists():
        files["terraform/terraform.tfstate"] = tf.read_bytes()
    spokes = inv["service"]["spokes"]
    nodes = {n["name"]: n for n in inv["nodes"]}
    manifest = {"format": FORMAT, "lab": inv["lab"], "version": version, "created": time.time(),
                "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": os.uname().nodename,
                "customers": {c: {"platform": nodes[c].get("platform"), "lan": nodes[c].get("lan"), "region": nodes[c].get("region"),
                                  "company": (nodes[c].get("customer") or {}).get("company"), "prefer_hub": nodes[c].get("prefer_hub")}
                              for c in spokes},
                "hubs": inv["service"]["hubs"], "notes": notes,
                "files": {k: {"sha256": _sha(v), "bytes": len(v)} for k, v in sorted(files.items())}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data):
            ti = tarfile.TarInfo(f"{inv['lab']}-backup/{name}")
            ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
            tar.addfile(ti, io.BytesIO(data))
        add("manifest.json", json.dumps(manifest, indent=1).encode())
        for k, v in sorted(files.items()):
            add(k, v)
    return buf.getvalue(), manifest


# ---- restore ---------------------------------------------------------------------------------------------------------
def read_archive(data):
    """-> (manifest, {path: bytes}); raises ValueError if the archive is not a backup of this lab or is damaged."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"not a .tar.gz: {e}")
    files = {}
    with tar:
        for m in tar.getmembers():
            if not m.isfile() or ".." in Path(m.name).parts or m.name.startswith("/"):
                continue
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            files[rel] = tar.extractfile(m).read()
    if "manifest.json" not in files:
        raise ValueError("no manifest.json: not a backup made by this portal")
    man = json.loads(files.pop("manifest.json"))
    if man.get("format") != FORMAT:
        raise ValueError(f"backup format {man.get('format')} is not supported (this portal reads format {FORMAT})")
    bad = [k for k, meta in man["files"].items() if k not in files or _sha(files[k]) != meta["sha256"]]
    if bad:
        raise ValueError("damaged: " + ", ".join(bad[:6]) + (" …" if len(bad) > 6 else ""))
    for f in INTENT[:2]:
        if f"intent/{f}" not in files:
            raise ValueError(f"the backup has no {f}")
    return man, files


def inventory_of(files):
    """The inventory the backup's intent describes: `lab.sh inventory` run against a scratch copy holding its lab.conf."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        shutil.copy(LAB / "lab.sh", d / "lab.sh")
        (d / "tools").mkdir()
        shutil.copy(LAB / "tools" / "inventory.py", d / "tools" / "inventory.py")
        for f in INTENT:
            if f"intent/{f}" in files:
                (d / f).write_bytes(files[f"intent/{f}"])
        p = subprocess.run([str(d / "lab.sh"), "inventory"], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise ValueError("the backup's lab.conf does not read: " + p.stderr[-400:])
        return json.loads(p.stdout)


def save_upload(data):
    man, files = read_archive(data)
    uid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    (STORE / f"{uid}.tar.gz").write_bytes(data)
    return uid, man, files


def load_upload(uid):
    p = STORE / f"{uid}.tar.gz"
    if not p.exists() or "/" in uid:
        raise ValueError(f"no uploaded backup {uid}")
    return read_archive(p.read_bytes())


def plan(cur_inv, files):
    """What restoring changes: customers removed, added, changed (in place or rebuilt), and the other differences."""
    want = inventory_of(files)
    cn = {n["name"]: n for n in cur_inv["nodes"]}
    wn = {n["name"]: n for n in want["nodes"]}
    cur_c, want_c = set(cur_inv["service"]["spokes"]), set(want["service"]["spokes"])
    removed, added = sorted(cur_c - want_c), sorted(want_c - cur_c)
    changed = []
    for c in sorted(cur_c & want_c):
        a, b = cn[c], wn[c]
        diff = {k: (a.get(k), b.get(k)) for k in ("platform", "lan", "region", "prefer_hub", "mgmt_ip", "t_idx")
                if a.get(k) != b.get(k)}
        if (a.get("customer") or {}) != (b.get("customer") or {}):
            diff["company / applications"] = ("", "")
        if a["wan"]["prefix"] != b["wan"]["prefix"] or a["wan"]["peer_port"] != b["wan"]["peer_port"]:
            diff["provider link"] = (f"{a['wan']['peer_port']} {a['wan']['prefix']}", f"{b['wan']['peer_port']} {b['wan']['prefix']}")
        if diff:
            identity = any(k in diff for k in ("mgmt_ip", "t_idx", "provider link"))
            changed.append({"name": c, "diff": {k: [str(x or ""), str(y or "")] for k, (x, y) in diff.items()},
                            "rebuild": "platform" in diff or identity, "relan": "lan" in diff or identity,
                            "identity": identity})
    other = []
    if cur_inv["service"]["hubs"] != want["service"]["hubs"]:
        other.append("the hubs differ: " + " → ".join([", ".join(cur_inv["service"]["hubs"]), ", ".join(want["service"]["hubs"])]))
    for k in ("as", "overlay", "network_id", "tunnel_key", "mtu", "mss", "keepalive", "holdtime_bgp", "holdtime"):
        if cur_inv["service"].get(k) != want["service"].get(k):
            other.append(f"DMVPN {k}: {cur_inv['service'].get(k)} → {want['service'].get(k)}")
    same_intent = all((LAB / f).exists() and (LAB / f).read_bytes() == files.get(f"intent/{f}", b"") for f in INTENT if f"intent/{f}" in files)
    problems = []
    if cur_inv["service"]["hubs"] != want["service"]["hubs"]:
        problems.append("the backup has different hubs; restoring hubs is not supported — rebuild the lab from its lab.conf instead")
    if cur_inv["provider"]["nodes"] != want["provider"]["nodes"]:
        problems.append("the backup has a different provider")
    return problems, {"removed": removed, "added": added, "changed": changed, "other": other, "same_intent": same_intent,
                      "want": {"customers": sorted(want_c), "nodes": sorted(wn)}}
