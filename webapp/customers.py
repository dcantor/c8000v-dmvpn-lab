"""What the portal needs to know to add or remove a customer: the facts of the running lab, the next free values for a
new one, the checks that must pass before anything is built, and the plan for taking one away.

Allocation follows the lab's own conventions, so a suggested customer reads like the ones already there: custN,
index 10+N (which fixes 100.70.<idx>.0/30 into the provider, 172.28.0.<idx> in the overlay and 10.255.5.<idx> as
router-id), 10.5.0.2N for management, 192.168.6N.0/24 for the LAN, host-custN behind it at 10.5.0.4N."""
import ipaddress
import json
import re
import subprocess
from pathlib import Path

import labconf

LAB = Path(__file__).resolve().parents[1]
CUSTOMERS = LAB / "customers.json"
PLATFORMS = {"c8000v": "Catalyst 8000v", "vyos": "VyOS"}
COMPANY_FIELDS = ("company", "industry", "address", "phone", "contact", "email", "account")

# fictional companies for new customers: 555-01xx numbers are reserved for fiction, .example domains for documentation
_FAKE = [
    ("Northwind Veterinary Partners", "Healthcare — veterinary clinics", "410 Main Street, Burlington, VT 05401", "802", "Alex Rivera, Practice IT Lead"),
    ("Blue Mesa Credit Union", "Financial services — credit union", "5100 Montgomery Blvd NE, Albuquerque, NM 87109", "505", "Jordan Blake, Infrastructure Manager"),
    ("Lakeshore Precision Machining", "Manufacturing — CNC components", "7700 Lake Road, Erie, PA 16511", "814", "Sam Okafor, Plant Systems Engineer"),
    ("Redwood Coast Brewing Co.", "Food & beverage — brewery", "300 Harbor Way, Eureka, CA 95501", "707", "Casey Lindqvist, Operations IT"),
    ("Summit Ridge Property Management", "Real estate — property management", "1600 Broadway, Suite 900, Denver, CO 80202", "303", "Morgan Patel, IT Coordinator"),
    ("Gulfstream Marine Services", "Marine — boat maintenance", "2450 SE 17th Street, Fort Lauderdale, FL 33316", "954", "Taylor Nguyen, Systems Administrator"),
    ("Piedmont Family Pharmacy", "Healthcare — retail pharmacy", "215 North Tryon Street, Charlotte, NC 28202", "704", "Riley Johnson, Pharmacy Systems Lead"),
    ("Granite State Architects", "Professional services — architecture", "95 Elm Street, Manchester, NH 03101", "603", "Jamie Wexler, Office IT Manager"),
]


def companies():
    return {k: v for k, v in json.loads(CUSTOMERS.read_text()).items() if not k.startswith("_")} if CUSTOMERS.exists() else {}


def fake_company(n):
    company, industry, address, area, contact = _FAKE[(n - 1) % len(_FAKE)]
    slug = re.sub(r"[^a-z0-9]+", "", company.lower().split()[0] + company.lower().split()[1])
    return {"company": company, "industry": industry, "address": address, "phone": f"+1 ({area}) 555-01{(n * 17) % 100:02d}",
            "contact": contact, "email": f"it@{slug}.example", "account": f"C8D-{10400 + n * 73}"}


