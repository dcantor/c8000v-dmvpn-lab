#!/usr/bin/env python3
"""Assemble the lab inventory (JSON) from the tab-separated facts `lab.sh inventory` prints out of lab.conf.

This is where the derived facts live, so nothing downstream derives them again: a DMVPN router's NBMA address, tunnel
address and router-id all come from its one index (T_IDX); every port's address comes from LINKS (.1 for the first
end, .2 for the second). The renderer, the tests, Nautobot's seed and the portal all read this one document."""
import ipaddress
import json
import sys
from pathlib import Path

CUSTOMERS = Path(__file__).resolve().parents[1] / "customers.json"
APPLICATIONS = Path(__file__).resolve().parents[1] / "applications.json"

scalar, maps, lists, ports = {}, {}, {}, {}
for line in sys.stdin:
    f = line.rstrip("\n").split("\t")
    if f[0] == "scalar":
        scalar[f[1]] = f[2]
    elif f[0] == "map":
        maps.setdefault(f[1], {})[f[2]] = f[3]
    elif f[0] == "list":
        lists[f[1]] = f[2].split()
    elif f[0] == "port":
        ports.setdefault(f[1], []).append((int(f[2]), f[3], f[4].split()))

role = maps["ROLE"]
companies = {k: v for k, v in json.loads(CUSTOMERS.read_text()).items() if not k.startswith("_")} if CUSTOMERS.exists() else {}


def port_name(node, num):
    return f"GigabitEthernet{num}" if maps["PLATFORM"][node] == "c8000v" else f"eth{num}"


def end_addr(prefix, end):
    n = ipaddress.ip_network(prefix)
    return f"{n.network_address + int(end)}/{n.prefixlen}"


overlay = ipaddress.ip_network(scalar["DMVPN_OVERLAY"])
nodes = []
for name in lists["ALL_NODES"]:
    node = {"name": name, "role": role[name], "platform": maps["PLATFORM"][name], "domain": scalar["DOMAIN_PREFIX"] + name,
            "region": maps["REGION"].get(name), "mgmt_ip": maps["MGMT_IP"][name],
            "console": int(maps["CONSOLE_PORT"][name]), "idx": int(maps["NODE_IDX"][name])}
    plist = []
    for num, pname, peer in ports.get(name, []):
        if peer:
            pn, pp, pfx, end = peer
            plist.append({"name": pname, "num": num, "ip": end_addr(pfx, end), "prefix": pfx, "peer": pn,
                          "peer_port": port_name(pn, int(pp)), "peer_role": role[pn],
                          "peer_ip": end_addr(pfx, 3 - int(end)).split("/")[0]})
        else:
            plist.append({"name": pname, "num": num, "ip": None, "prefix": None, "peer": None})
    node["ports"] = plist
    t = maps["T_IDX"].get(name)
    if t is not None:                       # a DMVPN router: hub or customer
        t = int(t)
        wan = next(p for p in plist if p.get("peer_role") == "provider")
        node.update({"t_idx": t, "router_id": f"{scalar['ROUTER_ID_NET']}.{t}",
                     "tunnel_ip": str(overlay.network_address + t),
                     "nbma": wan["ip"].split("/")[0], "wan": wan, "lan": maps["LAN"].get(name),
                     "host": maps["HOST_OF"].get(name)})
        lan_port = next((p for p in plist if p["num"] == int(scalar["LAN_PORT"]) and p["ip"]), None)
        node["lan_port"] = lan_port
        if role[name] == "spoke":
            node["customer"] = companies.get(name)   # who the site belongs to (customers.json); None if not recorded
    elif role[name] == "provider":
        node["router_id"] = f"{scalar['ROUTER_ID_NET']}.254"
    elif role[name] == "host":
        lan = plist[0]
        node.update({"lan_ip": lan["ip"], "gateway": lan["peer_ip"], "router": lan["peer"]})
    nodes.append(node)

inv = {
    "lab": scalar["LAB_NAME"],
    "oob": {"network": scalar["OOB_NET"], "gateway": scalar["OOB_GATEWAY"], "prefix": scalar["OOB_PREFIX"],
            "nms": scalar["NMS_IP"]},
    "mac_oui": scalar["MAC_OUI"],
    "service": {
        "as": int(scalar["DMVPN_AS"]), "overlay": scalar["DMVPN_OVERLAY"],
        "network_id": int(scalar["DMVPN_NETWORK_ID"]), "tunnel_key": int(scalar["DMVPN_TUNNEL_KEY"]),
        "holdtime": int(scalar["DMVPN_HOLDTIME"]), "mtu": int(scalar["DMVPN_MTU"]), "mss": int(scalar["DMVPN_MSS"]),
        "psk": scalar["DMVPN_PSK"], "nhrp_secret": scalar["NHRP_SECRET"],
        "keepalive": int(scalar["BGP_KEEPALIVE"]), "holdtime_bgp": int(scalar["BGP_HOLDTIME"]),
        "hubs": lists["HUBS"], "spokes": lists["SPOKES"],
    },
    "provider": {"as": int(scalar["PROVIDER_AS"]), "wan_net": f"{scalar['WAN_NET']}.0.0/16", "nodes": lists["PROVIDERS"]},
    "nodes": nodes,
    "applications": json.loads(APPLICATIONS.read_text())["applications"] if APPLICATIONS.exists() else [],
}
json.dump(inv, sys.stdout, indent=2)
print()
