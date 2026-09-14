# ATS Resume Optimizer (merged)

Merges two projects, **neither of which had its logic changed**:

- `skillsyncer_engine/scan.py` — your original SkillSyncer Playwright automation, byte-identical.
- `ats_engine/*.py` — your original coordinate-based PDF keyword injector (coord_extract, coord_rebuild, extract, build_pdf, local_inject, rewrite), byte-identical.

`app.py` is new glue code that wires the two together with a web UI.

## What it does

1. You paste a job description and upload your resume as a **PDF** on the home page.
2. `/scan` writes `jd.txt` / `resume.txt` into `skillsyncer_engine/` and runs `scan.py` exactly like running it from the terminal — this is a real SkillSyncer browser scan, same as before.
3. From the scan's keyword table, it works out which keywords scored **exactly 0% ("Not Found")** — these are the only ones passed on for injection. Keywords in "Frequency Variance" (already present, just underused) are deliberately **not** injected, per your requirement.
4. The results page shows your score, category breakdown, full keyword table, and an **"Optimize & Download Resume"** button.
5. Clicking it runs the original `ats_engine` pipeline (same steps as its old `main.py`) against the **actual PDF you uploaded** — using only those missing keywords — and the browser downloads `optimized_resume.pdf` directly.

## Where to put your API key

Open `app.py` and find this block near the top:

```python
# ---------------------------------------------------------------------------
# >>> PUT YOUR GEMINI API KEY HERE <<<
# Free key, no card: https://aistudio.google.com/apikey
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "PUT_YOUR_GEMINI_API_KEY_HERE")
```

Either:
- Paste your key directly in place of `"PUT_YOUR_GEMINI_API_KEY_HERE"`, **or**
- Set it as an environment variable before running (better if you'll ever share/commit this folder):
  ```bash
  export GEMINI_API_KEY=your_key_here
  ```

If no key is set, it automatically falls back to `local_inject.py` (the zero-API-key TF-IDF backend) instead of failing — lower quality phrasing, but still works.

## Setup

```bash
cd merged
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium     # needed for the SkillSyncer scan step
```

## Run

```bash
python app.py
```

Then open **http://localhost:5000**.

(Change the port with `PORT=5050 python app.py` if 5000 is taken.)

## Project layout

```
merged/
  app.py                    <- NEW glue code (Flask routes, session store, wiring)
  requirements.txt
  templates/
    index.html               <- NEW upload form (JD + resume PDF)
    results.html              <- NEW results + "Optimize & Download" button
  skillsyncer_engine/
    scan.py                  <- UNCHANGED (your SkillSyncer automation)
    jd.txt / resume.txt      <- written fresh on every scan, same as before
    debug/                    <- scan.py's own debug dumps (created at runtime)
  ats_engine/
    coord_extract.py         <- UNCHANGED
    coord_rebuild.py         <- UNCHANGED
    extract.py                <- UNCHANGED
    build_pdf.py              <- UNCHANGED
    local_inject.py           <- UNCHANGED
    rewrite.py                 <- UNCHANGED (Gemini backend)
    fonts/lm_roman/            <- UNCHANGED bundled fonts
  uploads/                   <- uploaded resume PDFs (per scan, uuid-named)
  outputs/                   <- generated optimized PDFs (per scan, uuid-named)
```

## Notes / limitations

- `skillsyncer_engine/scan.py` still has your SkillSyncer login + the specific
  `SCAN_URL` hardcoded exactly as before — untouched, so it should keep working
  the same way it already did.
- The scan step launches a real (non-headless) browser via Playwright and can
  take 20–40s; the optimize step calls Gemini per-bullet and can take
  20–60s. Both block the request until done — fine for local/personal use,
  but if you ever deploy this, run it behind something like gunicorn with a
  longer timeout, or move both steps to a background job.
- Session data (which PDF belongs to which scan) is kept in memory
  (`SESSIONS` dict in `app.py`), so restarting the app clears any
  in-progress "click optimize" links from old scans — just re-run the scan.
- Only `.pdf` resumes are accepted now (not paste-as-text), because the
  injector needs the real PDF file to preserve your exact layout/fonts when
  redrawing bullets.
