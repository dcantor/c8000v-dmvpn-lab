#!/usr/bin/env python3
"""Run exec commands on a C8000v over SSH (netmiko, admin/admin).   ios_cmd.py NODE|ADDRESS CMD [CMD ...]"""
import json
import os
import subprocess
import sys
from pathlib import Path

from netmiko import ConnectHandler

LAB_DIR = Path(__file__).resolve().parents[1]
target, cmds = sys.argv[1], sys.argv[2:]
if not target[0].isdigit():
    inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
    target = next(n["mgmt_ip"] for n in inv["nodes"] if n["name"] == target)
c = ConnectHandler(device_type="cisco_xe", host=target, username=os.environ.get("IOSXE_USERNAME", "admin"),
                   password=os.environ.get("IOSXE_PASSWORD", "admin"))
try:
    for cmd in cmds:
        out = c.send_command(cmd, read_timeout=90)
        if len(cmds) > 1:
            print(f"$ {cmd}")
        print(out)
finally:
    c.disconnect()
