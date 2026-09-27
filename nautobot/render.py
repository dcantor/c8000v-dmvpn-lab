#!/usr/bin/env python3
"""Render the lab's configuration from Nautobot (the source of truth) with the same renderer lab.conf uses.

Reads the saved GraphQL query `c8000v-dmvpn-lab-model`, rebuilds the inventory — devices, interfaces, addresses,
cables, BGP routing instances and the config context — and hands it to tools/render.py. If Nautobot and lab.conf
disagree about anything the renderer reads, `--check` says which file and which line, and exits 1.

   render.py                 print every rendered file
   render.py --check         exit 1 if Nautobot's rendering differs from the committed files (lab.conf's)
   render.py --write         write the files — i.e. make Nautobot the producer
   render.py --inventory     dump the rebuilt inventory as JSON"""
import argparse
import difflib
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
from render import render_all   # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--url", default=os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"))
p.add_argument("--token", default=os.environ.get("NAUTOBOT_TOKEN"))
p.add_argument("--check", action="store_true")
p.add_argument("--write", action="store_true")
p.add_argument("--inventory", action="store_true")
a = p.parse_args()
H = {"Authorization": f"Token {a.token}", "Accept": "application/json"}
QUERY_NAME = "c8000v-dmvpn-lab-model"
ROLE = {"dmvpn-hub": "hub", "dmvpn-spoke": "spoke", "wan-provider": "provider", "host": "host"}
PLATFORM = {"cisco_xe": "c8000v", "vyos": "vyos", "alpine": "alpine", "linux": "alpine"}
# the pre-shared key and the NHRP secret are secrets: lab.conf keeps them, Nautobot does not
SECRETS = json.loads(subprocess.run(
    ["bash", "-c", f'source {LAB}/lab.conf; printf \'{{"psk": "%s", "nhrp_secret": "%s", "lan_port": %s}}\' '
                   '"$DMVPN_PSK" "$NHRP_SECRET" "$LAN_PORT"'], capture_output=True, text=True, check=True).stdout)


def gql(query):
    r = requests.post(f"{a.url}/api/graphql/", json={"query": query}, headers=H, timeout=120)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        sys.exit(f"GraphQL: {body['errors']}")
    return body["data"]


