"""The portal deck: problem, what it does, capabilities, and an annotated tour (shots/ + shots/boxes.json)."""
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

D = Path(__file__).resolve().parent
SHOTS = D / "shots"
BOXES = json.loads((SHOTS / "boxes.json").read_text())

NAVY = RGBColor(0x0B, 0x1B, 0x2E)
TEAL = RGBColor(0x0E, 0x74, 0x90)
TEAL_L = RGBColor(0xE0, 0xF2, 0xF6)
ORANGE = RGBColor(0xEA, 0x58, 0x0C)
INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x5B, 0x67, 0x78)
LINE = RGBColor(0xD6, 0xDD, 0xE5)
BG = RGBColor(0xF6, 0xF8, 0xFA)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
GREEN = RGBColor(0x15, 0x80, 0x3D)
FONT = "Calibri"

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
BLANK = prs.slide_layouts[6]
SW, SH = 13.333, 7.5


def box(sl, x, y, w, h, fill=None, line=None, lw=0.75, shape=MSO_SHAPE.RECTANGLE, radius=None):
    s = sl.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        s.fill.background()
    else:
        s.fill.solid(); s.fill.fore_color.rgb = fill
    if line is None:
        s.line.fill.background()
    else:
        s.line.color.rgb = line; s.line.width = Pt(lw)
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    s.shadow.inherit = False
    return s


def text(sl, x, y, w, h, runs, size=14, color=INK, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=None):
    """runs: a string, or a list of paragraphs; a paragraph is a string or a list of (text, {size, color, bold})."""
    tb = sl.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.04)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    tf.vertical_anchor = anchor
    paras = runs if isinstance(runs, list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if spacing:
            p.space_after = Pt(spacing)
        for t, o in ([(para, {})] if isinstance(para, str) else para):
            r = p.add_run(); r.text = t
            f = r.font; f.name = FONT; f.size = Pt(o.get("size", size)); f.bold = o.get("bold", bold)
            f.color.rgb = o.get("color", color); f.italic = o.get("italic", False)
    return tb


def shape_text(s, t, size=12, color=WHITE, bold=True, align=PP_ALIGN.CENTER):
    tf = s.text_frame
    tf.margin_left = tf.margin_right = Inches(0.03); tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE; tf.word_wrap = True
    lines = t.split("\n")
    for i, l in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run(); r.text = l
        r.font.name = FONT; r.font.size = Pt(size if i == 0 else size - 2); r.font.bold = bold if i == 0 else False
        r.font.color.rgb = color


def header(sl, title, kicker=None, n=None):
    box(sl, 0, 0, SW, 7.5, fill=WHITE)
    box(sl, 0, 0, 0.14, 7.5, fill=TEAL)
    if kicker:
        text(sl, 0.45, 0.28, 9, 0.3, kicker.upper(), size=11, color=TEAL, bold=True)
    text(sl, 0.45, 0.5, 12.2, 0.6, title, size=26, color=NAVY, bold=True)
    if n:
        text(sl, 12.2, 7.08, 0.9, 0.3, str(n), size=10, color=MUTED, align=PP_ALIGN.RIGHT)
    text(sl, 0.45, 7.08, 6, 0.3, "C8000v DMVPN Portal · v0.21.0", size=10, color=MUTED)


def notes(sl, t):
    sl.notes_slide.notes_text_frame.text = t


# ---------- 1. title ----------
def title_slide():
    sl = prs.slides.add_slide(BLANK)
    box(sl, 0, 0, SW, SH, fill=NAVY)
    box(sl, 0, 5.2, SW, 0.06, fill=TEAL)
    # a small DMVPN motif: three hubs, spokes, one shortcut
    hubs = [(9.3, 1.3), (10.8, 1.0), (12.3, 1.3)]
    spokes = [(8.9, 3.6), (10.0, 4.1), (11.2, 4.2), (12.4, 3.7)]
    for hx, hy in hubs:
        for sx, sy in spokes:
            c = sl.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(hx), Inches(hy), Inches(sx), Inches(sy))
            c.line.color.rgb = RGBColor(0x2B, 0x4C, 0x6B); c.line.width = Pt(1)
    c = sl.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(10.0), Inches(4.1), Inches(12.4), Inches(3.7))
    c.line.color.rgb = RGBColor(0xA7, 0x8B, 0xFA); c.line.width = Pt(3)
    for hx, hy in hubs:
        s = box(sl, hx - 0.22, hy - 0.22, 0.44, 0.44, fill=TEAL, shape=MSO_SHAPE.OVAL)
    for sx, sy in spokes:
        box(sl, sx - 0.16, sy - 0.16, 0.32, 0.32, fill=RGBColor(0x94, 0xA3, 0xB8), shape=MSO_SHAPE.OVAL)
    text(sl, 0.8, 1.6, 8, 0.4, "NETWORK AS CODE · DMVPN PHASE 3", size=13, color=RGBColor(0x5E, 0xC8, 0xDB), bold=True)
    text(sl, 0.8, 2.05, 8.2, 1.4, "C8000v DMVPN Portal", size=46, color=WHITE, bold=True)
    text(sl, 0.8, 3.05, 7.6, 1.5, "One place to see, change, prove and share a multi-hub DMVPN service: "
         "for the operators who run it and the customers who use it.", size=18, color=RGBColor(0xCB, 0xD5, 0xE1))
    text(sl, 0.8, 5.55, 11, 0.4, "Three Catalyst 8000v hubs · C8000v and VyOS customers · two providers · Nautobot · Terraform (NAC) · VictoriaMetrics",
         size=13, color=RGBColor(0x94, 0xA3, 0xB8))
    text(sl, 0.8, 6.0, 8, 0.4, "Version 0.21.0 · September 2026", size=13, color=RGBColor(0x94, 0xA3, 0xB8))
    notes(sl, "Title. The portal runs on the lab host at :8094 and fronts the whole lab: lab.conf is the source of truth, "
              "Network-as-Code pushes the Catalyst 8000v routers, SSH pushes the VyOS routers, Nautobot mirrors the model.")


