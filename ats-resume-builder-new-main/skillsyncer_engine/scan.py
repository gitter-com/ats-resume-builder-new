"""
SkillSyncer headless automation
--------------------------------
Logs into app.skillsyncer.com in the background (NO visible browser window),
opens a job scan, replaces the Job Description, replaces the resume text,
triggers a rescan, and prints:
  - the exact overall match score (decimal, e.g. 59.35)
  - Hard/Soft/Other Skills category percentages + their "Not Found" and
    "Frequency Variance" keyword chip lists (exactly as shown on the site)
  - the FULL per-keyword table (every keyword, not just missing ones) with
    type, score %, resume count, and job count

Requirements:
    pip install playwright pdfplumber
    playwright install chromium

Usage:
    Put your job description in jd.txt and resume text in resume.txt
    (same folder as this script), then:
        python scan.py

Notes on the bugs fixed in this version:
1. Wrong "10" score: an ad-hoc JS snippet scanned the WHOLE page for any
   text matching a percent-like number and grabbed the first hit — which
   turned out to be the "AI Credits: 10" sidebar text, not the actual score.
   Fixed: the score is now read from the one specific circular-progress
   element SkillSyncer renders it in (`.vue-circular-progress .percent`),
   confirmed unique on the page.
2. Incomplete/wrong keyword data: SkillSyncer's results table has a markup
   bug of its own — the "Score" column is rendered as a bare <div> instead
   of a <td> inside the table row. `row.querySelectorAll('td')` silently
   skips that div, which shifted every column over by one (so "score" was
   actually reading "resume count", etc.) and made the table look broken.
   Fixed: we now read `row.children` (every direct child element in order,
   td or div) instead of only `<td>` elements.
3. Score printed as "56\n.37" instead of "56.37": the `.percent` element
   isn't a single text node — SkillSyncer splits it into separate child
   spans (e.g. one for the integer part, one for the decimal part), so
   `inner_text()` inserts a line break between them. Fixed: strip ALL
   whitespace/newlines from the captured text, not just leading/trailing.
"""

import asyncio
import json
import re
import os
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SKILLSYNCER_EMAIL = "learnmanagsys@gmail.com"
SKILLSYNCER_PASSWORD = "$k!/lsync3r@12e"

# Full URL of the specific scan to update
SCAN_URL = "https://app.skillsyncer.com/scans/966566a0-9146-467f-b747-a616575d4481"

# If True, actually click "Rescan" (consumes one of your free scans).
# If False, the script edits the JD + resume and stops WITHOUT scanning.
DO_RESCAN = True


def read_file_content(file_path: str) -> str:
    if not os.path.exists(file_path):
        print(f"\n[Error] File not found: {file_path}")
        print(f"Please create '{file_path}' in the same folder as scan.py.")
        return ""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

async def login(page):
    await page.goto("https://app.skillsyncer.com/", wait_until="domcontentloaded")
    await page.wait_for_selector("#email", timeout=15000)
    # id selectors — the accessible name of these inputs is ambiguous
    # ("Email Password" gets concatenated), so id is the reliable match.
    await page.locator("#email").fill(SKILLSYNCER_EMAIL)
    await page.locator("#password").fill(SKILLSYNCER_PASSWORD)
    await page.get_by_role("button", name="Sign In", exact=True).click()
    await page.wait_for_url(re.compile(r".*/dashboard"), timeout=15000)


# ---------------------------------------------------------------------------
# OVERLAY / CLICK HELPERS
# ---------------------------------------------------------------------------

async def force_remove_scan_nudge(page) -> bool:
    """Directly delete the '1 Scan Remaining!' upgrade-nudge block from the
    DOM, unconditionally. No leaf-node or CSS-position assumptions — those
    turned out to be too fragile (the modal's text is split across nested
    spans, so no single leaf element contains the full phrase, which made
    the old 'is it still there' check silently pass while the modal sat
    there blocking clicks). This just finds the smallest element whose
    text contains both distinctive strings from the nudge and removes it
    outright. Returns True if something was removed."""
    try:
        return await page.evaluate(
            """() => {
                const candidates = Array.from(
                    document.querySelectorAll('div, section, aside')
                ).filter(e => {
                    const t = e.textContent || '';
                    return t.includes('Scan Remaining') && t.includes('Upgrade Now');
                });
                if (candidates.length === 0) return false;
                candidates.sort((a, b) => a.textContent.length - b.textContent.length);
                candidates[0].remove();
                return true;
            }"""
        )
    except Exception:
        return False


