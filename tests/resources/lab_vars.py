"""Robot Framework variable file: every fact the suites check, derived from `lab.sh inventory` — never restated."""
import json
import subprocess
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[2]
INV = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
SVC, PROV = INV["service"], INV["provider"]
NODES = {n["name"]: n for n in INV["nodes"]}

HUBS = SVC["hubs"]
SPOKES = SVC["spokes"]
C8K = HUBS + SPOKES
PROVIDER = PROV["nodes"][0]
HOSTS = [n for n, v in NODES.items() if v["role"] == "host"]
ALL_NODES = list(NODES)

ROUTERS = {}
for name in C8K:
    n = NODES[name]
    lan = n["lan"]
    lan_ip = lan.rsplit(".", 1)[0] + ".1"
    ROUTERS[name] = {"role": n["role"], "host": n["mgmt_ip"], "nbma": n["nbma"], "tunnel": n["tunnel_ip"],
                     "router_id": n["router_id"], "lan": lan, "lan_ip": lan_ip, "region": n["region"],
                     "wan_prefix": n["wan"]["prefix"], "wan_peer": n["wan"]["peer_ip"], "host_vm": n["host"] or ""}
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
