# The portal presentation and demo video

`c8000v-dmvpn-portal.pptx` (61 slides, 16:9, with speaker notes) and `c8000v-dmvpn-portal.pdf` present the portal.
They cover the problem it solves, what it does and its capabilities. Then come four sections of annotated screenshots: the tour; Provision in depth, with every task and its dialog, plan and job; change control; customers, access and the API; Nautobot, where the customers and the VPN are modelled; and monitoring in Grafana. On each
screenshot, numbered callouts match the explanations beside it.

| File | Purpose |
|---|---|
| `capture.py` | signs in to the running portal, takes the screenshots into `shots/`, and writes the box of every element a callout points at into `shots/boxes.json` |
| `build.py` | `shots/` → the .pptx |
| `nb_session.py` | signs in to Nautobot once: it asks for the username and password in the terminal and keeps the session in `~/.cache/c8d/nautobot-state.json`, outside the repo |
| `demo.py` | records the demo walkthrough (the portal as staff and as a customer, Nautobot, Grafana) as browser videos, with captions, a pointer and highlight rings drawn into the page; the recordings go to `recordings/` (not in git) |
| `make_video.py` | the recordings and slides from the PDF → `c8000v-dmvpn-portal-demo.mp4` (1920x1080, H.264, about 6 minutes) |
| `narration.py` | the voice-over script, one line per scene, synthesized with Piper into `recordings/voice/` |
| `topdf.py` | the .pptx → the .pdf, drawn from the slides themselves, so no office suite is needed |

## Rebuilding

The screenshots need the portal running on :8094 and Playwright, which is in the portal's venv. Close any dialogs
first: the capture signs in to the portal as `admin`, `operator` and the `prairie` customer. Grafana and Prometheus are read
anonymously. Nautobot uses the session `nb_session.py` saved; without it, the Nautobot part is skipped.

```bash
webapp/.venv/bin/python docs/presentation/nb_session.py         # once, for the Nautobot pages
webapp/.venv/bin/python docs/presentation/capture.py            # or one part: tour | more | grafana | nautobot
python3 -m venv /tmp/deck && /tmp/deck/bin/pip install -r docs/presentation/requirements.txt
/tmp/deck/bin/python docs/presentation/build.py && /tmp/deck/bin/python docs/presentation/topdf.py
```

The figures on the Results slide come from the lab on 2026-09-27 and are written in `build.py`: update them there.
The PDF uses Liberation Sans in place of Calibri. The footer shows the version from `VERSION`.

The capture only opens dialogs and pages: it starts no job. The Restore slide uploads the oldest backup to show its plan, and the portal keeps that upload as a copy, which the capture then deletes.

## The demo video

`c8000v-dmvpn-portal-demo.mp4` runs about 6 minutes, with captions and no narration. It opens with the title,
problem, solution and capabilities slides, then walks through:
- the portal: the cloud, the map and a phase 3 shortcut forming, SLA, resilience, Provision's plans, change control
  and the rotation job's diff;
- the customer's own view;
- Nautobot: tenant, the hub's tunnels, prefixes, BGP and the config context;
- Grafana.

It closes with the results and summary slides. Recording starts no job.

```bash
webapp/.venv/bin/python docs/presentation/demo.py          # or one segment: portal | customer | nautobot | grafana
/tmp/deck/bin/pip install imageio-ffmpeg                   # when there is no ffmpeg with libx264
/tmp/deck/bin/python docs/presentation/make_video.py
```

### The narrated version

`c8000v-dmvpn-portal-demo-narrated.mp4` (7 min 54 s) is the same tour with a female voice-over: Piper's
`en_US-lessac-high` voice, synthesized offline. The walkthrough is re-recorded so it is paced to the voice. Each caption
starts its line, and the next scene waits until the line is finished.

```bash
/tmp/deck/bin/python docs/presentation/narration.py                   # the voice (needs the voice in ~/.cache/c8d/piper/)
webapp/.venv/bin/python docs/presentation/demo.py --narrated          # recordings paced to it
/tmp/deck/bin/python docs/presentation/make_video.py --narrated
```

The Lessac voice was trained on the Blizzard 2013 Lessac data, which is licensed for research use. Check that licence
before using the narrated video commercially.