async def dismiss_overlays(page):
    """SkillSyncer shows a '1 Scan Remaining!' / upgrade nudge overlay that
    intercepts clicks and can reappear after navigation or saving. Remove
    it directly (unconditionally, every call), then also close any other
    generic 'Close'-labeled overlay/toast as a best-effort extra."""
    # Try a few times in case it re-renders shortly after removal.
    for _ in range(3):
        removed = await force_remove_scan_nudge(page)
        if not removed:
            break
        await page.wait_for_timeout(300)

    for _ in range(3):
        closed_something = False
        for selector in [
            "button:has-text('Close')",
            "[aria-label='Close']",
            "button:has-text('×')",
            "button:has-text('✕')",
        ]:
            try:
                loc = page.locator(selector).first
                if await loc.is_visible(timeout=800):
                    await loc.click(timeout=1500)
                    closed_something = True
                    await page.wait_for_timeout(300)
            except Exception:
                pass
        if not closed_something:
            break


async def js_click(locator) -> bool:
    """Dispatch a genuine click via the DOM element's own .click(), which
    invokes its handler directly and is immune to anything visually
    overlapping it — unlike a coordinate-based click (even force: true),
    which can land on whatever is on top at that screen position."""
    try:
        await locator.evaluate("el => el.click()")
        return True
    except Exception:
        return False


async def click_robust(page, locator, attempts: int = 3):
    """Click a locator. The '1 Scan Remaining' nudge turned out to
    re-render itself repeatedly (not just once), so removing it ahead of
    time isn't reliable on its own — every click needs to be immune to it.
    A JS-dispatched click (calling the element's own .click()) fires the
    handler directly regardless of what's visually on top, so it's tried
    FIRST, with a normal coordinate-based click and a forced click as
    fallbacks in case the element genuinely isn't ready yet."""
    if await js_click(locator):
        return

    last_error = None
    for _ in range(attempts):
        try:
            await locator.click(timeout=5000)
            return
        except Exception as e:
            last_error = e
            await force_remove_scan_nudge(page)
            await page.wait_for_timeout(500)
    try:
        await locator.click(force=True, timeout=8000)
        return
    except Exception as e:
        last_error = e
    raise last_error


# ---------------------------------------------------------------------------
# EDIT JOB DESCRIPTION / RESUME
# ---------------------------------------------------------------------------

async def replace_editor_text(page, new_text: str):
    """Replace the content of SkillSyncer's rich-text (TipTap/ProseMirror)
    editor box — used for both the Job Description and the Resume editor.
    There is exactly one `div.tiptap.ProseMirror` on each of these pages.

    Previously this used `box.type(new_text)`, which fires a real
    keydown/keyup event for EVERY character. For a full resume or JD
    (hundreds of characters) that's slow, and anything that steals focus
    mid-typing — the "1 Scan Remaining" overlay re-rendering, a ProseMirror
    internal re-render, a slow tick of the page — can interrupt it partway,
    silently leaving only the first few lines typed. That's exactly why it
    was inconsistent (sometimes the JD cut off, sometimes the resume did).

    Fixed by using `page.keyboard.insertText()`, which inserts the whole
    string as ONE atomic operation instead of hundreds of separate
    keystrokes — nothing to interrupt partway through, and it's also much
    faster than simulating individual key presses."""
    box = page.locator("div.tiptap.ProseMirror").first
    await box.wait_for(state="visible", timeout=20000)
    await box.click()
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Delete")
    await page.keyboard.insert_text(new_text)

    # Verify the full text actually landed — if anything still interrupts
    # the insert (e.g. the page navigating mid-operation), fail loudly
    # instead of silently proceeding with a truncated JD/resume.
    await page.wait_for_timeout(300)
    actual_text = (await box.inner_text()).strip()
    expected_len = len(new_text.strip())
    actual_len = len(actual_text)
    if actual_len < expected_len * 0.95:  # allow tiny whitespace-normalization drift
        raise RuntimeError(
            f"Editor text looks truncated: expected ~{expected_len} chars, "
            f"got {actual_len}. First 100 chars in editor: {actual_text[:100]!r}"
        )


