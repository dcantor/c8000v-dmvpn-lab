#!/usr/bin/env python3
"""Apply a file of VyOS `set` lines over SSH: configure, reconcile, set*, commit, save (vyos_ssh: exec channels, no prompt).

A bare `set` push only ever adds, which is fine while a node's shape does not change — but this lab re-wires: a port
that was unwired becomes a branch's link, and a port a branch used goes back to being unwired. So before applying,
the running configuration of every ethernet port the file mentions is compared with what the file wants, and the
differences are deleted: an address the model no longer gives that port, and a `disable` on a port the model now
addresses. Without that, a re-wired port keeps its old address and stays administratively down, and nothing says so.

The routing sections are declarative too: every section below that the file configures is deleted and set again in the
same commit (policy, BGP, NHRP, static routes, IPsec, the tunnel and dummy interfaces). VyOS commits the difference
between the old and the new tree, so an unchanged section changes nothing — but a line the model dropped (a hub
preference taken away, a route-map no longer used) goes, instead of lingering in the running configuration.

   vyos_push.py HOST FILE [--dry-run]"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vyos_ssh  # noqa: E402

host, path = sys.argv[1], sys.argv[2]
dry = "--dry-run" in sys.argv
lines = [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]

# every ethernet port the file manages, and the addresses it wants on each (an empty set = the model says none)
want_addr, want_disabled = {}, set()
for l in lines:
    m = re.match(r"set interfaces ethernet (\S+)\b", l)
    if m: want_addr.setdefault(m[1], set())
    m = re.match(r"set interfaces ethernet (\S+) address (\S+)", l)
    if m: want_addr[m[1]].add(m[2].strip("'"))
    m = re.match(r"set interfaces ethernet (\S+) disable$", l)
    if m: want_disabled.add(m[1])

running = vyos_ssh.op(host, "show configuration commands", timeout=180)
if "set system host-name" not in running:
    print("FAILED: could not read the running configuration:", running[-400:]); sys.exit(1)
MANAGED = ("policy", "protocols bgp", "protocols nhrp", "protocols static", "vpn ipsec", "interfaces tunnel", "interfaces dummy")
deletes = [f"delete {sec}" for sec in MANAGED
           if any(l.startswith(f"set {sec} ") for l in lines) and re.search(rf"^set {sec} ", running, re.M)]
for l in running.splitlines():
    m = re.match(r"set interfaces ethernet (\S+) address (\S+)", l.strip())
    if m and m[1] in want_addr and m[2].strip("'") not in want_addr[m[1]]:
        deletes.append(f"delete interfaces ethernet {m[1]} address {m[2].strip(chr(39))}")
    m = re.match(r"set interfaces ethernet (\S+) disable$", l.strip())
    if m and want_addr.get(m[1]) and m[1] not in want_disabled:
        deletes.append(f"delete interfaces ethernet {m[1]} disable")
if dry:
    print("\n".join(deletes + lines)); sys.exit()
rc, out = vyos_ssh.configure(host, deletes + lines)
bad = [l for l in out.splitlines() if re.search(r"Invalid|Commit failed|is not valid|Set failed|Error", l)]
if rc != 0 or bad: print("FAILED:", *(bad or out.splitlines()[-15:]), sep="\n  "); sys.exit(1)
fixed = [d for d in deletes if d.removeprefix("delete ") not in MANAGED]
note = f" (reconciled {len(fixed)})" if fixed else ""
print("no changes" if "No configuration changes to commit" in out else
      f"applied {len(lines)} lines{note}, committed and saved")
