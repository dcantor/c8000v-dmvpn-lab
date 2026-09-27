#!/usr/bin/env python3
"""Write every rendered file — nodes/<n>/iosxe_config.txt, nodes/<n>/vyos_config.txt, nac/data/devices.nac.yaml —
from `lab.sh inventory` through tools/render.py. Only files whose content changes are rewritten.
Usage: tools/gen_configs.py [--check]      (--check: write nothing, exit 1 if anything would change)"""
import json
import subprocess
import sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import render_all   # noqa: E402

check = "--check" in sys.argv
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
files = render_all(inv)
changed = []
for rel, text in files.items():
    path = LAB_DIR / rel
    if path.exists() and path.read_text() == text:
        continue
    changed.append(rel)
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
# the pre-shared key, where Network-as-Code reads it and git does not (.gitignore): a global variable
sys.path.insert(0, str(Path(__file__).resolve().parent))
import labsecrets  # noqa: E402
sec = LAB_DIR / "nac" / "data" / "secrets.nac.yaml"
want = labsecrets.nac_secrets_yaml()
if not check and (not sec.exists() or sec.read_text() != want):
    sec.write_text(want)
    sec.chmod(0o600)
for rel in changed:
    print(f"  {'would change' if check else 'wrote'} {rel}")
print(f"{len(changed)} of {len(files)} file(s) {'out of date' if check else 'changed'}")
sys.exit(1 if check and changed else 0)
