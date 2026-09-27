#!/usr/bin/env python3
"""Run commands on a VyOS node over SSH (exec channels, vyos/vyos).   vyos_cmd.py HOST CMD [CMD ...]
A `show ...` command runs in op mode (VyOS's own wrapper); anything else — `sudo vtysh -c '...'` — runs in the shell."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vyos_ssh  # noqa: E402

host, cmds = sys.argv[1], sys.argv[2:]
for cmd in cmds:
    if len(cmds) > 1: print(f"$ {cmd}")
    out = vyos_ssh.op(host, cmd) if cmd.startswith("show ") else vyos_ssh.run(host, cmd)[1]
    print(out.rstrip("\n"))
