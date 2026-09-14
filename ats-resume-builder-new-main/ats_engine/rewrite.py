"""
rewrite.py
Cloud-quality keyword injection via Gemini 2.5 Flash (free tier, no card:
https://aistudio.google.com/apikey). Same input/output contract as
local_inject.inject_keywords() so main.py can use either interchangeably:

    new_bullets, report = inject_keywords(bullets, keywords, api_key=...)

report = {"already_present": [...], "added": [...], "unplaced": [...]}
"""
import os
import json
import requests
import re

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_INSTRUCTIONS = """You are an ATS resume optimization engine.

You receive a JSON array of resume bullet points and a list of target ATS
keywords. For each keyword:
- If it's already present (exact or near-exact phrase, case-insensitive) anywhere
  in the bullets, leave everything alone and report it under "already_present".
- If missing, first try to weave it naturally into the ONE existing bullet it
  fits best, as something the person actually did. Reword that bullet minimally
  so the keyword reads naturally in context -- never just tack the raw keyword
  onto the end. Never fabricate achievements, employers, numbers, or claim
  hands-on ownership of a technology the bullet doesn't already imply.
- If no bullet's actual work matches the keyword closely enough to claim direct
  experience, still place it -- but as an honest, low-commitment mention rather
  than a fabricated achievement. Attach a short clause to the most topically
  related bullet using phrasing like "with working knowledge of X", "exposure to
  X", or "familiar with X". This is still true even for keywords from a fairly
  different domain (e.g. mobile/QA tooling on a backend-heavy resume) -- pick
  whichever existing bullet is the closest thematic fit (testing bullet for
  QA/test tooling, deployment bullet for CI/CD or DevOps tooling, etc.) and
  attach it there. Only fall back to "unplaced" if a keyword truly cannot be
  attached to ANY bullet even as a passing mention (e.g. it contradicts the
  resume, like a competing framework claim).
- Keep each rewritten bullet close to its original length (roughly +-15 words
  per keyword added) -- it has to redraw into roughly the same amount of space
  in the final PDF, so a much longer bullet will get font-shrunk to fit. Do not
  add more than 2 keywords into any single bullet.
- Every bullet you were given must appear in your output, in the same order,
  whether you changed it or not.

Output STRICT JSON ONLY, no markdown fences, no preamble, matching exactly:
{
  "bullets": ["rewritten or unchanged bullet 1", "bullet 2", "..."],
  "already_present": ["..."],
  "added": ["..."],
  "unplaced": ["..."]
}
"""
def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)   # **bold** -> bold
    text = re.sub(r"__(.*?)__", r"\1", text)        # __bold__ -> bold
    return text

def _extract_json(raw_text: str) -> dict:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        cleaned = raw_text.strip().strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1:
            cleaned = cleaned[start:end + 1]
        return json.loads(cleaned)


def inject_keywords(bullets: list[str], keywords: list[str], api_key: str | None = None,
                     max_per_bullet: int = 2) -> tuple[list[str], dict]:
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key found. Get a free one at https://aistudio.google.com/apikey "
            "and set it: export GEMINI_API_KEY=your_key_here"
        )
    if not bullets or not keywords:
        return bullets, {"already_present": [], "added": [], "unplaced": list(keywords)}

    user_prompt = (
        f"BULLETS (JSON array, {len(bullets)} items):\n{json.dumps(bullets, indent=2)}\n\n"
        f"TARGET KEYWORDS:\n{', '.join(keywords)}\n\n"
        f"Max {max_per_bullet} keywords per bullet."
    )
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }

    last_err = None
    for attempt in range(4):
        try:
            resp = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                # Transient: rate-limited or the model is momentarily
                # overloaded on Google's end. Back off and retry instead
                # of treating it as a hard failure -- a single 503 here
                # used to abandon Gemini entirely and fall back to
                # local_inject for the whole run.
                last_err = requests.exceptions.HTTPError(
                    f"{resp.status_code} Server Error: {resp.reason}", response=resp
                )
                wait = 2 ** attempt  # 1s, 2s, 4s, 8s
                print(f"   (Gemini returned {resp.status_code}, retrying in {wait}s "
                      f"[{attempt + 1}/4]...)")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            print(f"   (Gemini request timed out, retrying {attempt + 1}/4...)")
    else:
        raise RuntimeError(
            "Gemini API kept failing (timeouts or server errors) after 4 attempts. "
            "This is usually Google's servers being briefly overloaded on the free "
            "tier -- try again in a minute, or set GEMINI_MODEL to a different model "
            "(e.g. gemini-1.5-flash) if it keeps happening."
        ) from last_err
    resp.raise_for_status()
    raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    result = _extract_json(raw_text)

    new_bullets = [_strip_markdown(b) for b in result.get("bullets", bullets)]
    if len(new_bullets) != len(bullets):
        # model dropped/added a line -- don't risk misaligning blocks, fall back to originals
        new_bullets = bullets

    already_present = list(result.get("already_present", []))
    added = list(result.get("added", []))
    unplaced = list(result.get("unplaced", []))

    # --- Coverage check: make sure every keyword we sent is accounted for. ---
    # Gemini's JSON sometimes silently drops a keyword from all three lists
    # when handling a longer batch (e.g. 10+ keywords) -- it isn't flagged
    # as already_present, added, or unplaced, it's just missing. Without this
    # check that keyword disappears from the resume with no trace. Any input
    # keyword not accounted for gets treated as unplaced so the force-inject
    # step below guarantees it still ends up in the resume.
    accounted_for = {k.strip().lower() for k in (already_present + added + unplaced)}
    dropped_by_model = [kw for kw in keywords if kw.strip().lower() not in accounted_for]
    if dropped_by_model:
        unplaced.extend(dropped_by_model)

    if unplaced and new_bullets:
        chunk_size = 3
        chunks = [unplaced[i:i + chunk_size] for i in range(0, len(unplaced), chunk_size)]
        n = len(new_bullets)
        for idx, chunk in enumerate(chunks):
            target = n - 1 - (idx % n)
            clause = " Also gained exposure to " + ", ".join(chunk) + "."
            new_bullets[target] = new_bullets[target].rstrip(". ") + "." + clause
        added += unplaced
        unplaced = []

    report = {
        "already_present": already_present,
        "added": added,
        "unplaced": unplaced,
    }
    return new_bullets, report