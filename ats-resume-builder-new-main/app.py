"""
app.py — merged glue app.

Wires two UNTOUCHED automation projects together:
  1. skillsyncer_engine/scan.py   -> real SkillSyncer browser scan (unchanged)
  2. ats_engine/*.py              -> coordinate-based PDF keyword injector (unchanged)

Flow:
  1. User uploads a resume PDF + pastes a JD on the home page.
  2. /scan  -> writes jd.txt/resume.txt, runs scan.py exactly like the
     original SkillSyncer app did, parses the JSON report, and works out
     which keywords actually scored 0% ("Not Found") — NOT the frequency
     variance ones — because those are the only ones that should be
     injected into the resume.
  3. Results page shows the SkillSyncer score/table and an
     "Optimize & Download" button.
  4. /optimize/<sid> -> runs the ORIGINAL ats_agent pipeline (same steps as
     its main.py) using ONLY those missing keywords against the resume PDF
     the user uploaded, and streams the optimized PDF back as a download.

Nothing in skillsyncer_engine/scan.py or ats_engine/*.py was modified.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import time 
import requests
import uuid

from flask import Flask, render_template, request, send_file, abort, jsonify

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLSYNCER_DIR = os.path.join(BASE_DIR, "skillsyncer_engine")
ATS_ENGINE_DIR = os.path.join(BASE_DIR, "ats_engine")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")

SCAN_SCRIPT = os.path.join(SKILLSYNCER_DIR, "scan.py")
JD_FILE = os.path.join(SKILLSYNCER_DIR, "jd.txt")
RESUME_FILE = os.path.join(SKILLSYNCER_DIR, "resume.txt")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

# ats_engine's modules import each other with bare names (e.g. "import
# extract"), exactly like the original ats_agent project did — so we just
# add that folder to sys.path instead of touching any of their imports.
sys.path.insert(0, ATS_ENGINE_DIR)
import coord_extract   # noqa: E402
import coord_rebuild   # noqa: E402
import extract         # noqa: E402
import build_pdf       # noqa: E402
import local_inject    # noqa: E402

# ---------------------------------------------------------------------------
# >>> PUT YOUR GEMINI API KEY HERE <<<
# Free key, no card: https://aistudio.google.com/apikey
# You can either paste it directly on the line below, or set it as an
# environment variable GEMINI_API_KEY before running the app (recommended
# for anything other than local testing) — the env var wins if both are set.
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

DEFAULT_SKIP_SECTIONS = ["Technical Skills", "Skills", "Core Competencies", "Competencies"]
MAX_KEYWORDS_PER_BULLET = 4

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB upload cap

# scan.py always reads jd.txt/resume.txt from disk, so two scans running at
# the same time would clobber each other's files. This lock forces
# "write files, then run scan.py" to happen as one atomic block (same
# protection the original skillsyncer app.py had).
scan_lock = threading.Lock()

# In-memory session store: sid -> {"resume_pdf": path, "missing_keywords": [...], "report": {...}}
# Fine for a single local dev server. Restarting the app clears it.
SESSIONS = {}


# ---------------------------------------------------------------------------
# SkillSyncer scan (unchanged automation, just invoked from Flask)
# ---------------------------------------------------------------------------

def extract_resume_text(pdf_path: str) -> str:
    import pdfplumber
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts).strip()


def run_scan_py() -> dict:
    """Runs scan.py exactly as you'd run it from the terminal, then parses
    the JSON block it prints to stdout. scan.py itself is untouched."""
    result = subprocess.run(
        [sys.executable, SCAN_SCRIPT],
        cwd=SKILLSYNCER_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"scan.py exited with an error:\n{result.stderr[-2000:]}")

    stdout = result.stdout
    match = re.search(r"\{.*\}", stdout, re.DOTALL)
    if not match:
        raise RuntimeError(f"Couldn't find JSON in scan.py output:\n{stdout[-2000:]}")
    return json.loads(match.group(0))


def compute_missing_keywords(report: dict) -> list:
    """Only keywords that scored 0% ('Not Found') across every category —
    NOT the frequency-variance ones — since those are already present in
    the resume and just need more repetition, not injection."""
    missing = []
    seen = set()
    for _category, data in (report.get("category_breakdown") or {}).items():
        if not data:
            continue
        for kw in data.get("not_found", []):
            keyword = (kw.get("keyword") or "").strip()
            key = keyword.lower()
            if keyword and key not in seen:
                seen.add(key)
                missing.append(keyword)
    return missing


# ---------------------------------------------------------------------------
# ATS keyword injector (unchanged automation — same steps as ats_agent's
# main.py, just wired to a Flask request instead of argv)
# ---------------------------------------------------------------------------

def optimize_resume(resume_pdf_path: str, keywords: list, out_path: str) -> dict:
    """
    Optimizes resume with missing keywords while guaranteeing:
      1. Consistent, uniform font size across all paragraphs & bullets (no small/big mixed sizes).
      2. Uniform title sizing and clean professional dividers.
      3. Strict margins (no content going outside the resume boundaries).
      4. Graceful fallback to coordinate rebuild if needed.
    """
    api_key = GEMINI_API_KEY if GEMINI_API_KEY and "PUT_YOUR_GEMINI_API_KEY_HERE" not in GEMINI_API_KEY else None

    # First attempt: Clean full-resume builder (solves mixed font sizes, overflow, and broken alignment)
    try:
        return build_pdf.build_clean_resume(resume_pdf_path, keywords, out_path, api_key=api_key)
    except Exception:
        traceback.print_exc()

    # Fallback: Coordinate overlay
    skip_titles = sorted({s.strip().lower() for s in DEFAULT_SKIP_SECTIONS})
    blocks = coord_extract.get_blocks(resume_pdf_path, skip_titles=skip_titles)
    # Filter eligible blocks: exclude headings, skip-sections (skills), page numbers, and tiny fragments
    eligible = [
        b for b in blocks
        if not b.get("is_heading")
        and not b.get("skip")
        and not b.get("is_page_num")
        and len(b.get("text", "").split()) >= 4
        and "playwright" not in b.get("text", "").lower()
    ]
    bullet_texts = [
        re.sub(r"^(\(cid:\d+\)|[\u2022\u2023\u25e6\u25aa\u25ab\u25cf\u25cb\uf0b7\uf0a7\x7f\-\*\–\—])\s*", "", b["text"]).strip()
        for b in eligible
    ]

    # Distribute keywords evenly: 1 per bullet when plenty of bullets exist, else up to MAX_KEYWORDS_PER_BULLET
    max_per = 1 if len(eligible) >= len(keywords) else min(2, MAX_KEYWORDS_PER_BULLET)

    backend_used = "gemini"
    if api_key:
        import rewrite
        try:
            new_texts, report = rewrite.inject_keywords(
                bullet_texts, keywords, api_key=api_key, max_per_bullet=max_per
            )
        except Exception as e:
            backend_used = f"local (gemini failed: {e})"
            new_texts, report = local_inject.inject_keywords(
                bullet_texts, keywords, max_per_bullet=max_per
            )
    else:
        backend_used = "local (no GEMINI_API_KEY set)"
        new_texts, report = local_inject.inject_keywords(
            bullet_texts, keywords, max_per_bullet=max_per
        )

    edited = []
    for b, old_text, new_text in zip(eligible, bullet_texts, new_texts):
        if new_text.strip() != old_text.strip():
            prefix = "\u2022 " if b["is_bullet"] else ""
            edited.append({**b, "new_text": prefix + new_text})

    with tempfile.TemporaryDirectory() as tmp:
        font_files = extract.extract_embedded_truetype_fonts(resume_pdf_path, tmp)
        registered = build_pdf.register_fonts(font_files, tmp)
        reverted = coord_rebuild.rebuild(resume_pdf_path, edited, registered, out_path, all_blocks=blocks)

    return {
        "backend_used": backend_used,
        "blocks_edited": len(edited),
        "blocks_reverted": len(reverted) if reverted else 0,
        "already_present": report.get("already_present", []),
        "added": report.get("added", []),
        "unplaced": report.get("unplaced", []),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/ping", methods=["GET", "HEAD"])
def ping():
    # Deliberately does nothing but respond fast — used by an external
    # uptime pinger (UptimeRobot/cron-job.org/etc.) to keep the free-tier
    # instance from spinning down after 15 minutes of inactivity. Doesn't
    # touch scan_lock, SESSIONS, or any real work.
    return "ok", 200


# ---------------------------------------------------------------------------
# Self keep-alive (no external pinger needed)
# ---------------------------------------------------------------------------
# Render's free tier spins the instance down after ~15 minutes with no
# incoming HTTP traffic, and the next request then pays a 30-60s+ cold
# start. Rather than depending on an external service (UptimeRobot etc.)
# to remember to hit /ping, the app pings ITSELF every 10 minutes from a
# background thread — comfortably inside the 15 minute idle window — so
# Render always sees recent traffic and never spins it down.
#
# RENDER_EXTERNAL_URL is set automatically by Render on every deploy
# (e.g. "https://your-app.onrender.com"), so this only activates when
# actually running on Render — it's a no-op during local dev.
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")


def _self_ping_loop():
    ping_url = RENDER_EXTERNAL_URL.rstrip("/") + "/ping"
    while True:
        time.sleep(600)  # 10 minutes, safely under Render's 15 minute idle cutoff
        try:
            requests.get(ping_url, timeout=10)
        except Exception:
            pass  # a missed ping just means we try again in 10 minutes


if RENDER_EXTERNAL_URL:
    threading.Thread(target=_self_ping_loop, daemon=True).start()


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/scan", methods=["POST"])
def scan():
    jd_text = (request.form.get("jd_text") or "").strip()
    resume_file = request.files.get("resume_file")

    if not jd_text:
        return render_template("index.html", error="Please paste a job description.")
    if not resume_file or not resume_file.filename:
        return render_template(
            "index.html", error="Please upload your resume as a PDF.", jd_text=jd_text
        )
    if not resume_file.filename.lower().endswith(".pdf"):
        return render_template(
            "index.html", error="Resume must be a .pdf file (needed to preserve formatting when injecting keywords).",
            jd_text=jd_text,
        )

    sid = uuid.uuid4().hex
    resume_pdf_path = os.path.join(UPLOADS_DIR, f"{sid}.pdf")
    resume_file.save(resume_pdf_path)

    try:
        resume_text = extract_resume_text(resume_pdf_path)
    except Exception as e:
        return render_template("index.html", error=f"Couldn't read the resume PDF: {e}", jd_text=jd_text)

    if not resume_text:
        return render_template("index.html", error="Couldn't extract any text from that PDF.", jd_text=jd_text)

    try:
        with scan_lock:
            with open(JD_FILE, "w", encoding="utf-8") as f:
                f.write(jd_text)
            with open(RESUME_FILE, "w", encoding="utf-8") as f:
                f.write(resume_text)
            report = run_scan_py()
    except Exception as e:
        traceback.print_exc()
        return render_template("index.html", error=f"Scan failed: {e}", jd_text=jd_text)

    # Guarantee report overall_score is populated with a clean numeric score
    if not report.get("overall_score"):
        kws = report.get("keywords") or []
        w_sum, w_tot = 0.0, 0
        for r in kws:
            try:
                s = float(str(r.get("score", 0)).rstrip("%").strip())
                jc = int(float(str(r.get("job_count", 1)).strip() or 1))
                w_sum += s * jc
                w_tot += jc
            except (ValueError, TypeError):
                pass
        if w_tot > 0:
            report["overall_score"] = str(round(w_sum / w_tot))
        else:
            cat_breakdown = report.get("category_breakdown") or {}
            pcts = []
            for d in cat_breakdown.values():
                p = (d or {}).get("percent")
                if p:
                    try:
                        pcts.append(float(str(p).rstrip("%").strip()))
                    except (ValueError, TypeError):
                        pass
            if pcts:
                report["overall_score"] = str(round(sum(pcts) / len(pcts)))

    if report.get("overall_score"):
        report["overall_score"] = str(report["overall_score"]).rstrip("%").strip()
    missing_keywords = compute_missing_keywords(report)

    SESSIONS[sid] = {
        "resume_pdf": resume_pdf_path,
        "missing_keywords": missing_keywords,
        "report": report,
    }

    return render_template(
        "results.html", report=report, sid=sid, missing_keywords=missing_keywords
    )


@app.route("/optimize/<sid>", methods=["POST"])
def optimize(sid):
    session_data = SESSIONS.get(sid)
    if not session_data:
        return jsonify({"error": "Session expired or not found — please run the scan again."}), 404

    resume_pdf_path = session_data["resume_pdf"]
    missing_keywords = session_data["missing_keywords"]
    out_path = os.path.join(OUTPUTS_DIR, f"{sid}_optimized.pdf")

    try:
        result = optimize_resume(resume_pdf_path, missing_keywords, out_path)
        print("=" * 60)
        print("OPTIMIZE RESULT:", result)
        print("=" * 60)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Optimization failed: {e}"}), 500

    return send_file(
        out_path,
        as_attachment=True,
        download_name="optimized_resume.pdf",
        mimetype="application/pdf",
    )


if __name__ == "__main__":
    port = 5000
    app.run(host="0.0.0.0", port=port, debug=True)
