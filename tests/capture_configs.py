#!/usr/bin/env python3
"""Download the running and startup configuration of every C8000v, and the provider's, into a directory."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "resources"))
from LabLib import LabLib                              # noqa: E402
from lab_vars import PLATFORM, PROVIDER, PROVIDER_HOST, ROUTERS  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
lib = LabLib()
stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
try:
    for name, r in ROUTERS.items():
        if PLATFORM[name] == "vyos":                     # a VyOS customer: its set commands, like the provider's
            try:
                cfg = lib.vyos_command(r["host"], "show configuration commands", timeout=120)
                (out / f"{name}.config-commands.txt").write_text(f"# {name} captured {stamp}\n{cfg}\n")
                print(f"[{name}] config captured")
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] {e.__class__.__name__}: {e}")
            continue
        for cmd, suffix in (("show running-config", "running-config"), ("show startup-config", "startup-config")):
            try:
                cfg = lib.run_command(r["host"], cmd, timeout=120)
            except Exception as e:      # a router that is down must not stop the capture
                print(f"[{name}] {cmd}: {e.__class__.__name__}: {e}")
                continue
            path = out / f"{name}.{suffix}.txt"
            path.write_text(f"! {name} ({r['host']}) {cmd} captured {stamp}\n{cfg}\n")
            print(f"[{name}] {path} ({len(cfg)} bytes)")
    try:
        cfg = lib.vyos_command(PROVIDER_HOST, "show configuration commands", timeout=120)
        (out / f"{PROVIDER}.config-commands.txt").write_text(f"# {PROVIDER} captured {stamp}\n{cfg}\n")
        print(f"[{PROVIDER}] config captured")
    except Exception as e:  # noqa: BLE001
        print(f"[{PROVIDER}] {e.__class__.__name__}: {e}")
finally:
    lib.close_all_connections()
