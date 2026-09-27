# The portal presentation

`c8000v-dmvpn-portal.pptx` (24 slides, 16:9, with speaker notes) and `c8000v-dmvpn-portal.pdf` present the portal.
They cover the problem it solves, what it does, its capabilities, and a tour of annotated screenshots. On each
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
webapp/.venv/bin/python docs/presentation/capture.py
python3 -m venv /tmp/deck && /tmp/deck/bin/pip install -r docs/presentation/requirements.txt
/tmp/deck/bin/python docs/presentation/build.py && /tmp/deck/bin/python docs/presentation/topdf.py
```

The figures on the Results slide come from the lab on 2026-09-27 and are written in `build.py`: update them there.
The PDF uses Liberation Sans in place of Calibri.
