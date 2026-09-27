#!/usr/bin/env python3
"""Install the lab's VyOS post-commit hooks on a VyOS customer and run them once (they then run after every commit,
including the one at boot). The hooks live in /config, which survives reboots and image upgrades.

   vyos_hooks.py HOST"""
import os
import sys
from pathlib import Path

import paramiko

HOOKS = {"99-c8d-ike-noesn": Path(__file__).resolve().parent / "vyos_ike_noesn.sh"}
host = sys.argv[1]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"),
          look_for_keys=False, allow_agent=False, timeout=20)
try:
    for name, src in HOOKS.items():
        dst = f"/config/scripts/commit/post-hooks.d/{name}"
        stdin, out, err = c.exec_command(f"sudo mkdir -p /config/scripts/commit/post-hooks.d && sudo tee {dst} >/dev/null && "
                                         f"sudo chmod 755 {dst} && sudo {dst} && echo ok", timeout=60)
        stdin.write(src.read_text()); stdin.channel.shutdown_write()
        res = out.read().decode().strip() + err.read().decode().strip()
        print(f"{name}: {'installed and run' if res.endswith('ok') else 'FAILED ' + res}")
        if not res.endswith("ok"):
            sys.exit(1)
finally:
    c.close()
