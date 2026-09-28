"""The demo's voice-over: the script, one line per scene, and its synthesis with Piper (offline neural TTS).

    python docs/presentation/narration.py          # writes recordings/voice/<key>.wav and durations.json

Needs `pip install piper-tts` and a voice (default: en_US-lessac-high, a female US English voice, from
huggingface.co/rhasspy/piper-voices) in ~/.cache/c8d/piper/, or PIPER_VOICE=/path/to/voice.onnx.
demo.py --narrated paces the walkthrough to these durations; make_video.py --narrated mixes them in.

The lines are written to be spoken: acronyms spelled out ("D M V P N"), names as they sound ("Vy O S")."""
import hashlib
import json
import os
import wave
from pathlib import Path

D = Path(__file__).resolve().parent
VOICE_DIR = D / "recordings" / "voice"
MODEL = Path(os.environ.get("PIPER_VOICE", Path.home() / ".cache/c8d/piper/en_US-lessac-high.onnx"))

LINES = {
    # ---- slides ------------------------------------------------------------------------------------------------------
    "s_title": "This is the C eight thousand V, D M V P N portal. One place to see, change, prove and share a multi-hub "
               "D M V P N service, for the operators who run it, and for the customers who use it.",
    "s_problem": "Running a D M V P N service by hand does not scale. Health is scattered across dozens of C L I sessions. "
                 "Every change touches hubs, spokes, providers and Nautobot. Configurations drift, and nobody can say who "
                 "changed what, or how long a failover really takes.",
    "s_what": "The portal sits on a single source of truth, lab dot conf. It reads every router live, turns every change into "
              "a controlled and verified job, and pushes configuration as code, to Catalyst eight thousand V routers, and to Vy O S.",
    "s_caps": "It covers eight areas: see, change, control, audit, prove, secure, customers, and automation. Let's take a tour.",
    "s_tour": "First, the portal, as an administrator.",
    "s_customers": "Next, what a customer sees.",
    "s_nautobot": "Behind the portal, Nautobot is the source of truth.",
    "s_nbmodel": "Customers are modelled as tenants, applications as virtual servers, and the V P N as devices, tunnel interfaces, "
                 "prefixes, B G P sessions, and a config context. Secrets are never stored there.",
    "s_monitoring": "Monitoring is built in. The portal measures the Catalyst routers itself, and publishes the results to Prometheus. "
                    "The providers report through exporters and Telegraf, and router syslog goes to Victoria Logs.",
    "s_results": "The results. Every spoke is registered with all three hubs, all twenty hosts reach each other, and all seventy-three "
                 "tests pass. A hub failure costs at most six tenths of a second, and dual-homed customers fail over in under seven.",
    "s_summary": "One source of truth. Every change a job. Resilience you can measure. Customers who see their own service. "
                 "And security by default. That is the C eight thousand V, D M V P N portal.",
    # ---- the portal, as an administrator -----------------------------------------------------------------------------
    "p_cloud": "The cloud view reads every hub and customer router, live: N H R P registrations, I P sec, B G P, and both "
               "providers, on one screen.",
    "p_drift": "Health checks everything the model expects. Drift detection compares each router's running configuration "
               "with what lab dot conf renders.",
    "p_map": "The network map is drawn from the routers' live state: three hubs, two providers, and every customer with its LAN host. "
             "Tunnels are coloured by health, and each customer's preferred hub is drawn in bold.",
    "p_details": "Selecting a customer shows its company, and the applications it subscribes to, each one checked every minute.",
    "p_phase3a": "Now, D M V P N phase three. The portal clears the shortcut between two customers, and traces the path, again and again.",
    "p_phase3b": "The first packets go through a hub. Then N H R P builds a direct tunnel, customer to customer, shown in purple.",
    "p_sla": "Service levels are measured for each customer: availability, latency, loss and tunnel uptime, against the S L A, "
             "with a monthly P D F report.",
    "p_res1": "On the resilience page, you fail a hub, a provider or a circuit on purpose, while every host pings every other host.",
    "p_res2": "Each experiment is broken down flow by flow: who failed over, who was cut off, and for how long.",
    "p_capacity": "The capacity page shows how many more customers fit, and which limit runs out first: today, the provider's "
                  "customer ports. Each hub is measured against its limits: spokes, I P sec sessions, C P U, memory, and the licensed throughput.",
    "p_prov": "In Provision, every change is a job. You pick a task, review its plan, and it runs, with approval wherever the policy asks for it.",
    "p_add1": "Adding a customer takes one form. The router type, preferred hub, addresses, ports and tunnel index are allocated "
              "for you, and checked to be free.",
    "p_add2": "Before anything runs, the plan shows exactly what will change, and the capacity left afterwards. Here, a new "
              "Vy O S router, dual-homed onto both providers, with no hub changes at all.",
    "p_mod": "Modifying a customer works the same way: its company, its applications, its preferred hub, a second provider, "
             "even its router type.",
    "p_mplan": "Again, the plan lists every router the change touches, and whether any of them has to be rebuilt, before anything happens.",
    "p_psk": "Administrators can rotate the D M V P N pre-shared key. The portal picks a random key, applies it everywhere, and "
             "proves that every I K E session was rebuilt with it.",
    "p_cr": "Change control enforces four eyes. Someone other than the requester must approve, inside change windows if you use them.",
    "p_policy": "The policy itself is edited right here: which tasks need approval, whether the approver must be someone else, "
                "emergency changes, and the change windows.",
    "p_jobs": "Every job is listed, with its progress, its duration, and its test results.",
    "p_audit1": "Each job records who asked, who approved, and every step with its result.",
    "p_audit2": "It also keeps a configuration diff for every router. The key rotation changed every one of them, yet the key "
                "itself is never shown.",
    # ---- the customer --------------------------------------------------------------------------------------------------
    "c_view": "When Prairie Grain Logistics signs in, it sees only its own service: its status, its site, and its applications.",
    "c_req": "Customers can ask for changes themselves, and follow each request through approval.",
    "c_diag": "And they can test every application from their own LAN host, right now, without opening a ticket.",
    # ---- Nautobot --------------------------------------------------------------------------------------------------------
    "n_tenants": "Each customer company is a tenant in Nautobot.",
    "n_tenant": "Its details are custom fields, and a relationship links it to the applications it subscribes to.",
    "n_hub": "Now one of the three hubs, and the interfaces that carry the V P N.",
    "n_ifaces": "On each hub, the two D M V P N clouds are real interfaces: tunnel zero over the first provider, and tunnel one over the second.",
    "n_prefix": "Every prefix has a role: the two overlays, the site LANs, and the access links. Each one says what it is for, "
                "so the allocator and the renderer always agree.",
    "n_bgp": "Every B G P session of the design is modelled: hub to hub, hub to customer, and site to provider, "
             "over both clouds and both providers.",
    "n_ctx": "And the D M V P N service itself is a config context. The renderer builds every router's configuration from "
             "Nautobot, byte for byte identical to lab dot conf.",
    # ---- Grafana ---------------------------------------------------------------------------------------------------------
    "g_cloud": "In Grafana, the whole cloud over the last twenty-four hours. The purple markers are the portal's own jobs and failover tests.",
    "g_provider": "Further down: overlay B G P, router C P U, and the provider's own telemetry.",
    "g_logs": "Router syslog sits right next to the metrics.",
    "g_lab": "Even the lab host, every virtual machine, and the outcome of the portal's own jobs are monitored.",
}