def inventory():
    return json.loads(subprocess.run([str(LAB / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)


def facts(inv=None):
    inv = inv or inventory()
    n = {x["name"]: x for x in inv["nodes"]}
    ports = [p for x in inv["nodes"] for p in x["ports"] if p.get("prefix")]
    return {"inv": inv, "nodes": n, "service": inv["service"], "provider": inv["provider"], "provider2": inv.get("provider2"),
            "hubs": list(inv["service"]["hubs"]), "customers": sorted(inv["service"]["spokes"], key=_num),
            "used_idx": {x["idx"] for x in inv["nodes"]},
            "used_console": {x["console"] for x in inv["nodes"]},
            "used_mgmt": {x["mgmt_ip"] for x in inv["nodes"]},
            "used_prefix": {p["prefix"] for p in ports},
            "used_t_idx": {x["t_idx"] for x in inv["nodes"] if x.get("t_idx")},
            "used_lan": {x["lan"] for x in inv["nodes"] if x.get("lan")}}


def _num(name):
    m = re.search(r"(\d+)$", name)
    return int(m[1]) if m else 0


def _free_provider_port(f, which="provider"):
    prov = f["nodes"][f[which]["nodes"][0]]
    free = [p["name"] for p in prov["ports"] if not p.get("peer") and 4 <= p["num"] <= 11]
    if not free:
        raise ValueError(f"{prov['name']} has no free customer port (eth4-eth11)")
    return prov["name"], free[0]


def dual_link(f, t, provider2=None, port=None):
    """A second-provider link for a customer with index t: the port (next free unless given) and the /30."""
    if not f.get("provider2"):
        raise ValueError("this lab has no second provider")
    if provider2 is None or port is None:
        provider2, port = _free_provider_port(f, "provider2")
    net = f["provider2"]["wan_net"]
    return {"provider2": provider2, "provider2_port": port, "wan2_prefix": f"{'.'.join(net.split('.')[:2])}.{t}.0/30"}


def suggest(f=None, region=None):
    """The next customer, every value the next free one. Nothing is created."""
    f = f or facts()
    n = max((_num(c) for c in f["customers"]), default=0) + 1
    t = 10 + n
    prov, pport = _free_provider_port(f)
    overlay = ipaddress.ip_network(f["service"]["overlay"])
    rid_net = f["nodes"][f["hubs"][0]]["router_id"].rsplit(".", 1)[0]
    wan_net = ipaddress.ip_network(f["provider"]["wan_net"])
    wan = f"{'.'.join(str(wan_net.network_address).split('.')[:2])}.{t}.0/30"
    return {"name": f"cust{n}", "host": f"host-cust{n}",
            "region": region or f["nodes"][f["hubs"][(n - 1) % len(f["hubs"])]]["region"],
            "mgmt_ip": f"10.5.0.{20 + n}", "host_mgmt": f"10.5.0.{40 + n}", "t_idx": t,
            "tunnel_ip": str(overlay.network_address + t), "nbma": str(ipaddress.ip_network(wan).network_address + 2),
            "router_id": f"{rid_net}.{t}", "lan": f"192.168.{60 + n}.0/24", "lan_port": "GigabitEthernet3",
            "provider": prov, "provider_port": pport, "wan_prefix": wan,
            "idx": 10 + n, "console": 5510 + n, "host_idx": 30 + n, "host_console": 5530 + n,
            "platform": "c8000v", "prefer_hub": None, "dual_homed": False,
            **({"provider2": None, "provider2_port": None, "wan2_prefix": None} if not f.get("provider2") else
               {k: v for k, v in _try(lambda: dual_link(f, t)).items()}),
            "customer": {**fake_company(n), "applications": ["APP-1002", "APP-1010"]}}   # email + SSO: what everyone takes


def _try(fn):
    try:
        return fn()
    except ValueError:
        return {"provider2": None, "provider2_port": None, "wan2_prefix": None}


def validate(spec, f=None):
    """Everything that must be true before a single VM is created. Returns a list of problems."""
    f = f or facts()
    p = []
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,19}", spec.get("name") or ""):
        p.append(f"{spec.get('name')!r} is not a usable name (lower case, 3-20 characters)")
    for key in ("name", "host"):
        if spec[key] in f["nodes"]:
            p.append(f"{spec[key]} already exists in this lab")
        elif _domain_exists(f["inv"]["nodes"][0]["domain"][: -len(f["inv"]["nodes"][0]["name"])] + spec[key]):
            p.append(f"a VM for {spec[key]} already exists on this host")
    for key, what in (("mgmt_ip", "management address"), ("host_mgmt", "host management address")):
        if spec[key] in f["used_mgmt"]:
            p.append(f"{what} {spec[key]} is already in use")
    if spec["t_idx"] in f["used_t_idx"]:
        p.append(f"index {spec['t_idx']} is taken — it fixes the WAN /30, the tunnel address and the router-id")
    if not 1 <= int(spec["t_idx"]) <= 253:
        p.append(f"index {spec['t_idx']} is outside the overlay")
    for key, used, what in (("idx", f["used_idx"], "node index"), ("host_idx", f["used_idx"], "host node index"),
                            ("console", f["used_console"], "console port"),
                            ("host_console", f["used_console"], "host console port")):
        if spec[key] in used:
            p.append(f"{what} {spec[key]} is taken")
    prov = f["nodes"].get(spec["provider"])
    if not prov or prov["role"] != "provider":
        p.append(f"{spec['provider']} is not the provider")
    else:
        port = next((x for x in prov["ports"] if x["name"] == spec["provider_port"]), None)
        if not port:
            p.append(f"{spec['provider']} has no port {spec['provider_port']}")
        elif port.get("peer"):
            p.append(f"{spec['provider']} {spec['provider_port']} is already wired to {port['peer']}")
    for key, want_len in (("wan_prefix", 30), ("lan", 24)):
        try:
            net = ipaddress.ip_network(spec[key])
            if net.prefixlen != want_len:
                p.append(f"{spec[key]} is not a /{want_len}")
            for other in f["used_prefix"] | f["used_lan"]:
                if net.overlaps(ipaddress.ip_network(other)):
                    p.append(f"{spec[key]} overlaps {other}")
        except ValueError as e:
            p.append(f"{spec[key]}: {e}")
    try:
        if not ipaddress.ip_network(spec["wan_prefix"]).subnet_of(ipaddress.ip_network(f["provider"]["wan_net"])):
            p.append(f"{spec['wan_prefix']} is outside the provider's range {f['provider']['wan_net']} — its listen "
                     "range would not accept the customer")
    except (ValueError, TypeError):
        pass
    cu = spec.get("customer") or {}
    for field in COMPANY_FIELDS:
        if not str(cu.get(field) or "").strip():
            p.append(f"customer {field} is missing")
    if spec.get("platform", "c8000v") not in PLATFORMS:
        p.append(f"router type {spec.get('platform')!r} is not one of {', '.join(PLATFORMS)}")
    if spec.get("prefer_hub") and spec["prefer_hub"] not in f["hubs"]:
        p.append(f"preferred hub {spec['prefer_hub']!r} is not a hub of this lab ({', '.join(f['hubs'])})")
    if spec.get("dual_homed"):
        p += _check_dual(spec, f)
    known = {a["id"] for a in f["inv"].get("applications") or []}
    for aid in cu.get("applications") or []:
        if aid not in known:
            p.append(f"{aid} is not an application of this lab")
    if cu.get("company") and any(v.get("company", "").lower() == cu["company"].strip().lower() for v in companies().values()):
        p.append(f"{cu['company']} is already a customer of this lab")
    return p


def _check_dual(spec, f, own=None):
    """A second-provider link: the provider, a free port on it, a /30 inside its range that overlaps nothing."""
    p = []
    if not f.get("provider2"):
        return ["this lab has no second provider — a customer cannot be dual-homed"]
    prov = f["nodes"].get(spec.get("provider2") or "")
    if not prov or prov["name"] not in f["provider2"]["nodes"]:
        return [f"{spec.get('provider2')} is not the second provider"]
    port = next((x for x in prov["ports"] if x["name"] == spec.get("provider2_port")), None)
    if not port:
        p.append(f"{prov['name']} has no port {spec.get('provider2_port')}")
    elif port.get("peer") and port.get("peer") != own:
        p.append(f"{prov['name']} {port['name']} is already wired to {port['peer']}")
    try:
        net = ipaddress.ip_network(spec.get("wan2_prefix") or "")
        if net.prefixlen != 30:
            p.append(f"{net} is not a /30")
        if not net.subnet_of(ipaddress.ip_network(f["provider2"]["wan_net"])):
            p.append(f"{net} is outside the second provider's range {f['provider2']['wan_net']}")
        mine = {(f["nodes"][own].get("wan2") or {}).get("prefix")} if own else set()
        for other in f["used_prefix"] - mine:
            if net.overlaps(ipaddress.ip_network(other)):
                p.append(f"{net} overlaps {other}")
    except ValueError as e:
        p.append(f"second provider link: {e}")
    return p


def _domain_exists(domain):
    r = subprocess.run(["sg", "libvirt", "-c", f"virsh -q -c qemu:///system dominfo {domain}"], capture_output=True, text=True)
    return r.returncode == 0


def plan(spec, f=None):
    """What adding this customer will do — shown before anything happens."""
    f = f or facts()
    return {"customer": spec["name"], "host": spec["host"], "region": spec["region"],
            "vm": (f"a new {PLATFORMS.get(spec.get('platform', 'c8000v'), '?')} router ({spec['mgmt_ip']}, console {spec['console']}, "
                   f"{'4 GB, 2 vCPU; first boot ~10 min' if spec.get('platform', 'c8000v') == 'c8000v' else '1 GB, 1 vCPU; first boot ~2 min'})"
                   f" and an Alpine host ({spec['host_mgmt']})"),
            "platform": spec.get("platform", "c8000v"),
            "company": f"{spec['customer']['company']} ({spec['customer']['industry']})" if spec.get("customer") else "",
            "cloud": f"Tunnel0 {spec['tunnel_ip']} sourced from {spec['nbma']}, registered with " + ", ".join(f["hubs"]),
            "wan": f"GigabitEthernet2 into {spec['provider']} {spec['provider_port']} on {spec['wan_prefix']}"
                   + (f"; and port 4 into {spec['provider2']} {spec['provider2_port']} on {spec['wan2_prefix']} "
                      "(dual-homed: Tunnel1 in the second cloud, the backup path)" if spec.get("dual_homed") else ""),
            "lan": f"{spec['lan']} on {spec['lan_port']}, with {spec['host']} behind it",
            "prefer": (f"prefers {spec['prefer_hub']}: its routes to the other customers go through {spec['prefer_hub']} "
                       "(local-preference 200) until the shortcut forms" if spec.get("prefer_hub") else
                       "no preferred hub: any of the hubs"),
            "untouched": "the hubs: a customer registers with NHRP and arrives on their BGP listen range, so no hub "
                         "configuration changes — Terraform adds resources on the new router only",
            "changed": [spec["provider"]] + ([spec["provider2"]] if spec.get("dual_homed") else [])}


def removal_plan(name, f=None):
    """What removing this customer will take away. Refuses the last one."""
    f = f or facts()
    if name not in f["nodes"] or f["nodes"][name]["role"] != "spoke":
        return [f"{name} is not a customer of this lab"], None
    if len(f["customers"]) <= 1:
        return ["this is the last customer — removing it would leave the cloud with none"], None
    n = f["nodes"][name]
    h = f["nodes"].get(n.get("host") or "")
    return [], {"name": name, "host": n["host"], "region": n.get("region"), "mgmt_ip": n["mgmt_ip"], "customer": n.get("customer"),
                "platform": n.get("platform", "c8000v"),
                "host_mgmt": h["mgmt_ip"] if h else None, "t_idx": n["t_idx"], "tunnel_ip": n["tunnel_ip"],
                "nbma": n["nbma"], "router_id": n["router_id"], "lan": n["lan"],
                "provider": n["wan"]["peer"], "provider_port": n["wan"]["peer_port"], "wan_prefix": n["wan"]["prefix"],
                "dual_homed": bool(n.get("wan2")), "provider2": (n.get("wan2") or {}).get("peer"),
                "provider2_port": (n.get("wan2") or {}).get("peer_port"), "wan2_prefix": (n.get("wan2") or {}).get("prefix"),
                "idx": n["idx"], "console": n["console"],
                "host_idx": h["idx"] if h else None, "host_console": h["console"] if h else None}


def _write_companies(update):
    doc = json.loads(CUSTOMERS.read_text()) if CUSTOMERS.exists() else {}
    update(doc)
    CUSTOMERS.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


def apply_to_labconf(spec):
    labconf.write(labconf.add_customer(labconf.read(), spec))
    cu = {f: str(spec["customer"][f]).strip() for f in COMPANY_FIELDS}
    cu["applications"] = sorted(set(spec["customer"].get("applications") or []))
    _write_companies(lambda d: d.__setitem__(spec["name"], cu))


def remove_from_labconf(spec):
    labconf.write(labconf.remove_customer(labconf.read(), spec))
    _write_companies(lambda d: d.pop(spec["name"], None))


# ---- modify a customer -----------------------------------------------------------------------------------------
MODIFIABLE = ("region", "lan", "platform", "prefer_hub", "dual_homed")


def current(name, f=None):
    """A customer as it is now, in the shape the modify dialog edits."""
    f = f or facts()
    n = f["nodes"].get(name)
    if not n or n["role"] != "spoke":
        return None
    h = f["nodes"].get(n.get("host") or "") or {}
    cu = dict(n.get("customer") or {})
    cu.setdefault("applications", [])
    return {"name": name, "host": n.get("host"), "region": n.get("region"), "lan": n["lan"], "platform": n.get("platform", "c8000v"),
            "prefer_hub": n.get("prefer_hub"), "mgmt_ip": n["mgmt_ip"], "host_mgmt": h.get("mgmt_ip"), "t_idx": n["t_idx"],
            "tunnel_ip": n["tunnel_ip"], "nbma": n["nbma"], "wan_prefix": n["wan"]["prefix"], "provider": n["wan"]["peer"],
            "provider_port": n["wan"]["peer_port"], "customer": cu,
            "dual_homed": bool(n.get("wan2")), "provider2": (n.get("wan2") or {}).get("peer"),
            "provider2_port": (n.get("wan2") or {}).get("peer_port"), "wan2_prefix": (n.get("wan2") or {}).get("prefix"),
            "has_provider2": bool(f.get("provider2"))}


def modify_plan(name, want, f=None):
    """What changing a customer takes. `want` holds the new values (anything left out stays as it is). Returns
    (problems, plan); the plan lists every change and the steps the run will take for them — a data-only change touches
    no router, a new preferred hub re-applies the router's policy, a new LAN re-addresses the router and rebuilds the
    host, a new router type rebuilds the router."""
    f = f or facts()
    old = current(name, f)
    if old is None:
        return [f"{name} is not a customer of this lab"], None
    new = {**old, **{k: want[k] for k in MODIFIABLE if k in want}}
    new["prefer_hub"] = new.get("prefer_hub") or None
    new["dual_homed"] = bool(new.get("dual_homed"))
    if new["dual_homed"] and not old["dual_homed"]:
        try:
            new.update(dual_link(f, old["t_idx"]))
        except ValueError as e:
            return [str(e)], None
    elif not new["dual_homed"]:
        new.update({"provider2": None, "provider2_port": None, "wan2_prefix": None})
    new["customer"] = {**old["customer"], **{k: v for k, v in (want.get("customer") or {}).items() if k in COMPANY_FIELDS + ("applications",)}}
    new["customer"]["applications"] = sorted(set(new["customer"].get("applications") or []))
    p = []
    if new["platform"] not in PLATFORMS:
        p.append(f"router type {new['platform']!r} is not one of {', '.join(PLATFORMS)}")
    if new["prefer_hub"] and new["prefer_hub"] not in f["hubs"]:
        p.append(f"preferred hub {new['prefer_hub']!r} is not a hub of this lab")
    if not str(new["region"] or "").strip():
        p.append("the region is missing")
    if new["lan"] != old["lan"]:
        try:
            net = ipaddress.ip_network(new["lan"])
            if net.prefixlen != 24:
                p.append(f"{new['lan']} is not a /24")
            if not net.subnet_of(ipaddress.ip_network("192.168.0.0/16")):
                p.append(f"{new['lan']} is outside 192.168.0.0/16 — the overlay only carries site LANs from there")
            for other in (f["used_prefix"] | f["used_lan"]) - {old["lan"]}:
                if net.overlaps(ipaddress.ip_network(other)):
                    p.append(f"{new['lan']} overlaps {other}")
        except ValueError as e:
            p.append(f"{new['lan']}: {e}")
    for field in COMPANY_FIELDS:
        if not str(new["customer"].get(field) or "").strip():
            p.append(f"customer {field} is missing")
    co = str(new["customer"].get("company") or "").strip().lower()
    if co and any(k != name and v.get("company", "").lower() == co for k, v in companies().items()):
        p.append(f"{new['customer']['company']} is already another customer of this lab")
    known = {a["id"] for a in f["inv"].get("applications") or []}
    p += [f"{aid} is not an application of this lab" for aid in new["customer"]["applications"] if aid not in known]

    changes = []
    if new["dual_homed"] and not old["dual_homed"]:
        p += _check_dual(new, f)
    for k, what in (("region", "region"), ("lan", "site LAN"), ("platform", "router type"), ("prefer_hub", "preferred hub"),
                    ("dual_homed", "second provider")):
        if new[k] != old[k]:
            fmt = ((lambda v: PLATFORMS.get(v, v)) if k == "platform" else
                   (lambda v, _n=new: f"dual-homed ({_n['provider2']} {_n['provider2_port']}, {_n['wan2_prefix']})" if v else "single-homed")
                   if k == "dual_homed" else (lambda v: v or "none"))
            changes.append({"field": k, "what": what, "old": fmt(old[k]), "new": fmt(new[k])})
    for k in COMPANY_FIELDS:
        if str(new["customer"].get(k) or "").strip() != str(old["customer"].get(k) or "").strip():
            changes.append({"field": f"customer.{k}", "what": k, "old": old["customer"].get(k), "new": new["customer"].get(k)})
    if new["customer"]["applications"] != sorted(set(old["customer"].get("applications") or [])):
        changes.append({"field": "customer.applications", "what": "applications",
                        "old": ", ".join(sorted(old["customer"].get("applications") or [])) or "none",
                        "new": ", ".join(new["customer"]["applications"]) or "none"})
    if not changes:
        p.append("nothing to change")
    rebuild = new["platform"] != old["platform"]
    relan = new["lan"] != old["lan"]
    rewire = new["dual_homed"] != old["dual_homed"] and not rebuild      # a rebuilt router is wired from scratch anyway
    routers = rebuild or relan or rewire or new["prefer_hub"] != old["prefer_hub"] or new["region"] != old["region"]
    what = []
    if rebuild:
        what.append(f"{name} is rebuilt as a {PLATFORMS.get(new['platform'], new['platform'])} router with the same identity (addresses, index, "
                    f"ports): the old VM is deleted, the new one boots ({'~10 min' if new['platform'] == 'c8000v' else '~2 min'}) and gets its day-0")
    if relan:
        what.append(f"{name}'s LAN becomes {new['lan']} (gateway .1); {old['host']} is rebuilt on it (.2) — its old address goes")
    if rewire:
        what.append(f"{name} is restarted with its port 4 " + (f"wired into {new['provider2']} {new['provider2_port']}" if new["dual_homed"]
                    else "unwired") + " (its configuration is saved first; a C8000v is back in ~5 min, a VyOS in ~1); "
                    + ("then Tunnel1 registers with every hub in the second cloud" if new["dual_homed"] else "its second cloud goes"))
    if new["prefer_hub"] != old["prefer_hub"] and not rebuild:
        what.append(f"{name}'s BGP policy is re-applied: " + (f"routes through {new['prefer_hub']} rank first" if new["prefer_hub"] else "no hub preferred"))
    if routers:
        what.append("Network-as-Code applied (C8000v) and the VyOS routers re-pushed; the hubs do not change")
    what.append("Nautobot " + ("re-modelled: the customer is taken out and seeded again" if rebuild or relan or rewire else "updated (seed)"))
    return p, {"name": name, "old": old, "new": new, "changes": changes, "rebuild": rebuild, "relan": relan, "rewire": rewire,
               "routers": routers, "what": what}


def apply_modify(plan):
    """lab.conf and customers.json: the new values."""
    o, n = plan["old"], plan["new"]
    labconf.write(labconf.modify_customer(labconf.read(), o["name"], o["host"], o, n))
    cu = {k: str(n["customer"].get(k) or "").strip() for k in COMPANY_FIELDS}
    cu["applications"] = n["customer"]["applications"]
    _write_companies(lambda d: d.__setitem__(o["name"], cu))
