"""Robot Framework variable file: every fact the suites check, derived from `lab.sh inventory` — never restated."""
import json
import re
import subprocess
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[2]
INV = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SVC, PROV = INV["service"], INV["provider"]
NODES = {n["name"]: n for n in INV["nodes"]}

HUBS = SVC["hubs"]
SPOKES = SVC["spokes"]
DMVPN = HUBS + SPOKES                                                    # every router in the cloud, any platform
PLATFORM = {n: v.get("platform", "c8000v") for n, v in NODES.items()}
C8K = [r for r in DMVPN if PLATFORM[r] == "c8000v"]                     # the IOS-XE routers (show commands, NAC)
C8K_SPOKES = [s for s in SPOKES if PLATFORM[s] == "c8000v"]
VYOS_SPOKES = [s for s in SPOKES if PLATFORM[s] == "vyos"]
# derived from whichever customers exist, so the suites follow the lab as the portal adds and removes them
C8K_PAIRS = ([f"{a}:{b}" for a, b in zip(C8K_SPOKES, C8K_SPOKES[1:] + C8K_SPOKES[:1])]   # a ring; two give a:b and b:a
             if len(C8K_SPOKES) >= 2 else [])
NEXT_CUSTOMER = max((int(re.search(r"(\d+)$", s)[1]) for s in SPOKES if re.search(r"(\d+)$", s)), default=0) + 1   # the allocator's rule
PROVIDER = PROV["nodes"][0]
HOSTS = [n for n, v in NODES.items() if v["role"] == "host"]
ALL_NODES = list(NODES)

ROUTERS = {}
for name in DMVPN:
    n = NODES[name]
    lan = n["lan"]
    lan_ip = lan.rsplit(".", 1)[0] + ".1"
    ROUTERS[name] = {"role": n["role"], "host": n["mgmt_ip"], "nbma": n["nbma"], "tunnel": n["tunnel_ip"],
                     "router_id": n["router_id"], "lan": lan, "lan_ip": lan_ip, "region": n["region"], "t_idx": n["t_idx"],
                     "wan_prefix": n["wan"]["prefix"], "wan_peer": n["wan"]["peer_ip"], "host_vm": n["host"] or ""}
COMPANIES = {c: NODES[c].get("customer") for c in SPOKES}
PREFER = {c: NODES[c]["prefer_hub"] for c in SPOKES if NODES[c].get("prefer_hub")}   # customer -> its preferred hub
PREFERRING = sorted(PREFER)
# two hosts whose traffic crosses a hub until the shortcut forms: the first behind a VyOS customer or one that prefers a hub
HUB_FIRST_PAIR = next(([a, b] for a in HOSTS for b in HOSTS
                       if NODES[a]["router"] != NODES[b]["router"]
                       and (PLATFORM[NODES[a]["router"]] == "vyos" or NODES[a]["router"] in PREFER)), [])
APPLICATIONS = INV.get("applications") or []
HOST_PAIR = [HOSTS[0], HOSTS[-1]] if len(HOSTS) >= 2 else []                 # a host and the one furthest from it
HOST_VMS = {h: {"host": NODES[h]["mgmt_ip"], "lan_ip": NODES[h]["lan_ip"].split("/")[0],
                "gateway": NODES[h]["gateway"], "router": NODES[h]["router"]} for h in HOSTS}
MGMT_IPS = {n: v["mgmt_ip"] for n, v in NODES.items()}
PROVIDER_HOST = NODES[PROVIDER]["mgmt_ip"]
PROVIDER_ROUTER_ID = NODES[PROVIDER]["router_id"]

OOB_GATEWAY = INV["oob"]["gateway"]
DOMAIN_NAME = "lab.local"
MGMT_ACL = "MGMT-ACCESS"
BGP_ASN = str(SVC["as"])
PROVIDER_ASN = str(PROV["as"])
WAN_NET = PROV["wan_net"]
OVERLAY = SVC["overlay"]
TUNNEL_KEY = str(SVC["tunnel_key"])
NHRP_NETWORK_ID = str(SVC["network_id"])
NHRP_HOLDTIME = str(SVC["holdtime"])
IKEV2_PROFILE = "DMVPN-IKEV2"
IPSEC_PROFILE = "DMVPN-IPSEC"
BANNER_TEXT = "c8000v-dmvpn-lab"
DOMAIN_PREFIX = INV["nodes"][0]["domain"][: -len(INV["nodes"][0]["name"])]