def durations():
    p = VOICE_DIR / "durations.json"
    return json.loads(p.read_text()) if p.exists() else {}


def synthesize():
    from piper import PiperVoice
    from piper.config import SynthesisConfig
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    voice = PiperVoice.load(str(MODEL))
    cfg = SynthesisConfig(length_scale=1.04)                    # a touch slower than the default: easier to follow
    have = durations()
    out = {}
    for key, text in LINES.items():
        wav = VOICE_DIR / f"{key}.wav"
        digest = hashlib.sha1(f"{MODEL.name}|{cfg.length_scale}|{text}".encode()).hexdigest()[:12]
        if wav.exists() and have.get(key, {}).get("hash") == digest:
            out[key] = have[key]
            continue
        with wave.open(str(wav), "wb") as w:
            voice.synthesize_wav(text, w, syn_config=cfg)
        with wave.open(str(wav)) as w:
            secs = w.getnframes() / w.getframerate()
        out[key] = {"secs": round(secs, 2), "hash": digest}
        print(f"{key}: {secs:.1f} s")
    (VOICE_DIR / "durations.json").write_text(json.dumps(out, indent=1))
    print(f"{len(out)} lines, {sum(v['secs'] for v in out.values()) / 60:.1f} minutes of speech")


if __name__ == "__main__":
    synthesize()
