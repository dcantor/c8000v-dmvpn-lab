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
    return {"inv": inv, "nodes": n, "service": inv["service"], "provider": inv["provider"],
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


def _free_provider_port(f):
    prov = f["nodes"][f["provider"]["nodes"][0]]
    free = [p["name"] for p in prov["ports"] if not p.get("peer") and 4 <= p["num"] <= 11]
    if not free:
        raise ValueError(f"{prov['name']} has no free customer port (eth4-eth11)")
    return prov["name"], free[0]


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
            "customer": fake_company(n)}


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
    if cu.get("company") and any(v.get("company", "").lower() == cu["company"].strip().lower() for v in companies().values()):
        p.append(f"{cu['company']} is already a customer of this lab")
    return p


def _domain_exists(domain):
    r = subprocess.run(["sg", "libvirt", "-c", f"virsh -q -c qemu:///system dominfo {domain}"], capture_output=True, text=True)
    return r.returncode == 0


def plan(spec, f=None):
    """What adding this customer will do — shown before anything happens."""
    f = f or facts()
    return {"customer": spec["name"], "host": spec["host"], "region": spec["region"],
            "vm": f"a new Catalyst 8000v ({spec['mgmt_ip']}, console {spec['console']}) and an Alpine host ({spec['host_mgmt']})",
            "company": f"{spec['customer']['company']} ({spec['customer']['industry']})" if spec.get("customer") else "",
            "cloud": f"Tunnel0 {spec['tunnel_ip']} sourced from {spec['nbma']}, registered with " + ", ".join(f["hubs"]),
            "wan": f"GigabitEthernet2 into {spec['provider']} {spec['provider_port']} on {spec['wan_prefix']}",
            "lan": f"{spec['lan']} on {spec['lan_port']}, with {spec['host']} behind it",
            "untouched": "the hubs: a customer registers with NHRP and arrives on their BGP listen range, so no hub "
                         "configuration changes — Terraform adds resources on the new router only",
            "changed": [spec["provider"]]}


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
                "host_mgmt": h["mgmt_ip"] if h else None, "t_idx": n["t_idx"], "tunnel_ip": n["tunnel_ip"],
                "nbma": n["nbma"], "router_id": n["router_id"], "lan": n["lan"],
                "provider": n["wan"]["peer"], "provider_port": n["wan"]["peer_port"], "wan_prefix": n["wan"]["prefix"],
                "idx": n["idx"], "console": n["console"],
                "host_idx": h["idx"] if h else None, "host_console": h["console"] if h else None}


def _write_companies(update):
    doc = json.loads(CUSTOMERS.read_text()) if CUSTOMERS.exists() else {}
    update(doc)
    CUSTOMERS.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")


def apply_to_labconf(spec):
    labconf.write(labconf.add_customer(labconf.read(), spec))
    cu = {f: str(spec["customer"][f]).strip() for f in COMPANY_FIELDS}
    _write_companies(lambda d: d.__setitem__(spec["name"], cu))


def remove_from_labconf(spec):
    labconf.write(labconf.remove_customer(labconf.read(), spec))
    _write_companies(lambda d: d.pop(spec["name"], None))