def num(name):
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def inventory_from_nautobot():
    q = requests.get(f"{a.url}/api/extras/graphql-queries/", params={"name": QUERY_NAME}, headers=H, timeout=30).json()["results"]
    if not q:
        sys.exit(f"saved GraphQL query {QUERY_NAME} not found — run ./lab.sh nautobot seed")
    d = gql(q[0]["query"])
    if not d["config_contexts"]:
        sys.exit("config context c8000v-dmvpn-lab not found — run ./lab.sh nautobot seed")
    ctx = d["config_contexts"][0]["data"]
    pre = ctx["domain_prefix"]
    short = lambda name: name[len(pre):] if name.startswith(pre) else name   # noqa: E731
    overlay = ipaddress.ip_network(ctx["dmvpn"]["overlay"])
    addr1 = lambda i: (i["ip_addresses"] or [{"address": None}])[0]["address"]   # noqa: E731

    P1 = ctx["provider"]["nodes"][0]
    P2 = (ctx.get("provider2") or {}).get("nodes", [None])[0]          # the second provider, if the lab has one
    nodes = []
    for x in sorted(d["devices"], key=lambda x: x["name"]):
        name, role = short(x["name"]), ROLE[x["role"]["name"]]
        c8k = role in ("hub", "spoke")                     # carries the cloud (C8000v or VyOS)
        platform = PLATFORM.get((x.get("platform") or {}).get("name"), "c8000v")
        oob = next(i for i in x["interfaces"] if i["mgmt_only"])
        idx = int(oob["mac_address"].split(":")[4], 16)      # MACs are <oui>:<idx>:<port>
        ifaces = {i["name"]: i for i in x["interfaces"]}
        data_ports = sorted((i for i in x["interfaces"] if not i["mgmt_only"] and i["type"] != "VIRTUAL"
                             and (i["name"].startswith("GigabitEthernet") or i["name"].startswith("eth"))),
                            key=lambda i: num(i["name"]))
        ports = []
        for i in data_ports:
            ci = i.get("connected_interface")
            if not ci or not i["ip_addresses"]:
                ports.append({"name": i["name"], "num": num(i["name"]), "ip": None, "prefix": None, "peer": None})
                continue
            parent = i["ip_addresses"][0].get("parent")
            ports.append({"name": i["name"], "num": num(i["name"]), "ip": addr1(i),
                          "prefix": parent["prefix"] if parent else None, "peer": short(ci["device"]["name"]),
                          "peer_port": ci["name"], "peer_role": ROLE[ci["device"]["role"]["name"]],
                          "peer_ip": addr1(ci).split("/")[0] if ci["ip_addresses"] else None})
        ri = (x["bgp_routing_instances"] or [None])[0]
        loc = x["location"]["name"]
        node = {"name": name, "role": role, "platform": platform, "domain": x["name"],
                "region": loc.split("-", 1)[1].capitalize() if loc.startswith("c8d-") else None,
                "mgmt_ip": x["primary_ip4"]["address"].split("/")[0], "idx": idx, "ports": ports}
        if c8k:
            wan = next(p for p in ports if p.get("peer") == P1)
            wan2 = next((p for p in ports if p.get("peer") == P2), None) if P2 else None
            tun = addr1(ifaces.get("Tunnel0") or ifaces["tun0"]).split("/")[0]
            t1 = ifaces.get("Tunnel1") or ifaces.get("tun1")
            tun2 = addr1(t1).split("/")[0] if wan2 and t1 else None
            lan_port = next((p for p in ports if p["num"] == SECRETS["lan_port"] and p["ip"]), None)
            lan = (lan_port["prefix"] if lan_port else
                   str(ipaddress.ip_interface(addr1(ifaces["Loopback10"])).network))
            if role == "spoke":
                node["prefer_hub"] = ((ri or {}).get("extra_attributes") or {}).get("preferred_hub")
            node.update({"t_idx": int(ipaddress.ip_address(tun)) - int(overlay.network_address),
                         "router_id": ri["router_id"]["address"].split("/")[0], "tunnel_ip": tun,
                         "nbma": wan["ip"].split("/")[0], "wan": wan, "lan": lan,
                         "host": lan_port["peer"] if lan_port else None, "lan_port": lan_port,
                         "wan2": wan2, "nbma2": wan2["ip"].split("/")[0] if wan2 else None, "tunnel2_ip": tun2})
        elif role == "provider":
            node["router_id"] = ri["router_id"]["address"].split("/")[0]
        nodes.append(node)

    svc = {**ctx["dmvpn"], "psk": SECRETS["psk"], "nhrp_secret": SECRETS["nhrp_secret"]}
    return {"lab": ctx["lab"], "oob": ctx["oob"], "mac_oui": ctx["mac_oui"], "service": svc,
            "provider": ctx["provider"], "provider2": ctx.get("provider2"), "nodes": nodes, "applications": applications(pre)}


def applications(pre):
    """The applications' VIPs from the hubs' Virtual Servers (seed.py): what the hubs' VIP templates are rendered from."""
    r = requests.get(f"{a.url}/api/load-balancers/virtual-servers/", params={"limit": 1000, "depth": 1}, headers=H, timeout=60)
    r.raise_for_status()
    apps = {}
    for vs in r.json()["results"]:
        aid = (vs.get("custom_fields") or {}).get("application_id")
        dev = ((vs.get("device") or {}).get("name") or "")
        if not aid or not dev.startswith(pre) or not vs.get("vip"):
            continue
        hub = dev[len(pre):]
        x = apps.setdefault(aid, {"id": aid, "hubs": [], "vips": {}})
        x["hubs"].append(hub)
        x["vips"][hub] = vs["vip"]["address"].split("/")[0]
    return [apps[k] for k in sorted(apps)]


inv = inventory_from_nautobot()
if a.inventory:
    print(json.dumps(inv, indent=1))
    sys.exit()

rc = 0
for rel, text in sorted(render_all(inv).items()):
    path = LAB / rel
    if a.check:
        have = path.read_text() if path.exists() else ""
        if have == text:
            print(f"{rel}: Nautobot == lab.conf ({text.count(chr(10))} lines)")
        else:
            rc = 1
            print(f"{rel}: DIFFERS")
            print("".join(difflib.unified_diff(have.splitlines(True), text.splitlines(True), "lab.conf", "nautobot", n=1)))
    elif a.write:
        if not path.exists() or path.read_text() != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {rel}")
    else:
        print(f"===== {rel} =====\n{text}")
sys.exit(rc)
