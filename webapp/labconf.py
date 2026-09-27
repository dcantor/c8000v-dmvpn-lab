"""Targeted edits of lab.conf — the operational source for what libvirt built: VMs, addresses, links.

Every edit keeps the file's structure: new entries go inside the existing `declare -A NAME=( ... )` / `NAME=( ... )`
blocks, so `lab.sh` and `gen_configs.py` keep working and the diff of adding a customer reads like one."""
import re
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
CONF = LAB / "lab.conf"


def read(): return CONF.read_text()


def write(text): CONF.write_text(text)


def _block(text, name):
    """(start, end) of the `NAME=(` ... `)` block; end points at the closing paren."""
    m = re.search(rf"^(?:declare -A )?{re.escape(name)}=\(", text, re.M)
    if not m: raise ValueError(f"{name} not found in lab.conf")
    depth = 0
    for j in range(m.end() - 1, len(text)):
        if text[j] == "(": depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0: return m.start(), j
    raise ValueError(f"unterminated {name} block")


def add_assoc(text, name, key, value):
    """Add `[key]=value` to an associative array (no-op if present, error if present with another value)."""
    s, e = _block(text, name); body = text[s:e]
    m = re.search(rf"\[{re.escape(key)}\]=(\S+)", body)
    if m:
        if m[1] != str(value): raise ValueError(f"{name}[{key}] is {m[1]}, not {value}")
        return text
    sep = "\n" + " " * (len(re.match(r"^(?:declare -A )?\w+=\(", body)[0]) + 1) if "\n" in body.strip() else " "
    return text[:e].rstrip() + f"{sep}[{key}]={value} " + text[e:]


def set_assoc(text, name, key, value):
    """Set `[key]=value` in an associative array: replace the value if the key is there, add it if not."""
    s, e = _block(text, name); body = text[s:e]
    if re.search(rf"\[{re.escape(key)}\]=\S+", body):
        return text[:s] + re.sub(rf"\[{re.escape(key)}\]=\S+", f"[{key}]={value}", body, count=1) + text[e:]
    return add_assoc(text, name, key, value)


def remove_assoc(text, name, key):
    s, e = _block(text, name)
    return text[:s] + re.sub(rf"\s*\[{re.escape(key)}\]=\S+", "", text[s:e]) + text[e:]


def add_list_item(text, name, item, comment=None):
    s, e = _block(text, name); body = text[s:e]
    if item in body: return text
    if "\n" in body.strip():
        return text[:e].rstrip() + ("\n  # " + comment if comment else "") + f"\n  {item}\n" + text[e:]
    return text[:e].rstrip() + f" {item}" + text[e:]


def remove_list_item(text, name, item):
    s, e = _block(text, name); body = text[s:e]
    body = re.sub(rf"\n[ \t]*{re.escape(item)}(?=\n)", "", body)
    if item in body: body = body.replace(f" {item}", "")
    return text[:s] + body + text[e:]


def _word_array(text, name, add=None, drop=None):
    def sub(m):
        words = [w for w in m[1].split() if w != drop]
        if add and add not in words: words.append(add)
        return f"{name}=({' '.join(words)})"
    return re.sub(rf"^{re.escape(name)}=\(([^)]*)\)", sub, text, count=1, flags=re.M)