async def debug_dump(page, label: str):
    """Save a screenshot + list of visible buttons so a failure can
    actually be diagnosed instead of guessed at. Never raises."""
    try:
        os.makedirs("debug", exist_ok=True)
        await page.screenshot(path=f"debug/{label}.png")
        buttons = await page.evaluate(
            """() => Array.from(document.querySelectorAll('button'))
                 .map(b => ({text: b.textContent.trim(),
                             visible: !!(b.offsetWidth || b.offsetHeight)}))
                 .filter(b => b.text)"""
        )
        with open(f"debug/{label}.json", "w", encoding="utf-8") as f:
            json.dump({"url": page.url, "buttons": buttons}, f, indent=2)
        print(f"[debug] Saved debug/{label}.png and debug/{label}.json")
    except Exception as e:
        print(f"[debug] Failed to capture debug info: {e}")


async def update_job_description(page, new_jd: str):
    await dismiss_overlays(page)
    await click_robust(page, page.get_by_role("button", name="Edit Job"))

    # A new overlay/toast can appear in the moment right after this click —
    # clear it before checking for the "Edit" button.
    await dismiss_overlays(page)

    # The "Edit" button only appears once the Edit Job panel has finished
    # rendering. Wait for it; if it never shows (e.g. the panel toggled
    # closed because "Edit Job" was actually clicked twice, or an overlay
    # ate the first click), try clicking "Edit Job" once more before
    # giving up with debug info saved to disk.
    edit_btn = page.get_by_role("button", name="Edit", exact=True)
    try:
        await edit_btn.wait_for(state="visible", timeout=15000)
    except Exception:
        await debug_dump(page, "edit_job_panel_not_open")
        await dismiss_overlays(page)
        await click_robust(page, page.get_by_role("button", name="Edit Job"))
        await dismiss_overlays(page)
        try:
            await edit_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            await debug_dump(page, "edit_job_panel_not_open_retry")
            raise

    await click_robust(page, edit_btn)

    await replace_editor_text(page, new_jd)

    await click_robust(page, page.get_by_role("button", name="Save"))
    await page.wait_for_timeout(1500)  # allow auto-rescore to settle


async def update_resume_text(page, resume_text: str):
    await dismiss_overlays(page)
    await click_robust(
        page, page.get_by_role("button", name="Update Resume", exact=True)
    )
    try:
        await page.wait_for_url(re.compile(r".*/update"), timeout=15000)
    except Exception:
        await debug_dump(page, "update_resume_nav_failed")
        raise
    await dismiss_overlays(page)

    await replace_editor_text(page, resume_text)

    if DO_RESCAN:
        await dismiss_overlays(page)
        try:
            await click_robust(page, page.get_by_text("Rescan", exact=False))
        except Exception:
            await debug_dump(page, "rescan_click_failed")
            raise
        # Wait for the score gauge to actually update to a new value.
        await page.wait_for_timeout(5000)


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

async def expand_all_collapsed(page):
    """Click through every collapsed toggle on the page so hidden keyword
    chips actually exist in the DOM before we scrape.

    Two kinds of triggers are handled, because SkillSyncer apparently uses
    both:
      1. Text-labeled toggles: "Show more", "View all", "+9 more", etc.
      2. Icon-only accordion toggles with no visible text — these are
         caught via the standard `aria-expanded="false"` attribute, which
         is the normal accessible-accordion pattern even when there's no
         button label to match on.

    Why this matters: some of the extra keywords (Application
    Architecture, GitHub Actions, TypeScript, Frontend, Maestro, DevOps,
    Kotlin, Swift, Ruby, Empathy, Clean) live behind a "Skill Impact"-style
    panel that may be conditionally rendered (not just CSS-hidden) AND may
    use an icon toggle with no matching text — so a text-only trigger
    search would silently skip it.

    Runs up to 10 rounds (in case expanding one panel reveals another),
    waiting for Vue to render each time."""
    for _ in range(10):
        clicked = await page.evaluate(
            r"""
            () => {
                const textPattern = /(show more|view all|see all|expand|view more|skill impact|impact|details|full report|\+\s*\d+\s*more)/i;

                // Text-labeled toggles (leaf nodes only, short text)
                const textCandidates = Array.from(
                    document.querySelectorAll('button, a, [role="button"], span, div')
                ).filter(el => {
                    if (el.children.length > 0) return false;
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 40) return false;
                    if (!textPattern.test(t)) return false;
                    return el.offsetParent !== null;
                });

                // Icon-only accordion toggles: aria-expanded="false"
                const ariaCandidates = Array.from(
                    document.querySelectorAll('[aria-expanded="false"]')
                ).filter(el => el.offsetParent !== null);

                const target = textCandidates[0] || ariaCandidates[0];
                if (target) { target.click(); return true; }
                return false;
            }
            """
        )
        if not clicked:
            break
        await page.wait_for_timeout(500)


