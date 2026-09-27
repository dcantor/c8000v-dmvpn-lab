"""Re-authenticating the IKE sessions after a pre-shared key rotation, and proving it took.

After the pre-shared key changes, the IKE SAs that are up keep the key they were made with until they rekey (8 h).
reauth() makes every router set up its IKE SAs again, one router at a time, waiting for it to be registered with every hub before the next: a C8000v clears its crypto
sessions, a VyOS router terminates its strongSwan SAs. verify() then reads every router's IKE SAs: that each one
authenticated with the pre-shared key (IOS "Auth sign / Auth verify: PSK") and how old it is — so a rotation can prove
every SA is younger than the new key."""
import re
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "tools"))
import vyos_ssh  # noqa: E402

from state import ios  # noqa: E402


def _reset(n):
    if (n.get("platform") or "c8000v") == "vyos":
        out = vyos_ssh.run(n["mgmt_ip"], "for i in $(sudo swanctl --list-sas 2>/dev/null | sed -n 's/^[^ ]*: #\\([0-9]*\\), .*/\\1/p'); do "
                                         "sudo swanctl --terminate --ike-id $i --force >/dev/null 2>&1; done; echo done")[1]
        return "strongSwan: every IKE SA terminated" if "done" in out else out[-200:]
    ios(n["mgmt_ip"], "clear crypto session")
    return "clear crypto session"


def reauth(inv, state, say, settle=150):
    """Every router, customers first then the hubs (a customer keeps its other hubs while one is cleared)."""
    nodes = {n["name"]: n for n in inv["nodes"]}
    order = inv["service"]["spokes"] + inv["service"]["hubs"]
    for r in order:
        say(f"{r}: {_reset(nodes[r])}")
        deadline = time.time() + settle
        time.sleep(8)
        while True:
            snap = state.get(refresh=True)
            c = (snap.get("cloud") or {}).get(r) or {}
            e = c.get("expect") or {}
            ok = not c.get("error") and c.get("sa", 0) >= e.get("sa", 1) and c.get("overlay_up", 0) >= e.get("overlay", 1)
            if ok:
                say(f"{r}: back — {c.get('sa')} IPsec sessions, {c.get('overlay_up')} overlay BGP sessions")
                break
            if time.time() > deadline:
                raise RuntimeError(f"{r} did not come back within {settle} s: {c.get('sa')} IPsec, {c.get('overlay_up')} BGP sessions")
            time.sleep(8)


IOS_SA = re.compile(r"(?m)^\s*\d+\s+(\d+\.\d+\.\d+\.\d+)/\d+\s+(\d+\.\d+\.\d+\.\d+)/\d+\s+\S+\s+(\w+)\s*\n(?:.*\n){0,2}?.*Auth sign: (\w+), Auth verify: (\w+)\s*\n\s*Life/Active Time: \d+/(\d+) sec")


def sas(n):
    """[{peer, auth: psk|cert, age}] of the router's established IKE SAs."""
    if (n.get("platform") or "c8000v") == "vyos":
        out = vyos_ssh.run(n["mgmt_ip"], "sudo swanctl --list-sas 2>/dev/null")[1]
        res, cur = [], None
        for l in out.splitlines():
            m = re.match(r"^\S+: #\d+, ESTABLISHED, IKEv2", l)
            if m:
                cur = {"peer": None, "auth": None, "age": None}
                res.append(cur)
                continue
            if cur is None:
                continue
            m = re.search(r"established (\d+)s ago", l)
            if m:
                cur["age"] = int(m[1])
            m = re.match(r"^\s+local\s+'([^']*)'", l)
            if m:
                cur["auth"] = "cert" if "CN=" in m[1] or "cn=" in m[1] else "psk"
            m = re.match(r"^\s+remote\s+'[^']*' @ (\d+\.\d+\.\d+\.\d+)", l)
            if m:
                cur["peer"] = m[1]
        return [x for x in res if x["peer"]]
    out = ios(n["mgmt_ip"], "show crypto ikev2 sa detailed")["show crypto ikev2 sa detailed"]
    res = []
    for m in IOS_SA.finditer(out):
        if m[3] != "READY":
            continue
        sign, verify = m[4], m[5]
        res.append({"peer": m[2], "auth": "cert" if sign == "RSA" and verify == "RSA" else ("psk" if sign == verify == "PSK" else f"{sign}/{verify}"),
                    "age": int(m[6])})
    return res


def verify(inv, want=None, younger_than=None):
    """{router: {"sas": n, "wrong": [...], "old": [...]}}: SAs not authenticated as `want`, or older than `younger_than` s."""
    out = {}
    for n in inv["nodes"]:
        if n["role"] not in ("hub", "spoke"):
            continue
        try:
            s = sas(n)
        except Exception as e:                                         # noqa: BLE001
            out[n["name"]] = {"sas": 0, "wrong": [], "old": [], "error": f"{e.__class__.__name__}: {e}"}
            continue
        out[n["name"]] = {"sas": len(s),
                          "wrong": [x for x in s if want and x["auth"] != want],
                          "old": [x for x in s if younger_than is not None and x["age"] is not None and x["age"] > younger_than],
                          "by_auth": {a: sum(1 for x in s if x["auth"] == a) for a in {x["auth"] for x in s}}}
    return out
