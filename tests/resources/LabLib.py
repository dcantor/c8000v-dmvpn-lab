"""Robot Framework keyword library for c8000v-dmvpn-lab.

C8000v over SSH (netmiko) and RESTCONF (requests); the VyOS routers over SSH exec channels (tools/vyos_ssh.py); the Alpine hosts over SSH
(paramiko); terraform and lab.sh on the lab host."""
import os
import socket
import subprocess
import time
from pathlib import Path

import paramiko
import requests
import urllib3
from netmiko import ConnectHandler
from robot.api import logger
from robot.api.deco import keyword, library

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LAB_DIR = Path(__file__).resolve().parents[2]
USERNAME = os.environ.get("IOSXE_USERNAME", "admin")
PASSWORD = os.environ.get("IOSXE_PASSWORD", "admin")


@library(scope="GLOBAL")
class LabLib:
    def __init__(self):
        self._ssh = {}   # (kind, host) -> netmiko connection

    # ---- C8000v: SSH ---------------------------------------------------------
    def _conn(self, host, kind="cisco_xe"):
        key = (kind, host)
        if key not in self._ssh:
            if kind == "cisco_xe":
                self._ssh[key] = ConnectHandler(device_type="cisco_xe", host=host, username=USERNAME,
                                                password=PASSWORD, secret=PASSWORD, fast_cli=False)
                self._ssh[key].enable()
            else:
                self._ssh[key] = ConnectHandler(device_type="vyos", host=host, username="vyos", password="vyos")
        return self._ssh[key]

    @keyword
    def run_command(self, host, command, timeout=60):
        """Run a show/exec command on a C8000v over SSH and return its output."""
        try:
            out = self._conn(host).send_command(command, read_timeout=float(timeout))
        except (OSError, EOFError):                     # a stale session (router reloaded): reconnect once
            self._ssh.pop(("cisco_xe", host), None)
            out = self._conn(host).send_command(command, read_timeout=float(timeout))
        logger.info(f"<pre>{host}# {command}\n{out}</pre>", html=True)
        return out

    @keyword
    def get_running_config(self, host):
        return self._conn(host).send_command("show running-config", read_timeout=120)

    @keyword
    def vyos_command(self, host, command, timeout=60):
        """A `show ...` in op mode, anything else (`sudo vtysh -c '...'`) in the shell — plain SSH exec channels
        (tools/vyos_ssh.py): netmiko's interactive VyOS login intermittently misses the prompt."""
        import sys
        sys.path.insert(0, str(LAB_DIR / "tools"))
        import vyos_ssh
        out = (vyos_ssh.op(host, command, timeout=float(timeout)) if command.startswith("show ")
               else vyos_ssh.run(host, command, timeout=float(timeout))[1])
        logger.info(f"<pre>vyos@{host}$ {command}\n{out}</pre>", html=True)
        return out

    @keyword
    def close_all_connections(self):
        for c in self._ssh.values():
            try:
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._ssh.clear()

    # ---- C8000v: RESTCONF ----------------------------------------------------
    @keyword
    def restconf_get(self, host, path):
        """GET /restconf/data/<path> and return the parsed JSON."""
        url = f"https://{host}/restconf/data/{path}"
        r = requests.get(url, auth=(USERNAME, PASSWORD), verify=False, timeout=30,
                         headers={"Accept": "application/yang-data+json"})
        logger.info(f"GET {url} -> {r.status_code}\n{r.text[:2000]}")
        r.raise_for_status()
        return r.json() if r.text else {}

    # ---- Alpine hosts --------------------------------------------------------
    @keyword
    def host_command(self, host, command, timeout=60):
        """Run a shell command on a LAN host; returns [rc, output]."""
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(host, username="lab", password="lab", timeout=20, look_for_keys=False, allow_agent=False)
        try:
            _, out, err = c.exec_command(command, timeout=float(timeout))
            rc = out.channel.recv_exit_status()
            text = out.read().decode() + err.read().decode()
        finally:
            c.close()
        logger.info(f"<pre>lab@{host}$ {command}\n{text}\n(rc={rc})</pre>", html=True)
        return [rc, text]

    # ---- lab host --------------------------------------------------------------
    @keyword
    def domain_state(self, domain):
        r = subprocess.run(["virsh", "-q", "-c", "qemu:///system", "domstate", domain], capture_output=True, text=True)
        return r.stdout.strip() or "undefined"

    @keyword
    def tcp_port_should_be_open(self, host, port, timeout=5):
        with socket.socket() as s:
            s.settimeout(float(timeout))
            try:
                s.connect((host, int(port)))
            except OSError as e:
                raise AssertionError(f"{host}:{port} not reachable: {e}")

    @keyword
    def terraform_plan_exit_code(self):
        """`terraform plan -detailed-exitcode` via lab.sh nac: 0 = no drift, 2 = changes, 1 = error."""
        r = subprocess.run([str(LAB_DIR / "lab.sh"), "nac", "plan", "-detailed-exitcode", "-no-color",
                            "-input=false", "-lock=false"], capture_output=True, text=True, timeout=900)
        logger.info(f"<pre>{r.stdout[-6000:]}\n{r.stderr[-2000:]}</pre>", html=True)
        return r.returncode

    @keyword
    def rendered_configs_are_current(self):
        """tools/gen_configs.py --check: the committed renders match lab.conf."""
        r = subprocess.run(["python3", str(LAB_DIR / "tools" / "gen_configs.py"), "--check"], capture_output=True, text=True)
        logger.info(f"<pre>{r.stdout}\n{r.stderr}</pre>", html=True)
        return r.returncode

    @keyword
    def lab_sh_exit_code(self, *args):
        """Run ./lab.sh with arguments; return the exit code (output goes to the log)."""
        r = subprocess.run([str(LAB_DIR / "lab.sh"), *args], capture_output=True, text=True, timeout=900)
        logger.info(f"<pre>$ lab.sh {' '.join(args)}\n{r.stdout[-6000:]}\n{r.stderr[-2000:]}</pre>", html=True)
        return r.returncode

    @keyword
    def nautobot_device_names(self, location):
        """The names of the Nautobot devices at a location and its children (GraphQL)."""
        import requests as rq
        token = subprocess.run([str(LAB_DIR / "lab.sh"), "nautobot", "token"], capture_output=True, text=True).stdout.strip()
        url = os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080")
        q = '{ locations(name: ["%s"]) { name devices { name } children { devices { name } } } }' % location
        r = rq.post(f"{url}/api/graphql/", json={"query": q}, headers={"Authorization": f"Token {token}"}, timeout=60)
        r.raise_for_status()
        loc = r.json()["data"]["locations"]
        if not loc:
            return []
        names = [d["name"] for d in loc[0]["devices"]] + [d["name"] for c in loc[0]["children"] for d in c["devices"]]
        logger.info(f"{location}: {sorted(names)}")
        return sorted(names)

    @keyword
    def pdf_page_count(self, content):
        """Pages in a PDF (bytes), by poppler's pdfinfo — Chrome compresses its object streams, so the raw bytes do
        not show the page objects."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(content); f.flush()
            out = subprocess.run(["pdfinfo", f.name], capture_output=True, text=True, check=True).stdout
        return int(next(l.split()[1] for l in out.splitlines() if l.startswith("Pages:")))

    @keyword
    def nautobot_customer_tenants(self, group):
        """{tenant name: {"fields": custom field data, "devices": [device names]}} for a tenant group (GraphQL)."""
        import requests as rq
        token = subprocess.run([str(LAB_DIR / "lab.sh"), "nautobot", "token"], capture_output=True, text=True).stdout.strip()
        url = os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080")
        q = '{ tenants(tenant_group: ["%s"]) { name _custom_field_data devices { name } } }' % group
        r = rq.post(f"{url}/api/graphql/", json={"query": q}, headers={"Authorization": f"Token {token}"}, timeout=60)
        r.raise_for_status()
        out = {t["name"]: {"fields": t["_custom_field_data"], "devices": sorted(d["name"] for d in t["devices"])}
               for t in r.json()["data"]["tenants"]}
        logger.info(out)
        return out

    @keyword
    def nautobot_applications(self):
        """What Nautobot holds for the applications: {(app id, hub device): {vip, port, protocol, url, name}} and the
        subscriptions {tenant name: sorted app ids}."""
        import requests as rq
        token = subprocess.run([str(LAB_DIR / "lab.sh"), "nautobot", "token"], capture_output=True, text=True).stdout.strip()
        url, H = os.environ.get("NAUTOBOT_URL", "http://10.0.0.10:8080"), {"Authorization": f"Token {token}"}
        q = """{ virtual_servers { id name port protocol _custom_field_data vip { host } device { name } }
                 tenants { id name } }"""
        r = rq.post(f"{url}/api/graphql/", json={"query": q}, headers=H, timeout=60)
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise AssertionError(body["errors"])
        vs, subs, app_of = {}, {}, {}
        for v in body["data"]["virtual_servers"]:
            cfd = v["_custom_field_data"] or {}
            if not cfd.get("application_id") or not v["device"]:
                continue
            app_of[v["id"]] = cfd["application_id"]
            vs[f'{cfd["application_id"]}@{v["device"]["name"]}'] = {"vip": v["vip"]["host"], "port": v["port"],
                                                                    "protocol": str(v["protocol"]).lower(), "url": cfd.get("application_url"),
                                                                    "name": cfd.get("application_name")}
        tenant = {t["id"]: t["name"] for t in body["data"]["tenants"]}
        # the subscriptions relationship is not on the Virtual Server GraphQL type: read its associations over REST
        nxt = f"{url}/api/extras/relationship-associations/?relationship=application_subscriptions&limit=500"
        while nxt:
            page = rq.get(nxt, headers={**H, "Accept": "application/json"}, timeout=60)
            page.raise_for_status()
            page = page.json()
            for a in page["results"]:
                src, dst = str(a["source_id"]), str(a["destination_id"])
                if src in tenant and dst in app_of:
                    subs.setdefault(tenant[src], set()).add(app_of[dst])
            nxt = page.get("next")
        out = {"vs": vs, "subs": {k: sorted(v) for k, v in subs.items()}}
        logger.info(out)
        return out

    @keyword
    def read_lab_file(self, rel):
        return (LAB_DIR / rel).read_text()

    @keyword
    def unique_marker(self, prefix="robot"):
        return f"{prefix}-{int(time.time())}"