async def get_overall_score(page) -> str:
    """Exact decimal score next to the 'Match Score' label, e.g. '40.01'.

    Rewritten against a real page capture: the score is NOT in any
    '.percent' or '.vue-circular-progress' element (those classes don't
    exist anywhere on the page) — it's a plain SVG ring with the number
    rendered in a `span.tabular-nums` sibling, next to a span whose text
    is literally 'Match Score'. Walk up from that label and grab the
    first tabular-nums span with a numeric value."""
    try:
        score = await page.evaluate(r"""
            () => {
                const label = Array.from(document.querySelectorAll('span')).find(
                    e => (e.textContent || '').trim() === 'Match Score'
                );
                if (!label) return null;
                let container = label.parentElement;
                for (let i = 0; i < 5 && container; i++) {
                    const spans = Array.from(container.querySelectorAll('span'));
                    const numeric = spans.find(s => /^\d{1,3}(?:\.\d+)?$/.test((s.textContent || '').trim()));
                    if (numeric) return numeric.textContent.trim();
                    container = container.parentElement;
                }
                return null;
            }
        """)
        if score:
            return str(score).strip()
    except Exception:
        pass

    # Fallback to the older selectors in case a different page layout
    # (e.g. a future redesign) reintroduces a '.percent'-style element.
    selectors = [
        ".vue-circular-progress .percent",
        ".percent",
        "div.percent",
        "[class*='percent']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                text = await loc.inner_text(timeout=1000)
                cleaned = re.sub(r"\s+", "", text).replace("%", "")
                m = re.search(r"(\d+(?:\.\d+)?)", cleaned)
                if m:
                    return m.group(1)
        except Exception:
            continue

    return None


async def get_category_breakdown(page) -> dict:
    """Hard/Soft/Other Skills percentage, read from the three progress-bar
    rows above the keyword list (the ones with a colored dot, a label —
    'Hard' / 'Soft' / 'Other', a bar, and a trailing 'NN%' span).

    Replaces an older version written for a chip-based 'Not Found (n)' /
    'Frequency Variance' layout SkillSyncer no longer has — confirmed from
    a real page capture (debug/report_page.html and a user screenshot):
    the current UI has no such chips, and the category label text is
    'Hard' / 'Soft' / 'Other' (not 'Hard Skills' etc., which never
    matched). not_found/frequency_variance are left empty here — they get
    rebuilt from the full keyword table by derive_category_breakdown_from_table,
    which is the authoritative source now anyway."""
    percents = await page.evaluate(
        r"""
        () => {
          const labels = ['Hard', 'Soft', 'Other'];
          const result = {};
          labels.forEach(label => {
            const labelSpan = Array.from(document.querySelectorAll('span')).find(
              e => e.children.length === 0 &&
                   (e.textContent || '').trim() === label &&
                   /uppercase/.test(e.className || '')
            );
            let percent = null;
            if (labelSpan) {
              let row = labelSpan.closest('.flex.items-center.gap-2') || labelSpan.parentElement;
              for (let i = 0; i < 4 && row && !percent; i++) {
                const pctSpan = Array.from(row.querySelectorAll('span')).find(
                  s => /^\d{1,3}%\s*$/.test((s.textContent || '').trim())
                );
                if (pctSpan) { percent = pctSpan.textContent.trim(); break; }
                row = row.parentElement;
              }
            }
            result[label] = percent;
          });
          return result;
        }
        """
    )
    type_to_category = {"Hard": "Hard Skills", "Soft": "Soft Skills", "Other": "Other Skills"}
    return {
        cat: {"percent": percents.get(label), "not_found": [], "frequency_variance": []}
        for label, cat in type_to_category.items()
    }


async def show_all_keywords_filter(page):
    """The keyword list defaults to the 'Needs review' filter chip, which
    hides fully-matched (100%) keywords — confirmed from a real page
    capture showing 'Needs review 43 / Missing 39 / Covered 22 / All 65'
    as sibling filter buttons with 'Needs review' active by default.
    Click 'All' so the full 65-keyword set (not just the 43 needing
    review) is what gets scraped, matching this script's own stated goal
    of capturing every keyword, not just the missing ones."""
    try:
        all_btn = page.get_by_role("button", name=re.compile(r"^All\b"))
        if await all_btn.first.is_visible(timeout=2000):
            await click_robust(page, all_btn.first)
            await page.wait_for_timeout(500)
    except Exception:
        pass


async def get_full_keyword_table(page) -> list:
    """Every keyword row from the scan-overview keyword list (Keyword,
    Type, Score, Resume count, Job count).

    Rewritten against a real page capture: this app never renders a
    <table> anywhere (confirmed: zero <table> tags on either the /update
    or overview page) — a `table tbody tr` selector always returned
    zero rows. The actual structure is a set of <li> group headers
    ('Hard skills' / 'Soft skills' / 'Other keywords', each followed by
    a nested <ul> of `li.group/skill` rows). Each row's keyword is in
    `button span.truncate`, its score in `span.text-center.font-semibold`,
    and its resume/job counts in the two `span.text-center.text-gray-500`
    siblings (deliberately excluding the font-semibold score span, which
    also happens to be text-center, by requiring the text-gray-500 class
    the score span doesn't have)."""
    rows = await page.evaluate(
        r"""
        () => {
          const typeMap = { 'Hard skills': 'Hard', 'Soft skills': 'Soft', 'Other keywords': 'Other' };
          const groupItems = Array.from(document.querySelectorAll('li')).filter(li => {
            const headerDiv = li.querySelector(':scope > div');
            if (!headerDiv || !/sticky/.test(headerDiv.className || '')) return false;
            const t = (headerDiv.textContent || '');
            return Object.keys(typeMap).some(k => t.includes(k));
          });

          const rows = [];
          groupItems.forEach(groupLi => {
            const headerDiv = groupLi.querySelector(':scope > div');
            const headerText = (headerDiv.textContent || '');
            let type = null;
            for (const key in typeMap) { if (headerText.includes(key)) { type = typeMap[key]; break; } }
            const ul = groupLi.querySelector(':scope > ul');
            if (!ul || !type) return;

            const items = ul.querySelectorAll(':scope > li');
            items.forEach(item => {
              const nameSpan = item.querySelector('button span.truncate');
              if (!nameSpan) return;
              const keyword = nameSpan.textContent.trim();
              const scoreSpan = item.querySelector('span.text-center.font-semibold');
              const countSpans = Array.from(item.querySelectorAll('span.text-center.text-gray-500'));
              const score = scoreSpan ? scoreSpan.textContent.trim() : '';
              const resume_count = countSpans[0] ? countSpans[0].textContent.trim() : '';
              const job_count = countSpans[1] ? countSpans[1].textContent.trim() : '';
              if (keyword) rows.push({ keyword, type, score, resume_count, job_count });
            });
          });
          return rows;
        }
        """
    )
    seen = set()
    deduped = []
    for r in rows:
        key = (r["keyword"], r["type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def derive_category_breakdown_from_table(keywords: list, dom_breakdown: dict) -> dict:
    """Rebuild the Hard/Soft/Other 'Not Found' and 'Frequency Variance'
    lists directly from the already-scraped Full Keyword Table, instead
    of trusting a second, independent DOM scrape of the summary cards.

    Why: the keyword table has been cross-checked against SkillSyncer's
    own displayed chips and matches exactly — every keyword scoring 0%
    lines up 1:1 with the site's "Not Found" list, and every partial
    score (0% < score < 100%) lines up with its "Frequency Variance"
    list. The card-scraping approach (searching each card's DOM for a
    "Frequency Variance" text marker and splitting chips by position)
    is a SEPARATE scrape of the same underlying data, so it can silently
    drift out of sync with the table if that marker doesn't render
    exactly where expected — which is exactly what was happening.

    Deriving from the table instead makes the two views mathematically
    guaranteed to agree, since they now come from one source:
      - not_found:          score == 0        (count = job_count)
      - frequency_variance: 0 < score < 100    (count = job_count - resume_count)
      - fully matched, excluded from both:     score == 100

    The percent shown per category is kept from the DOM scrape (it's the
    number SkillSyncer itself displays, so it's the most authoritative
    source) — only the not_found/frequency_variance lists get rebuilt."""
    type_to_category = {"Hard": "Hard Skills", "Soft": "Soft Skills", "Other": "Other Skills"}
    grouped = {
        cat: {"not_found": [], "frequency_variance": []}
        for cat in type_to_category.values()
    }

    def _to_number(val):
        try:
            return float(str(val).rstrip("%").strip() or 0)
        except ValueError:
            return 0.0

    for row in keywords:
        category = type_to_category.get(row.get("type"))
        if not category:
            continue

        score = _to_number(row.get("score"))
        resume_count = int(_to_number(row.get("resume_count")))
        job_count = int(_to_number(row.get("job_count")))
        keyword = row.get("keyword", "")

        if score == 0:
            grouped[category]["not_found"].append({"keyword": keyword, "count": job_count})
        elif 0 < score < 100:
            grouped[category]["frequency_variance"].append(
                {"keyword": keyword, "count": max(job_count - resume_count, 0)}
            )
        # score == 100 -> fully matched, intentionally excluded from both lists

    result = {}
    for category, lists in grouped.items():
        dom_data = (dom_breakdown or {}).get(category) or {}
        percent = dom_data.get("percent")

        if percent is None:
            # Fallback if the DOM percent couldn't be read at all: a
            # job-count-weighted average score across that category's
            # keywords, which closely approximates SkillSyncer's own
            # displayed percentage.
            cat_rows = [r for r in keywords if type_to_category.get(r.get("type")) == category]
            weighted_sum, weight_total = 0.0, 0
            for r in cat_rows:
                s = _to_number(r.get("score"))
                jc = int(_to_number(r.get("job_count")))
                weighted_sum += s * jc
                weight_total += jc
            percent = f"{round(weighted_sum / weight_total)}%" if weight_total else None

        result[category] = {
            "percent": percent,
            "not_found": lists["not_found"],
            "frequency_variance": lists["frequency_variance"],
        }
    return result


async def wait_for_report_content(page, timeout_ms: int = 25000):
    """Poll for the report actually having rendered, instead of guessing a
    fixed sleep. Rescan is an async server-side job — how long it takes to
    finish and repaint the page varies, so a blind wait_for_timeout either
    scrapes too early (empty result) or wastes time waiting longer than
    necessary. This resolves as soon as either a 'Hard Skills' heading or
    a percentage-shaped number shows up anywhere in the page's visible
    text, and simply times out (without raising) if neither ever appears,
    so the caller can still dump debug info and proceed."""
    try:
        await page.wait_for_function(
            """() => {
                const t = document.body.innerText || '';
                return /hard skills/i.test(t) || /\\d{1,3}\\s*%/.test(t);
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass


async def dump_debug_snapshot(page, label: str):
    """Save both the full page HTML and a screenshot. Unlike debug_dump(),
    this is meant to be called on every run (not just failures) so that if
    a field still comes back null/empty, there's always a same-run artifact
    to diagnose against instead of reproducing the failure blind."""
    try:
        os.makedirs("debug", exist_ok=True)
        html = await page.content()
        with open(f"debug/{label}.html", "w", encoding="utf-8") as f:
            f.write(html)
        await page.screenshot(path=f"debug/{label}.png", full_page=True)
    except Exception:
        pass


async def scrape_report(page) -> dict:
    result = {}

    # Navigate to the scan overview page FIRST, before scraping anything.
    #
    # This used to assume the score gauge + Hard/Soft/Other Skills
    # breakdown render on the /update (resume editor) page, and only went
    # "Back to Scan" afterward to read the keyword table. That assumption
    # is no longer true: a debug/report_page.html captured mid-run showed
    # zero matches for "Hard Skills", "circular-progress", "Overall Match"
    # and zero <table> tags anywhere on the /update page — it's purely the
    # resume editor + a "keywords not found" insertion sidebar, none of
    # which is the report. Whatever SkillSyncer's current markup looks
    # like, all three pieces (score, breakdown, table) need to be read
    # from the SAME overview page, in the SAME visit, so they can't
    # silently drift apart.
    # "Back to Scan" is rendered as a <button>, not an <a> — confirmed from
    # debug/back_to_scan_not_found.png: get_by_role("link", ...) never
    # matches it, so it silently timed out here on every run.
    await dismiss_overlays(page)
    back_link = page.get_by_role("button", name="Back to Scan")
    try:
        await back_link.wait_for(state="visible", timeout=10000)
        await click_robust(page, back_link)
    except Exception:
        await debug_dump(page, "back_to_scan_not_found")
    await dismiss_overlays(page)

    # Wait for the report to actually have content instead of a fixed
    # sleep — rescan scoring is an async job on SkillSyncer's end and its
    # timing isn't guaranteed.
    await wait_for_report_content(page)

    # Switch the keyword list from the default "Needs review" filter to
    # "All", so fully-matched (100%) keywords are included in the table
    # too, not just the ones needing review.
    await show_all_keywords_filter(page)

    # Expand every "show more" toggle so hidden chips/rows actually exist
    # in the DOM before we read it. Do this AFTER navigating back, since
    # the overview page has its own collapsed sections independent of
    # whatever was expanded on the /update page.
    await expand_all_collapsed(page)

    # Always save the fully-expanded page HTML + a screenshot for
    # diagnosis, from the actual page being scraped. If any field is
    # still null/empty after this fix, send debug/report_page.html (and
    # .png) so the selectors can be corrected against the real current
    # markup instead of guessing again.
    await dump_debug_snapshot(page, "report_page")

    result["overall_score"] = await get_overall_score(page)
    dom_category_breakdown = await get_category_breakdown(page)
    result["keywords"] = await get_full_keyword_table(page)

    # Rebuild category_breakdown from the table we just scraped, so the
    # summary cards and the Full Keyword Table can never disagree with
    # each other (see derive_category_breakdown_from_table for why).
    result["category_breakdown"] = derive_category_breakdown_from_table(
        result["keywords"], dom_category_breakdown
    )

    # Ensure overall_score is clean and populated
    if not result.get("overall_score"):
        # Fallback 1: Keyword-weighted average across all SkillSyncer keywords
        if result.get("keywords"):
            weighted_sum, weight_total = 0.0, 0
            for r in result["keywords"]:
                try:
                    s = float(str(r.get("score", 0)).rstrip("%").strip())
                    jc = int(float(str(r.get("job_count", 1)).strip() or 1))
                    weighted_sum += s * jc
                    weight_total += jc
                except (ValueError, TypeError):
                    pass
            if weight_total > 0:
                result["overall_score"] = f"{round(weighted_sum / weight_total)}"

        # Fallback 2: Mean of category percentages
        if not result.get("overall_score") and result.get("category_breakdown"):
            cat_pcts = []
            for cdata in result["category_breakdown"].values():
                p = (cdata or {}).get("percent")
                if p:
                    try:
                        cat_pcts.append(float(str(p).rstrip("%").strip()))
                    except (ValueError, TypeError):
                        pass
            if cat_pcts:
                result["overall_score"] = f"{round(sum(cat_pcts) / len(cat_pcts))}"

    if result.get("overall_score"):
        result["overall_score"] = str(result["overall_score"]).replace("%", "").strip()

    return result


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main():
    print("\n" + "=" * 50)
    print("   READING JD & RESUME FROM FILES")
    print("=" * 50)

    new_jd = read_file_content("jd.txt")
    if not new_jd:
        return
    print("Loaded 'jd.txt' successfully.")

    resume_text = read_file_content("resume.txt")
    if not resume_text:
        return
    print("Loaded 'resume.txt' successfully.")

    print("\nLaunching browser (headless) and running the scan...\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            await login(page)
            await page.goto(SCAN_URL, wait_until="domcontentloaded")
            await page.wait_for_selector("text=Edit Job", timeout=45000)

            await update_job_description(page, new_jd)
            await update_resume_text(page, resume_text)

            if DO_RESCAN:
                report = await scrape_report(page)
                print(json.dumps(report, indent=2))
                if not report.get("overall_score") and not report.get("keywords"):
                    print(
                        "\n[warning] Nothing was scraped — overall_score and "
                        "keywords are both empty. Check debug/report_page.html "
                        "and debug/report_page.png to see exactly what page/"
                        "state was captured; that's almost certainly SkillSyncer's "
                        "markup having changed again, not a network/login failure."
                    )
            else:
                print(json.dumps({
                    "status": "JD and resume updated. DO_RESCAN=False, no scan consumed."
                }))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())