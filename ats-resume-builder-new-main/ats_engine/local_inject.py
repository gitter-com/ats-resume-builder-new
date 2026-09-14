"""
local_inject.py
Fully offline ATS keyword injection. No API key, no Ollama, no external model
of any kind -- just TF-IDF similarity (scikit-learn, runs on-device) to find
the best-fitting existing bullet for each missing keyword, and a small set of
hand-written connector templates to weave it in naturally.

This is intentionally simpler than an LLM rewrite: it won't rephrase a whole
sentence, it appends a short natural clause containing the keyword to the
bullet that's already closest to that topic. That's a deliberate trade-off
for zero dependencies / zero cost / deploys anywhere (this is the same
approach used in the AuraATS project this agent is meant to slot into).
"""
import re
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Cycled so repeated keywords/bullets don't all read identically.
TEMPLATES = [
    "with hands-on {kw} experience",
    "while applying {kw} best practices",
    "leveraging {kw} throughout the process",
    "backed by working knowledge of {kw}",
    "with a strong focus on {kw}",
    "incorporating {kw} into the workflow",
    "ensuring alignment with {kw} standards",
]

# A few keywords read better with a different grammatical shape than the
# generic templates above; add overrides here as you find awkward cases.
OVERRIDES = {
    "goal": "in pursuit of clear delivery Goals",
    "technology": "staying current with emerging Technology trends",
    "excellent communication skills": "supported by Excellent Communication Skills across cross-functional teams",
}


def already_present(keyword: str, text: str) -> bool:
    kw = keyword.strip()
    if not kw:
        return False
    escaped = re.escape(kw)
    pattern = rf"\b{escaped}\b"
    return bool(re.search(pattern, text, re.IGNORECASE))


def _clause_for(keyword: str, idx: int) -> str:
    key = keyword.strip().lower()
    if key in OVERRIDES:
        return OVERRIDES[key]
    template = TEMPLATES[idx % len(TEMPLATES)]
    return template.format(kw=keyword.strip())


def inject_keywords(bullets: list[str], keywords: list[str], max_per_bullet: int = 2) -> tuple[list[str], dict]:
    """
    bullets: candidate sentences the keyword is allowed to be woven into
             (already filtered to exclude Skills/Core Competencies sections
             by the caller -- this function doesn't know about sections).
    keywords: full target list; ones already present in the bullets are
              reported back untouched and skipped.
    Returns (new_bullets, report) where report has
        {"already_present": [...], "added": [...], "unplaced": [...]}
    """
    report = {"already_present": [], "added": [], "unplaced": []}
    missing = []
    joined = " ".join(bullets)
    for kw in keywords:
        if already_present(kw, joined):
            report["already_present"].append(kw)
        else:
            missing.append(kw)

    if not missing or not bullets:
        report["unplaced"] = missing
        return bullets, report

    vectorizer = TfidfVectorizer(stop_words="english")
    bullet_vecs = vectorizer.fit_transform(bullets)
    kw_vecs = vectorizer.transform(missing)
    sims = cosine_similarity(kw_vecs, bullet_vecs)  # shape (n_missing, n_bullets)

    new_bullets = list(bullets)
    insert_count = [0] * len(bullets)
    tmpl_idx = 0

    for i, kw in enumerate(missing):
        order = sims[i].argsort()[::-1]  # best-matching bullets first
        placed = False
        for bullet_idx in order:
            if insert_count[bullet_idx] >= max_per_bullet:
                continue
            clause = _clause_for(kw, tmpl_idx)
            tmpl_idx += 1
            b = new_bullets[bullet_idx].rstrip()
            if b.endswith("."):
                new_bullets[bullet_idx] = b[:-1] + f", {clause}."
            else:
                new_bullets[bullet_idx] = b + f", {clause}"
            insert_count[bullet_idx] += 1
            report["added"].append(kw)
            placed = True
            break
        if not placed:
            report["unplaced"].append(kw)

    return new_bullets, report
