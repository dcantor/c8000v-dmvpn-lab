"""VyOS over plain SSH exec channels — no interactive prompt to detect.

netmiko drives VyOS through an interactive shell and waits for the prompt; on a freshly booted VyOS it intermittently
misses it ("Pattern not detected: 'vyos@cust6:~$ set terminal length 0'") and the whole push fails. Everything here
runs one non-interactive command per channel instead: op-mode through VyOS's own wrapper, configuration through
vbash with the script template, which is how VyOS documents scripted configuration."""
import os
import shlex
import time

import paramiko

USER, PASS = os.environ.get("VYOS_USERNAME", "vyos"), os.environ.get("VYOS_PASSWORD", "vyos")


def _client(host, tries=3):
    for i in range(tries):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host, username=USER, password=PASS, timeout=20, look_for_keys=False, allow_agent=False)
            return c
        except Exception:                                         # noqa: BLE001
            c.close()
            if i == tries - 1:
                raise
            time.sleep(5)


def run(host, cmd, stdin=None, timeout=300):
    """One command on its own channel; returns (exit code, stdout + stderr)."""
    c = _client(host)
    try:
        i, o, e = c.exec_command(cmd, timeout=timeout)
        if stdin is not None:
            i.write(stdin)
            i.channel.shutdown_write()
        rc = o.channel.recv_exit_status()
        return rc, o.read().decode(errors="replace") + e.read().decode(errors="replace")
    finally:
        c.close()


def op(host, command, timeout=120):
    """An op-mode command (`show ...`) through vyatta-op-cmd-wrapper. VyOS's own output filters — `| match RE`,
    `| grep RE`, `| no-more`, `| count` — are the interactive shell's; here they become the shell's grep / wc."""
    base, *filters = [p.strip() for p in command.split(" | ")]
    cmd = "/opt/vyatta/bin/vyatta-op-cmd-wrapper " + base.replace("'", "")
    for f in filters:
        word, _, arg = f.partition(" ")
        arg = arg.strip()
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'":
            arg = arg[1:-1]
        if word in ("match", "grep"):
            cmd += " | grep -E -- " + shlex.quote(arg)
        elif word == "count":
            cmd += " | wc -l"
        elif word == "no-more":
            continue
        else:
            raise ValueError(f"unsupported op-mode filter: | {f}")
    return run(host, cmd, timeout=timeout)[1]


def configure(host, lines, save=True, timeout=600):
    """Enter configuration mode, apply the lines, commit (and save) in one vbash script. Returns (rc, output)."""
    script = "\n".join(["source /opt/vyatta/etc/functions/script-template", "configure", *lines, "commit",
                        *(["save"] if save else []), "exit", ""])
    path = f"/tmp/c8d-push-{os.getpid()}-{int(time.time())}.sh"
    rc, out = run(host, f"cat > {shlex.quote(path)} && sg vyattacfg -c 'vbash {path}'; rc=$?; rm -f {shlex.quote(path)}; exit $rc",
                  stdin=script, timeout=timeout)
    return rc, out
