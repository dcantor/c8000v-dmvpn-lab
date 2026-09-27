# The portal presentation

`c8000v-dmvpn-portal.pptx` (40 slides, 16:9, with speaker notes) and `c8000v-dmvpn-portal.pdf` present the portal.
They cover the problem it solves, what it does and its capabilities. Then come four sections of annotated screenshots: the tour; Provision in depth, with every task and its dialog, plan and job; change control; and customers, access and the API. On each
screenshot, numbered callouts match the explanations beside it.

| File | Purpose |
|---|---|
| `capture.py` | signs in to the running portal, takes the screenshots into `shots/`, and writes the box of every element a callout points at into `shots/boxes.json` |
| `build.py` | `shots/` → the .pptx |
| `topdf.py` | the .pptx → the .pdf, drawn from the slides themselves, so no office suite is needed |

## Rebuilding

The screenshots need the portal running on :8094 and Playwright, which is in the portal's venv. Close any dialogs
first: the capture signs in as `admin` and as the `prairie` customer.

```bash
webapp/.venv/bin/python docs/presentation/capture.py            # or: capture.py tour | capture.py more
python3 -m venv /tmp/deck && /tmp/deck/bin/pip install -r docs/presentation/requirements.txt
/tmp/deck/bin/python docs/presentation/build.py && /tmp/deck/bin/python docs/presentation/topdf.py
```

The figures on the Results slide come from the lab on 2026-09-27 and are written in `build.py`: update them there.
The PDF uses Liberation Sans in place of Calibri. The footer shows the version from `VERSION`.

The capture only opens dialogs and pages: it starts no job. The Restore slide uploads the oldest backup to show its plan, and the portal keeps that upload as a copy, which the capture then deletes.