# ---------- 2. problem ----------
def problem_slide(n):
    sl = prs.slides.add_slide(BLANK)
    header(sl, "Running a DMVPN service by hand does not scale", "The problem", n)
    text(sl, 0.45, 1.2, 12.3, 0.6, "A single customer touches three hubs, one or two providers, two router operating systems and a source of truth. "
         "Every question and every change means logging in to several boxes.", size=15, color=MUTED)
    items = [
        ("Scattered state", "Is every spoke registered with every hub? Are shortcuts forming? Health lives in a dozen CLI sessions on IOS XE and VyOS."),
        ("Slow, risky changes", "Adding, changing or removing a customer means consistent edits on hubs, spoke, provider and Nautobot. It is easy to miss one."),
        ("Drift", "Hand edits leave routers differing from the intended design, and nobody notices until something breaks."),
        ("No change control", "Who changed what, who approved it, and what exactly changed on each router? Usually nobody can say."),
        ("Unproven resilience", "Dual hubs and dual providers look good on paper, but how long does failover really take?"),
        ("Blind customers", "Customers cannot see their own service, test it, or ask for changes. SLA reports are assembled by hand."),
        ("Static secrets", "The DMVPN pre-shared key is set once and rarely rotated, because rotating it by hand risks an outage."),
    ]
    x0, y0, cw, ch, gap = 0.45, 2.05, 3.0, 2.2, 0.12
    for i, (h, b) in enumerate(items):
        col, row = i % 4, i // 4
        x, y = x0 + col * (cw + gap), y0 + row * (ch + gap)
        box(sl, x, y, cw, ch, fill=BG, line=LINE, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
        box(sl, x, y + 0.18, 0.07, 0.42, fill=ORANGE)
        text(sl, x + 0.2, y + 0.16, cw - 0.3, 0.45, h, size=15, color=NAVY, bold=True)
        text(sl, x + 0.2, y + 0.62, cw - 0.32, ch - 0.7, b, size=12, color=INK)
    x, y = x0 + 3 * (cw + gap), y0 + ch + gap
    s = box(sl, x, y, cw, ch, fill=NAVY, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
    text(sl, x + 0.2, y + 0.2, cw - 0.4, ch - 0.4, [[("The result", {"bold": True, "size": 15, "color": RGBColor(0x5E, 0xC8, 0xDB)})],
         [("Slow onboarding, outages from avoidable mistakes, and no evidence for customers or auditors.", {"size": 13, "color": WHITE})]], spacing=6)
    notes(sl, "The problem statement: the service is simple to draw but has many moving parts, and without tooling every question costs a round of logins.")


# ---------- 3. what it is ----------
def what_slide(n):
    sl = prs.slides.add_slide(BLANK)
    header(sl, "What the portal does", "The solution", n)
    text(sl, 0.45, 1.2, 12.3, 0.7, [[("One web portal on top of a single source of truth (", {}), ("lab.conf", {"bold": True}),
         ("). It reads every router live, turns every change into a controlled, verified job, and gives each customer a view of its own service.", {})]],
         size=15, color=MUTED)

    def node(x, y, w, h, title, sub, fill=WHITE, line=TEAL, tc=NAVY):
        s = box(sl, x, y, w, h, fill=fill, line=line, lw=1.5, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
        text(sl, x + 0.08, y + 0.1, w - 0.16, 0.35, title, size=13.5, color=tc, bold=True, align=PP_ALIGN.CENTER)
        text(sl, x + 0.08, y + 0.44, w - 0.16, h - 0.5, sub, size=10.5, color=MUTED if tc == NAVY else RGBColor(0xCB, 0xD5, 0xE1), align=PP_ALIGN.CENTER)
        return s

    def arrow(x1, y1, x2, y2, label=None, lx=None, ly=None):
        c = sl.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        c.line.color.rgb = MUTED; c.line.width = Pt(1.5)
        ln = c.line._get_or_add_ln()
        tail = ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"})
        ln.append(tail)
        if label:
            text(sl, lx if lx is not None else (x1 + x2) / 2 + 0.05, ly if ly is not None else (y1 + y2) / 2 - 0.15, 2.2, 0.3, label, size=9.5, color=MUTED)

    node(0.5, 2.3, 2.4, 1.05, "Operators", "viewer · operator · approver · admin")
    node(0.5, 4.3, 2.4, 1.05, "Customers", "their own site, SLA, requests, tests")
    node(3.8, 2.6, 2.9, 2.5, "Portal  :8094", "FastAPI · jobs · change control · SLA probes · failover lab · config history · roles",
         fill=NAVY, line=NAVY, tc=WHITE)
    arrow(2.9, 2.83, 3.8, 3.3); arrow(2.9, 4.83, 3.8, 4.4)
    node(7.6, 1.95, 2.35, 1.0, "lab.conf", "the intended design")
    node(7.6, 3.35, 2.35, 1.0, "Render", "hub, spoke and provider config")
    node(7.6, 4.75, 2.35, 1.0, "Nautobot", "mirrors the model")
    arrow(6.7, 3.2, 7.6, 2.45); arrow(8.775, 2.95, 8.775, 3.35); arrow(6.7, 4.3, 7.6, 5.2)
    node(10.6, 1.55, 2.3, 1.05, "Catalyst 8000v", "Terraform (NAC) → RESTCONF")
    node(10.6, 2.95, 2.3, 1.05, "VyOS", "SSH push, declarative")
    node(10.6, 4.35, 2.3, 1.05, "Providers", "mpls and mpls2 (VyOS)")
    node(10.6, 5.75, 2.3, 0.9, "VictoriaMetrics", "SLA and application probes")
    arrow(9.95, 3.75, 10.6, 2.1); arrow(9.95, 3.85, 10.6, 3.45); arrow(9.95, 3.95, 10.6, 4.85)
    arrow(6.7, 4.9, 10.6, 6.2, "every minute: pings, app checks", 7.4, 6.2)
    text(sl, 0.5, 5.95, 6.3, 0.9, [[("Read path: ", {"bold": True, "color": TEAL}), ("the portal polls every router (RESTCONF, CLI, VyOS op mode) and draws what is really there, not what should be.", {})]],
         size=11.5, color=INK)
    notes(sl, "Architecture: the portal never edits routers ad hoc. Changes go through lab.conf, the renderer and the same push pipeline, then a verification step.")


# ---------- 4. capabilities ----------
def caps_slide(n):
    sl = prs.slides.add_slide(BLANK)
    header(sl, "Capabilities at a glance", "What you can do", n)
    caps = [
        ("See", "Live cloud table and KPIs, a topology map drawn from live state, per-customer details, path trace and 'watch the shortcut form'."),
        ("Change", "Add, modify and remove customers, deploy the model, fix drift, back up and restore, choose the preferred hub or add a second provider."),
        ("Control", "Change requests with four-eyes approval, change windows, emergency changes, and maintenance mode that mutes alerts."),
        ("Audit", "Every job snapshots every router before and after it runs, records who asked and who approved, and shows a diff per router."),
        ("Prove", "SLA per customer (availability, latency, loss), application checks, simulated failures and measured failover times."),
        ("Secure", "Sign-in with roles, admin-only pre-shared key rotation with a random key, and secrets masked in every view and log."),
        ("Customers", "A read-only customer portal: its own site and applications, SLA and PDF report, change requests and self-service tests."),
        ("Automate", "A REST API for everything (Swagger), Prometheus metrics, progress and ETA for each job, and a 73-test Robot suite after each change."),
    ]
    x0, y0, cw, ch, gx, gy = 0.45, 1.3, 3.0, 2.72, 0.12, 0.14
    for i, (h, b) in enumerate(caps):
        col, row = i % 4, i // 4
        x, y = x0 + col * (cw + gx), y0 + row * (ch + gy)
        box(sl, x, y, cw, ch, fill=BG, line=LINE, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.05)
        c = box(sl, x + 0.22, y + 0.24, 0.5, 0.5, fill=TEAL, shape=MSO_SHAPE.OVAL)
        shape_text(c, str(i + 1), size=15)
        text(sl, x + 0.86, y + 0.3, cw - 1, 0.4, h, size=18, color=NAVY, bold=True)
        text(sl, x + 0.22, y + 0.92, cw - 0.4, ch - 1.0, b, size=12.5, color=INK)
    notes(sl, "Eight capability areas. The tour that follows shows each one in the portal, with numbered callouts matching the explanation beside each screenshot.")


def divider(title, sub, n):
    sl = prs.slides.add_slide(BLANK)
    box(sl, 0, 0, SW, SH, fill=NAVY)
    box(sl, 0.8, 3.05, 0.12, 1.3, fill=ORANGE)
    text(sl, 1.15, 2.95, 11, 0.8, title, size=36, color=WHITE, bold=True)
    text(sl, 1.15, 3.75, 11, 0.8, sub, size=17, color=RGBColor(0xCB, 0xD5, 0xE1))
    text(sl, 12.2, 7.08, 0.9, 0.3, str(n), size=10, color=RGBColor(0x94, 0xA3, 0xB8), align=PP_ALIGN.RIGHT)
    return sl


# ---------- annotated screenshots ----------
IMG_X, IMG_Y, IMG_W = 0.45, 1.3, 8.75
IMG_H = IMG_W * 1000 / 1600
PX = 9.5


def shot_slide(n, name, kicker, title, points, takeaway, extra_boxes=None, drop=()):
    """points: explanations, one per callout, in callout order (after extra boxes are merged)."""
    sl = prs.slides.add_slide(BLANK)
    header(sl, title, kicker, n)
    box(sl, IMG_X + 0.04, IMG_Y + 0.06, IMG_W, IMG_H, fill=LINE)                       # a soft shadow
    pic = sl.shapes.add_picture(str(SHOTS / f"{name}.png"), Inches(IMG_X), Inches(IMG_Y), Inches(IMG_W), Inches(IMG_H))
    pic.line.color.rgb = LINE; pic.line.width = Pt(0.75)
    calls = [c for i, c in enumerate(BOXES.get(name, [])) if i not in drop] + (extra_boxes or [])
    assert len(calls) == len(points), f"{name}: {len(calls)} callouts, {len(points)} explanations"
    for i, c in enumerate(calls):
        bx, by, bw, bh = c["box"]
        pad = 0.004
        x, y = IMG_X + (bx - pad) * IMG_W, IMG_Y + (by - pad) * IMG_H
        w, h = (bw + 2 * pad) * IMG_W, max((bh + 2 * pad) * IMG_H, 0.16)
        x, y = max(IMG_X, x), max(IMG_Y, y)
        w, h = min(w, IMG_X + IMG_W - x), min(h, IMG_Y + IMG_H - y)
        r = box(sl, x, y, w, h, line=ORANGE, lw=2.25, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08 if h > 0.5 else 0.25)
        d = 0.34
        cx = min(max(x - d / 2, IMG_X - d / 2 + 0.02), IMG_X + IMG_W - d)
        cy = min(max(y - d / 2, IMG_Y - d / 2 + 0.02), IMG_Y + IMG_H - d)
        b = box(sl, cx, cy, d, d, fill=ORANGE, line=WHITE, lw=1.5, shape=MSO_SHAPE.OVAL)
        shape_text(b, str(i + 1), size=12)
    # explanation panel
    py = IMG_Y
    for i, p in enumerate(points):
        head, body = p if isinstance(p, tuple) else (p, "")
        b = box(sl, PX, py + 0.02, 0.3, 0.3, fill=ORANGE, shape=MSO_SHAPE.OVAL)
        shape_text(b, str(i + 1), size=11)
        tb = text(sl, PX + 0.42, py - 0.02, SW - PX - 0.75, 0.9, [[(head, {"bold": True, "color": NAVY, "size": 13})], [(body, {"size": 11.5})]], size=11.5)
        lines = 1 + max(1, -(-len(body) // 40)) if body else 1
        py += 0.28 + lines * 0.2 + 0.12
    ty = max(py + 0.05, IMG_Y + IMG_H - 1.05)
    box(sl, PX, ty, SW - PX - 0.4, IMG_Y + IMG_H - ty, fill=TEAL_L, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08)
    text(sl, PX + 0.15, ty + 0.08, SW - PX - 0.7, IMG_Y + IMG_H - ty - 0.16, [[("Why it matters  ", {"bold": True, "color": TEAL, "size": 11.5})],
         [(takeaway, {"size": 11.5})]], anchor=MSO_ANCHOR.MIDDLE)
    notes(sl, title + "\n\n" + "\n".join(f"{i + 1}. {p[0] if isinstance(p, tuple) else p}: {p[1] if isinstance(p, tuple) else ''}" for i, p in enumerate(points))
          + "\n\n" + takeaway)
    return sl


def lifecycle_slide(n):
    sl = prs.slides.add_slide(BLANK)
    header(sl, "How a change moves through the portal", "Change lifecycle", n)
    steps = [("Request", "An operator (or a customer) asks: add, modify, remove, deploy, rotate the key…"),
             ("Approve", "Someone else approves it (four eyes). An emergency change needs a reason."),
             ("Window", "It runs now if a change window is open. Otherwise it is scheduled for the next window."),
             ("Run the job", "Snapshot, render, NAC and VyOS push, then verify. Progress and ETA are shown live."),
             ("Prove", "Every router registered, every host reachable, 73 tests, Nautobot == lab.conf."),
             ("Record", "The config diff per router, who asked, who approved and the outcome, kept per job.")]
    x, y, w, h = 0.45, 1.6, 2.05, 1.25
    for i, (t, b) in enumerate(steps):
        s = box(sl, x + i * (w + 0.06), y, w + (0.25 if i < 5 else 0), h, fill=TEAL if i % 2 == 0 else NAVY,
                shape=MSO_SHAPE.CHEVRON if i else MSO_SHAPE.PENTAGON)
        shape_text(s, t, size=15)
        text(sl, x + i * (w + 0.06) + 0.05, y + h + 0.2, w - 0.05, 1.6, b, size=12, color=INK)
    y2 = 4.55
    box(sl, 0.45, y2, 12.4, 2.25, fill=BG, line=LINE, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
    text(sl, 0.7, y2 + 0.18, 6, 0.4, "What is never allowed", size=15, color=NAVY, bold=True)
    text(sl, 0.7, y2 + 0.6, 5.8, 1.6, ["• Approving your own request (four eyes)", "• A viewer or customer starting a job",
                                      "• Rotating the pre-shared key unless you are an admin", "• A secret reaching git, a job log or the screen"], size=12.5, spacing=4)
    text(sl, 6.9, y2 + 0.18, 6, 0.4, "What never needs approval", size=15, color=NAVY, bold=True)
    text(sl, 6.9, y2 + 0.6, 5.7, 1.6, ["• Adding a customer: it takes nothing away", "• Dry runs, tests and drift checks",
                                      "• Customer diagnostics from its own LAN host", "• Reading anything the role allows"], size=12.5, spacing=4)
    notes(sl, "The policy is editable in the portal: which job types need approval, change windows, emergency changes and expiry of stale requests.")


def results_slide(n):
    sl = prs.slides.add_slide(BLANK)
    header(sl, "Measured, not assumed", "Results in the lab", n)
    kpis = [("3 / 3", "hubs every spoke is registered with"), ("20 / 20", "LAN hosts reach every other host"),
            ("33 / 33", "application checks passing"), ("73 / 73", "Robot tests pass after each change"),
            ("0.6 s", "worst failover when a hub fails; 29 of 35 flows never noticed"), ("6.8 s", "worst failover for dual-homed flows when a provider fails"),
            ("54", "IKE SAs re-authenticated with the new pre-shared key"), ("5 m 28 s", "key rotation end to end, one router at a time, then verified")]
    x0, y0, cw, ch, gx, gy = 0.45, 1.35, 3.0, 1.75, 0.12, 0.16
    for i, (k, l) in enumerate(kpis):
        col, row = i % 4, i // 4
        x, y = x0 + col * (cw + gx), y0 + row * (ch + gy)
        box(sl, x, y, cw, ch, fill=BG, line=LINE, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
        text(sl, x + 0.2, y + 0.15, cw - 0.4, 0.7, k, size=32, color=GREEN if i < 4 else TEAL, bold=True)
        text(sl, x + 0.2, y + 0.92, cw - 0.4, 0.8, l, size=12.5, color=INK)
    text(sl, 0.45, 5.25, 12.4, 1.5, [
        [("Provider failure: ", {"bold": True, "color": NAVY}), ("dual-homed customers (cust1, cust4) move to cloud 2 over the second provider. Single-homed customers lose that provider "
          "by design, and the portal shows it as 'cut off' rather than hiding it.", {})],
        [("Hub failure: ", {"bold": True, "color": NAVY}), ("customers keep two other hubs, live shortcuts need no hub, and routing converges when BGP gives up on the failed hub (hold 9 s).", {})]],
        size=12.5, spacing=6)
    notes(sl, "Figures from the lab on 27 September 2026: the health check, the failover measurements on the Resilience page, and the pre-shared key rotation job (CR-0021).")


def summary_slide(n):
    sl = prs.slides.add_slide(BLANK)
    box(sl, 0, 0, SW, SH, fill=NAVY)
    text(sl, 0.8, 0.8, 11, 0.8, "In short", size=34, color=WHITE, bold=True)
    pts = [("One source of truth", "lab.conf drives the configuration, Nautobot and the portal, and drift is detected against it."),
           ("Every change is a job", "It is planned, approved, run, verified and recorded with a before-and-after diff per router."),
           ("Resilience you can measure", "Simulate a failure and get the outage timeline for every flow."),
           ("Customers see their own service", "SLA, applications, requests and self-service diagnostics, and nothing else."),
           ("Secure by default", "Roles, four eyes, key rotation on demand, and secrets never shown.")]
    y = 1.9
    for h, b in pts:
        box(sl, 0.8, y + 0.08, 0.14, 0.5, fill=ORANGE)
        text(sl, 1.15, y, 11, 0.45, h, size=19, color=WHITE, bold=True)
        text(sl, 1.15, y + 0.42, 11, 0.45, b, size=14, color=RGBColor(0xCB, 0xD5, 0xE1))
        y += 0.98
    text(sl, 0.8, 6.85, 11, 0.4, "github.com/dcantor/c8000v-dmvpn-lab · portal on the lab host at :8094 · API at /docs", size=12, color=RGBColor(0x94, 0xA3, 0xB8))
    text(sl, 12.2, 7.08, 0.9, 0.3, str(n), size=10, color=RGBColor(0x94, 0xA3, 0xB8), align=PP_ALIGN.RIGHT)


# ---------- build ----------
title_slide()
n = 2
problem_slide(n); n += 1
what_slide(n); n += 1
caps_slide(n); n += 1
divider("A tour of the portal", "Numbered callouts on each screenshot match the explanations beside it.", n); n += 1

TOUR = [
    ("01_login", "Access", "Everyone signs in, and the role decides what they see", [
        ("Sign in", "Staff roles are viewer, operator, approver and admin. A customer account sees only its own service. Sessions are signed cookies, passwords are PBKDF2 hashes.")],
     "Read-only users cannot start jobs, nobody approves their own change, and customers never see another customer."),
    ("02_cloud", "See", "The cloud: every router, read live", [
        ("KPIs", "Hubs, customers, registrations, shortcuts and BGP at a glance."),
        ("Every router", "NHRP registrations, IPsec, overlay BGP, the provider and the preferred hub, read from the routers themselves."),
        ("Both providers", "Access links and eBGP on mpls and mpls2."),
        ("Who you are", "The account, its role and the portal version, always visible.")],
     "One screen answers 'is every spoke registered with every hub?' with no CLI logins."),
    ("03_drift", "See", "Health and configuration drift", [
        ("Health", "Everything the model expects to be up is checked, and anything missing is named."),
        ("Drift", "Each router is compared with what lab.conf renders. 'Fix drift' puts it back as a controlled job.")],
     "Hand edits no longer go unnoticed: drift is visible per router, and fixing it is one approved job."),
    ("04_map", "See", "A topology map drawn from live state", [
        ("The map", "Hubs, providers, customers and LAN hosts. Tunnel colour shows health, and the preferred hub is drawn bold."),
        ("Layers", "Turn provider 2 and cloud 2, shortcuts, hosts and labels on or off."),
        ("Export", "SVG or PNG, light or dark, exactly as drawn."),
        ("Details", "Click a node to see the customer company, its applications (checked every minute) and the router's live state.")],
     "Everyone gets the same live picture, from operations to account managers and customers."),
    ("05_path", "See", "Path trace: watch the phase 3 shortcut form", [
        ("First via a hub, then direct", "The first packets go through a hub. NHRP then builds a spoke-to-spoke shortcut, drawn in purple."),
        ("Details and actions", "Trace, watch the shortcut form, ping every host, and view the config history per router."),
        ("Every trace, hop by hop", "The raw output is kept so you can copy or download it.")],
     "It shows that DMVPN phase 3 works end to end, and makes it easy to explain."),
    ("06_sla", "Prove", "SLA per customer, from VictoriaMetrics", [
        ("Against targets", "Availability, latency, loss and tunnel uptime, each compared with its SLA target."),
        ("Latency to each hub", "Probed every minute from the customer's own LAN host."),
        ("Window and report", "24 hours, 30 days or this month, with a monthly PDF report for the customer.")],
     "SLA reporting is automatic and based on measurements the customer can check."),
    ("07_resilience", "Prove", "Simulate failures on purpose", [
        ("Choose a failure", "A hub, a provider, or a customer circuit. The page says what should happen before you press anything."),
        ("Measure failover", "Every LAN host pings every other host five times a second while the fault is in."),
        ("Every measurement", "Flows unaffected, failed over or cut off, the worst failover and the time back to healthy.")],
     "Resilience becomes a number rather than a claim, for example 0.6 s worst case for a hub failure."),
    ("08_failover", "Prove", "One failover experiment, flow by flow", [
        ("The experiment", "What failed, for how long, and the headline figures."),
        ("Each flow", "The verdict and an outage timeline for every host pair, so you can see which paths moved and which were cut.")],
     "You can see exactly which customers a failure affects, and for how long."),
    ("09_provision", "Change", "Provision: every change is a job", [
        ("Tasks", "Add, modify and remove customers, deploy, check for and fix drift, back up, restore, dry run and tests."),
        ("Pre-shared key", "Admins rotate the DMVPN key. The system picks a random 40-character key, and only its fingerprint is ever shown.")],
     "Operators pick what they want done, and the portal carries it out consistently on every router."),
    ("10_modify", "Change", "Modify a customer, with the plan shown first", [
        ("Router type", "Move a customer between C8000v and VyOS."),
        ("Preferred hub", "Pick the hub its traffic should prefer (local-preference 200)."),
        ("Second provider", "Dual-home the customer onto mpls2 and cloud 2."),
        ("The plan", "Exactly what will change on which routers, before anything runs.")],
     "No surprises: the impact is visible and reviewed before a change request is filed."),
    ("11_changes", "Control", "Change requests: four eyes and change windows", [
        ("Change requests", "Each request shows who asked, what for, who approved or rejected it, and its status, including requests from customers.")],
     "Disruptive work cannot start without a second person, and it runs only inside the agreed windows."),
    ("12_job", "Audit", "A job: who, what, and the proof", [
        ("Who", "Who started the job and who approved it."),
        ("Progress", "Live progress and the time it took."),
        ("What changed", "The before-and-after diff per router. Secrets are masked but their change is still visible."),
        ("Every step", "Each step's result, for example '54 IKE SAs on 8 routers, all made after the rotation'.")],
     "Every change leaves evidence: the steps, the approvals and the exact configuration difference."),
    ("13_tools", "Secure", "Lab tools and accounts", [
        ("Accounts and roles", "Admins add, disable and delete accounts and set passwords. Accounts still on their lab-default password are flagged."),
        ("Every tool", "Nautobot, the API, the lab hub and the repository, each with how to sign in.")],
     "Access is managed in one place, with visible warnings when the defaults have not been changed."),
    ("14_customer", "Customers", "The customer's own view", [
        ("Status", "Service status, incidents and planned maintenance for this customer."),
        ("Its own site", "The map, cut down to the customer's own site and the hubs it uses."),
        ("Its applications", "Its company details and live application status."),
        ("Its own pages", "Network, service levels, requests and diagnostics. Nothing else is reachable.")],
     "Customers can answer their own questions, which means fewer tickets and more trust."),
    ("15_requests", "Customers", "Customer self-service requests", [
        ("Ask for a change", "Applications, the preferred hub or a second provider, with a reason."),
        ("Track it", "Each request becomes a change request for the operators, and its progress is shown here.")],
     "Customers can request changes without email, and the request still goes through staff approval."),
    ("16_diag", "Customers", "Self-service diagnostics", [
        ("Tests from its LAN host", "The customer tests its applications, the hubs, or traces a path."),
        ("Every application, every hub", "Round-trip time, loss and HTTP status, run on demand.")],
     "The first-line diagnosis is done by the customer in seconds, with no ticket needed."),
]
EXTRA = {"12_job": [{"label": "steps", "box": [0.05, 0.085, 0.45, 0.375]}]}
for name, kicker, title, pts, take in TOUR:
    # 12: order the manual "steps" box last (matches the explanation order)
    shot_slide(n, name, kicker, title, pts, take, extra_boxes=EXTRA.get(name)); n += 1

lifecycle_slide(n); n += 1
results_slide(n); n += 1
summary_slide(n)

out = D / "c8000v-dmvpn-portal.pptx"
prs.save(out)
print(out, len(prs.slides), "slides")
