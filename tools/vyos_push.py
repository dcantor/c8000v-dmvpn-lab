#!/usr/bin/env python3
"""Apply a file of VyOS `set` lines over SSH (netmiko): configure, reconcile, set*, commit, save.

A bare `set` push only ever adds, which is fine while a node's shape does not change — but this lab re-wires: a port
that was unwired becomes a branch's link, and a port a branch used goes back to being unwired. So before applying,
the running configuration of every ethernet port the file mentions is compared with what the file wants, and the
differences are deleted: an address the model no longer gives that port, and a `disable` on a port the model now
addresses. Without that, a re-wired port keeps its old address and stays administratively down, and nothing says so.

   vyos_push.py HOST FILE [--dry-run]"""
import os
import re
import sys

from netmiko import ConnectHandler

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

c = ConnectHandler(device_type="vyos", host=host, username=os.environ.get("VYOS_USERNAME", "vyos"),
                   password=os.environ.get("VYOS_PASSWORD", "vyos"))
try:
    running = c.send_command("show configuration commands", read_timeout=180)
    deletes = []
    for l in running.splitlines():
        m = re.match(r"set interfaces ethernet (\S+) address (\S+)", l.strip())
        if m and m[1] in want_addr and m[2].strip("'") not in want_addr[m[1]]:
            deletes.append(f"delete interfaces ethernet {m[1]} address {m[2].strip(chr(39))}")
        m = re.match(r"set interfaces ethernet (\S+) disable$", l.strip())
        if m and want_addr.get(m[1]) and m[1] not in want_disabled:
            deletes.append(f"delete interfaces ethernet {m[1]} disable")
    if dry:
        print("\n".join(deletes + lines)); sys.exit()
    out = c.send_config_set(deletes + lines + ["commit", "save"], exit_config_mode=True, cmd_verify=False,
                            read_timeout=240)
finally:
    c.disconnect()
bad = [l for l in out.splitlines() if re.search(r"Invalid|Commit failed|is not valid|Error", l)]
if bad: print("FAILED:", *bad, sep="\n  "); sys.exit(1)
note = f" (reconciled {len(deletes)})" if deletes else ""
print("no changes" if "No configuration changes to commit" in out else
      f"applied {len(lines)} lines{note}, committed and saved")