# ---- a customer, and the host behind it -----------------------------------------------------------------------
def add_customer(text, spec):
    """Register a customer: its identity, its link into the provider, its LAN link, and the host behind it."""
    c, h = spec["name"], spec["host"]
    for arr, val in (("ROLE", "spoke"), ("REGION", spec["region"]), ("MGMT_IP", spec["mgmt_ip"]),
                     ("T_IDX", spec["t_idx"]), ("LAN", spec["lan"]), ("HOST_OF", h),
                     ("CONSOLE_PORT", spec["console"]), ("NODE_IDX", spec["idx"]),
                     ("PLATFORM", spec.get("platform", "c8000v"))):
        text = add_assoc(text, arr, c, val)
    if spec.get("prefer_hub"):
        text = add_assoc(text, "PREFER_HUB", c, spec["prefer_hub"])
    for arr, val in (("ROLE", "host"), ("MGMT_IP", spec["host_mgmt"]),
                     ("CONSOLE_PORT", spec["host_console"]), ("NODE_IDX", spec["host_idx"])):
        text = add_assoc(text, arr, h, val)
    # LINKS speaks in bare port numbers ("mpls:7 cust4:2"); the inventory and the UI speak in names
    num = lambda port: "".join(ch for ch in str(port) if ch.isdigit())     # noqa: E731
    text = add_list_item(text, "LINKS", f'"{spec["provider"]}:{num(spec["provider_port"])} {c}:2 {spec["wan_prefix"]}"',
                         comment=f"{c}: added by the portal")
    text = add_list_item(text, "LINKS", f'"{c}:{num(spec["lan_port"])} {h}:1 {spec["lan"]}"')
    if spec.get("dual_homed"):
        text = add_list_item(text, "LINKS", dual_line(c, spec))
    text = _word_array(text, "SPOKES", add=c)
    return _word_array(text, "HOSTS", add=h)


def remove_customer(text, spec):
    c, h = spec["name"], spec["host"]
    for arr in ("ROLE", "REGION", "MGMT_IP", "T_IDX", "LAN", "HOST_OF", "CONSOLE_PORT", "NODE_IDX", "PLATFORM", "PREFER_HUB"):
        if re.search(rf"^(?:declare -A )?{arr}=\(", text, re.M):
            text = remove_assoc(text, arr, c)
    for arr in ("ROLE", "MGMT_IP", "CONSOLE_PORT", "NODE_IDX"):
        text = remove_assoc(text, arr, h)
    s, e = _block(text, "LINKS")
    body = re.sub(rf'\n[ \t]*"[^"\n]*\b(?:{re.escape(c)}|{re.escape(h)}):[^"\n]*"', "", text[s:e])
    body = re.sub(rf"\n[ \t]*# {re.escape(c)}: added by the portal(?=\n)", "", body)
    text = text[:s] + body + text[e:]
    text = _word_array(text, "SPOKES", drop=c)
    return _word_array(text, "HOSTS", drop=h)


def dual_line(c, spec):
    """The second provider's link: the provider anchors it (.1), the customer's port 4 is .2."""
    num = "".join(ch for ch in str(spec["provider2_port"]) if ch.isdigit())
    return f'"{spec["provider2"]}:{num} {c}:4 {spec["wan2_prefix"]}"'


def modify_customer(text, c, h, old, new):
    """Change what can change about a customer in place: its region, its site LAN (and so its host's link), its router
    platform and its preferred hub. Its identity — name, index, addresses, ports — stays."""
    if new["region"] != old["region"]:
        text = set_assoc(text, "REGION", c, new["region"])
    if new["platform"] != old["platform"]:     # c8000v is the default: it is written only as the absence of an entry
        text = set_assoc(text, "PLATFORM", c, new["platform"]) if new["platform"] != "c8000v" else remove_assoc(text, "PLATFORM", c)
    if new.get("prefer_hub") != old.get("prefer_hub"):
        if not re.search(r"^declare -A PREFER_HUB=\(", text, re.M):
            text = text.replace("\n# ---- the wiring", "\ndeclare -A PREFER_HUB=( )\n\n# ---- the wiring", 1)
        text = set_assoc(text, "PREFER_HUB", c, new["prefer_hub"]) if new.get("prefer_hub") else remove_assoc(text, "PREFER_HUB", c)
    if new.get("dual_homed") != old.get("dual_homed"):
        if new.get("dual_homed"):
            text = add_list_item(text, "LINKS", dual_line(c, new))
        else:
            s, e = _block(text, "LINKS")
            text = text[:s] + re.sub(rf'\n[ \t]*"{re.escape(old["provider2"])}:\d+ {re.escape(c)}:4 [^"\n]*"', "", text[s:e]) + text[e:]
    if new["lan"] != old["lan"]:
        text = set_assoc(text, "LAN", c, new["lan"])
        s, e = _block(text, "LINKS")
        body = re.sub(rf'("{re.escape(c)}:\d+ {re.escape(h)}:\d+ ){re.escape(old["lan"])}"', rf'\g<1>{new["lan"]}"', text[s:e])
        text = text[:s] + body + text[e:]
    return text
