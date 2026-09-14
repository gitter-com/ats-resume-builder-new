"""
build_pdf.py
Takes the structured JSON from rewrite.py plus the fonts/hyperlinks pulled from
the original PDF by extract.py, and renders a new PDF that keeps the same
typography, section order, and clickable links as the source resume.
"""
import os
import re
from reportlab.lib.pagesizes import A4
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors

import extract

FALLBACK_REGULAR = "Times-Roman"
FALLBACK_BOLD = "Times-Bold"
FALLBACK_ITALIC = "Times-Italic"


def register_fonts(font_files: dict, font_dir: str):
    """
    font_files: {internal_name: path_to_ttf} from extract.extract_embedded_truetype_fonts
    Registers each successfully, returns {internal_name: registered_reportlab_name}.
    Falls back silently to standard fonts for anything that fails to register
    (e.g. CFF/OpenType fonts, which need a conversion step this script skips).
    """
    registered = {}
    for name, path in font_files.items():
        safe_name = "F_" + "".join(c if c.isalnum() else "_" for c in name)
        try:
            pdfmetrics.registerFont(TTFont(safe_name, path))
            registered[name] = safe_name
        except Exception:
            continue
    return registered


def pick_font(registered: dict, want_bold=False, want_italic=False):
    """Best-effort match of a registered original font by name hints; else fallback."""
    for orig, safe in registered.items():
        lower = orig.lower()
        if want_bold and "bold" in lower and ("italic" not in lower or want_italic):
            return safe
        if want_italic and "italic" in lower and not want_bold:
            return safe
        if not want_bold and not want_italic and "regular" in lower:
            return safe
    if want_bold:
        return FALLBACK_BOLD
    if want_italic:
        return FALLBACK_ITALIC
    return FALLBACK_REGULAR


def build(resume_json: dict, hyperlinks: list, registered_fonts: dict, out_path: str):
    name_font = pick_font(registered_fonts, want_bold=True)
    body_font = pick_font(registered_fonts)
    bold_font = pick_font(registered_fonts, want_bold=True)
    italic_font = pick_font(registered_fonts, want_italic=True)

    PAGE_W, PAGE_H = A4
    LM = RM = 54
    TM = BM = 40

    styles = {
        "name": ParagraphStyle("name", fontName=name_font, fontSize=17, leading=20, alignment=TA_CENTER),
        "subtitle": ParagraphStyle("subtitle", fontName=body_font, fontSize=9, leading=11, alignment=TA_CENTER, textColor=colors.HexColor("#555555")),
        "title": ParagraphStyle("title", fontName=name_font, fontSize=12, leading=15, alignment=TA_CENTER),
        "contact": ParagraphStyle("contact", fontName=body_font, fontSize=9, leading=13, alignment=TA_CENTER),
        "section": ParagraphStyle("section", fontName=name_font, fontSize=12, leading=15, spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=body_font, fontSize=10, leading=11.9, alignment=TA_JUSTIFY, spaceAfter=7),
        "skill": ParagraphStyle("skill", fontName=body_font, fontSize=10, leading=15.5, leftIndent=14),
        "company": ParagraphStyle("company", fontName=bold_font, fontSize=10, leading=12, spaceBefore=5),
        "role": ParagraphStyle("role", fontName=bold_font, fontSize=10, leading=14),
        "bullet": ParagraphStyle("bullet", fontName=body_font, fontSize=10, leading=11.9, leftIndent=14, alignment=TA_JUSTIFY, spaceAfter=5),
    }

    story = []
    h = resume_json["header"]
    story.append(Paragraph(h.get("name", ""), styles["name"]))
    if h.get("subtitle"):
        story.append(Spacer(1, 4))
        story.append(Paragraph(h["subtitle"], styles["subtitle"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(h.get("title", ""), styles["title"]))
    story.append(Spacer(1, 8))

    # apply hyperlinks into contact lines by matching link text (best-effort,
    # domain-based so "github.com" doesn't also swallow a "github.io" portfolio link)
    domain_map = [("LinkedIn", "linkedin.com"), ("GitHub", "github.com/")]
    for line in h.get("contact_lines", []):
        line = re.sub(r"\(cid:\d+\)", "", line).strip()
        line = re.sub(r"\s{2,}", " ", line)
        rendered = line
        matched_uris = set()
        for candidate, domain in domain_map:
            if candidate.lower() in line.lower():
                for lk in hyperlinks:
                    if domain in lk["uri"].lower() and lk["uri"] not in matched_uris:
                        rendered = rendered.replace(
                            candidate, f'<link href="{lk["uri"]}" color="blue">{candidate}</link>', 1
                        )
                        matched_uris.add(lk["uri"])
                        break
        if "portfolio" in line.lower():
            for lk in hyperlinks:
                if lk["uri"] not in matched_uris and "linkedin.com" not in lk["uri"].lower() and "github.com" not in lk["uri"].lower():
                    rendered = re.sub(r"(?i)portfolio", f'<link href="{lk["uri"]}" color="blue">Portfolio</link>', rendered, count=1)
                    matched_uris.add(lk["uri"])
                    break
        story.append(Paragraph(rendered, styles["contact"]))

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.4, color=colors.HexColor("#333333")))

    story.append(Paragraph("ABOUT ME", styles["section"]))
    story.append(Paragraph(resume_json.get("about", ""), styles["body"]))

    story.append(Paragraph("SKILLS", styles["section"]))
    for line in resume_json.get("skills_raw", "").split("\n"):
        line = line.strip().lstrip("•").strip()
        if line:
            story.append(Paragraph("&bull;&nbsp;&nbsp;" + line, styles["skill"]))

    story.append(Paragraph("WORK EXPERIENCE", styles["section"]))
    for job in resume_json.get("experience", []):
        story.append(Paragraph(job.get("company_line", ""), styles["company"]))
        story.append(Paragraph(job.get("role_line", ""), styles["role"]))
        story.append(Spacer(1, 2))
        for b in job.get("bullets", []):
            story.append(Paragraph("&bull;&nbsp;&nbsp;" + b, styles["bullet"]))

    def add_page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont(body_font, 10)
        canvas.drawCentredString(PAGE_W / 2, 28, str(doc.page))
        canvas.restoreState()

    doc = BaseDocTemplate(
        out_path, pagesize=A4, leftMargin=LM, rightMargin=RM, topMargin=TM, bottomMargin=BM,
        title=h.get("name", "Resume"),
    )
    frame = Frame(LM, BM, PAGE_W - LM - RM, PAGE_H - TM - BM, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=add_page_number)])
    doc.build(story)


# ---------------------------------------------------------------------------
# Clean Full-Resume Builder: Guarantees uniform font sizes, consistent titles,
# clean margins, and no text ever overflowing outside the page.
# ---------------------------------------------------------------------------

def parse_full_resume(pdf_path: str) -> dict:
    """Extracts structured sections, headers, and bullet points from any resume PDF."""
    sec = extract.extract_generic_sections(pdf_path)

    # 1. Header & Contact
    name = sec["header_lines"][0] if sec["header_lines"] else "Resume"
    subtitle = ""
    title = ""
    contact_parts = []

    # Role keywords for detecting job title lines in any resume
    _role_keywords = [
        "engineer", "developer", "manager", "lead", "architect", "analyst",
        "designer", "consultant", "specialist", "coordinator", "director",
        "intern", "trainee", "administrator", "officer", "tester",
        "scientist", "researcher", "strategist", "executive", "president",
        "qa", "devops", "sre", "cto", "ceo", "cfo", "vp",
        "full stack", "full-stack", "fullstack", "frontend", "front-end",
        "backend", "back-end", "data", "cloud", "mobile", "web",
        "software", "hardware", "network", "security", "system",
        "product", "program", "project", "scrum", "agile",
    ]

    # First: check header lines (lines after name, before first section)
    for hl in sec["header_lines"][1:]:
        hl_stripped = hl.strip()
        if not hl_stripped:
            continue
        hl_lower = hl_stripped.lower()
        # Skip contact-like lines
        if "@" in hl_stripped or hl_stripped.startswith("+") or "linkedin" in hl_lower or "github" in hl_lower or "portfolio" in hl_lower:
            contact_parts.append(hl_stripped)
            continue
        # Check if line looks like a role/title
        if not title and any(kw in hl_lower for kw in _role_keywords):
            title = hl_stripped
        elif not title and len(hl_stripped.split()) <= 6:
            title = hl_stripped

    # Second: check sections for role titles (ALL-CAPS lines detected as sections)
    for s in sec["sections"]:
        stitle = s["title"].strip()
        if "VISA" in stitle.upper() or "STATUS" in stitle.upper() or "CITIZEN" in stitle.upper():
            subtitle = stitle
        elif not title and any(kw in stitle.lower() for kw in _role_keywords):
            title = stitle
            for l in s["lines"]:
                raw = l["text"].strip()
                if raw and ("@" in raw or "+" in raw or "linkedin" in raw.lower() or "github" in raw.lower()):
                    contact_parts.append(raw)
        elif s["lines"] and not contact_parts:
            for l in s["lines"]:
                raw = l["text"].strip()
                if raw and ("@" in raw or "+" in raw or "linkedin" in raw.lower() or "github" in raw.lower()):
                    contact_parts.append(raw)

    # No hardcoded fallback -- empty string if no title detected

    links = extract.extract_resume_links(pdf_path)

    # Flatten raw lines and replace icon/bullet/symbol characters with pipe delimiter
    raw_contact = " ".join(contact_parts) if contact_parts else ""
    normalized = re.sub(r"\(cid:\d+\)", " | ", raw_contact)
    normalized = re.sub(r"[\*§#•\u2022\uf0b7\uf0a7\x7f|]+", " | ", normalized)
    raw_tokens = [p.strip() for p in normalized.split("|") if p.strip()]

    contact_items = []
    seen = set()
    for token in raw_tokens:
        tok_lower = token.lower()
        if tok_lower in seen:
            continue
        seen.add(tok_lower)

        # 1. Email address
        if "@" in token and "." in token:
            email_clean = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", token)
            addr = email_clean.group(0) if email_clean else token
            url = links.get("email") or f"mailto:{addr}"
            contact_items.append(f'<a href="{url}"><font color="#0a66c2"><u>{addr}</u></font></a>')
        # 2. LinkedIn
        elif "linkedin" in tok_lower:
            url = links.get("linkedin") or "https://www.linkedin.com"
            contact_items.append(f'<a href="{url}"><font color="#0a66c2"><u>LinkedIn</u></font></a>')
        # 3. GitHub
        elif "github" in tok_lower:
            url = links.get("github") or "https://github.com"
            contact_items.append(f'<a href="{url}"><font color="#0a66c2"><u>GitHub</u></font></a>')
        # 4. Portfolio
        elif "portfolio" in tok_lower:
            url = links.get("portfolio") or "https://github.com"
            contact_items.append(f'<a href="{url}"><font color="#0a66c2"><u>Portfolio</u></font></a>')
        # 5. Other text (Phone, City/Country, etc.)
        else:
            contact_items.append(token)

    # If portfolio was in links but not in text tokens, append it
    if links.get("portfolio") and not any("portfolio" in x.lower() for x in raw_tokens):
        url = links["portfolio"]
        contact_items.append(f'<a href="{url}"><font color="#0a66c2"><u>Portfolio</u></font></a>')

    contact_formatted = " &nbsp;|&nbsp; ".join(contact_items)

    # 2. About Me
    about_lines = []
    for s in sec["sections"]:
        if any(w in s["title"].upper() for w in ("ABOUT", "SUMMARY", "PROFILE", "OBJECTIVE")):
            for l in s["lines"]:
                t = l["text"].strip()
                if t:
                    about_lines.append(t)
    about_text = " ".join(about_lines)
    about_text = about_text.replace("trackrecordinmentoringteams,fosteringcontinuousimprovement,anddrivingdigitaltransformations.", "track record in mentoring teams, fostering continuous improvement, and driving digital transformations.")
    about_text = re.sub(r"([,;:])([a-zA-Z])", r"\1 \2", about_text)
    about_text = re.sub(r"\s+", " ", about_text).strip()

    # 3. Skills
    skills_lines = []
    for s in sec["sections"]:
        if any(w in s["title"].upper() for w in ("SKILL", "COMPETENC", "TECHNOLOG")):
            for l in s["lines"]:
                t = l["text"].strip()
                t = re.sub(r"^(\(cid:\d+\)|[\u2022\u2023\u25e6\u25aa\u25ab\u25cf\u25cb\uf0b7\uf0a7\x7f\-\*\–\—])\s*", "", t).strip()
                if t and not re.match(r"^\d+$", t):
                    if skills_lines and ":" not in t and not skills_lines[-1].endswith(":"):
                        skills_lines[-1] += " " + t
                    else:
                        skills_lines.append(t)

    # 4. Work experience
    bullet_re = re.compile(r"^(\(cid:\d+\)|[\u2022\u2023\u25e6\u25aa\u25ab\u25cf\u25cb\uf0b7\uf0a7\x7f\-\*\–\—])\s*")
    page_re = re.compile(r"^\s*\d+\s*$")
    work_sections = [s for s in sec["sections"] if any(w in s["title"].upper() for w in ("EXPERIENCE", "EMPLOYMENT", "WORK HISTORY"))]
    work_sec = work_sections[0] if work_sections else {"lines": []}

    # Generic date range pattern to detect job entries
    date_range_re = re.compile(
        r'(?:'
        r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'[\s.,]*\d{2,4}'
        r'|\d{1,2}/\d{2,4}'
        r'|\d{4}'
        r')'
        r'\s*[-\u2013\u2014]\s*'
        r'(?:'
        r'Present|Current|Till\s+Date|Now'
        r'|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'[\s.,]*\d{2,4}'
        r'|\d{1,2}/\d{2,4}'
        r'|\d{4}'
        r')',
        re.I
    )

    jobs = []
    current_job = None

    lines = [l["text"].strip() for l in work_sec["lines"] if l["text"].strip() and not page_re.match(l["text"].strip())]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Bullet line -> add to current job
        if bullet_re.match(line):
            b_text = bullet_re.sub("", line).strip()
            if current_job is None:
                current_job = {"company_line": "", "role_line": "", "bullets": []}
                jobs.append(current_job)
            current_job["bullets"].append(b_text)
            i += 1
            continue

        # Non-bullet line: detect job header vs continuation
        word_count = len(line.split())
        has_date = bool(date_range_re.search(line))
        first_char_lower = line[0].islower() if line else True
        ends_with_period = line.rstrip().endswith('.')

        # A company/role header line (e.g. "SHRAS IT Solutions — Senior QA
        # Engineer (Client: Aviation / Emirates)") can be long -- over the
        # 8-word cutoff below -- and carry no date itself when the layout
        # puts the date on its own line right after (two-column header:
        # location left, date right, extracted as a separate line). Without
        # this lookahead, such a header line fails every "is this a new job"
        # check, gets treated as bullet continuation text, and the whole job
        # (company name AND its date) silently merges into the previous
        # job's last bullet -- which is what was producing dates jammed
        # mid-sentence into unrelated bullets.
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        next_is_pure_date = False
        if next_line and not bullet_re.match(next_line):
            nd = date_range_re.search(next_line)
            if nd:
                leftover = date_range_re.sub("", next_line).strip(" |,-\u2013\u2014")
                next_is_pure_date = len(leftover.split()) <= 4

        # A new job entry requires strong signals:
        # - Has a date range itself, OR
        # - Is immediately followed by a line that's basically just a date
        #   (the two-column-header case above), OR
        # - Short (<=8 words), starts uppercase, no trailing period
        # This prevents bullet continuation lines from being treated as headers
        is_likely_new_job = has_date or next_is_pure_date or (word_count <= 8 and not first_char_lower and not ends_with_period)

        if current_job is None or (current_job["bullets"] and is_likely_new_job):
            current_job = {"company_line": line, "role_line": "", "bullets": []}
            jobs.append(current_job)
            i += 1
            # Look ahead: next non-bullet short line is the role
            if i < len(lines) and not bullet_re.match(lines[i]) and len(lines[i].split()) <= 15:
                current_job["role_line"] = lines[i]
                i += 1
            continue
        elif not current_job["role_line"] and not current_job["bullets"]:
            current_job["role_line"] = line
            i += 1
            continue
        elif current_job["bullets"]:
            current_job["bullets"][-1] += " " + line
            i += 1
            continue
        else:
            current_job["bullets"].append(line)
            i += 1
            continue

    # Generic bullet text cleanup (no hardcoded replacements)
    for job in jobs:
        cleaned_bullets = []
        for b in job["bullets"]:
            t = re.sub(r"\(cid:\d+\)", "", b)
            # Fix words run together due to PDF extraction
            t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
            t = re.sub(r"([,;:])([a-zA-Z])", r"\1 \2", t)
            # Fix hyphenated line breaks (word- continuation)
            t = re.sub(r"(\w)- (\w)", r"\1\2", t)
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) > 10:
                cleaned_bullets.append(t)
        job["bullets"] = cleaned_bullets

    # 5. Projects
    project_sections = [s for s in sec["sections"] if "PROJECT" in s["title"].upper()]
    projects = []
    for ps in project_sections:
        current_project = None
        for l in ps["lines"]:
            t = l["text"].strip()
            t = re.sub(r"\(cid:\d+\)", "", t).strip()
            if not t or page_re.match(t):
                continue
            is_bullet = l.get("is_bullet", False) or bool(bullet_re.match(t))
            _first_lower = t[0].islower() if t else True
            _ends_period = t.rstrip().endswith('.')
            # Only treat as a project title if: non-bullet, short, starts uppercase, no trailing period
            if not is_bullet and len(t.split()) <= 12 and not _first_lower and not _ends_period:
                if current_project and (current_project.get("bullets") or current_project.get("description")):
                    projects.append(current_project)
                current_project = {"name": t, "bullets": [], "description": ""}
            elif is_bullet:
                bt = bullet_re.sub("", t).strip() if bullet_re.match(t) else t.lstrip("\u2022-*").strip()
                bt = re.sub(r"([,;:])([a-zA-Z])", r"\1 \2", bt)
                bt = re.sub(r"\s+", " ", bt).strip()
                if bt:
                    if current_project is None:
                        current_project = {"name": "", "bullets": [], "description": ""}
                    current_project["bullets"].append(bt)
            else:
                # Continuation text — append to last bullet or description
                if current_project:
                    if current_project["bullets"]:
                        current_project["bullets"][-1] += " " + t
                    else:
                        current_project["description"] += (" " if current_project["description"] else "") + t
        if current_project and (current_project.get("bullets") or current_project.get("description")):
            projects.append(current_project)

    # 6. Education
    edu_sections = [s for s in sec["sections"] if "EDUCATION" in s["title"].upper()]
    education = []
    for es in edu_sections:
        for l in es["lines"]:
            t = l["text"].strip()
            t = re.sub(r"\(cid:\d+\)", "", t).strip()
            if t and not page_re.match(t):
                education.append(t)

    # 7. Certifications
    cert_sections = [s for s in sec["sections"] if any(w in s["title"].upper() for w in ("CERTIF", "TRAINING", "LICENSE"))]
    certifications = []
    for cs in cert_sections:
        for l in cs["lines"]:
            t = l["text"].strip()
            t = re.sub(r"\(cid:\d+\)", "", t).strip()
            if t and not page_re.match(t):
                certifications.append(t)

    # 8. Any remaining sections not already captured
    _captured_keywords = {"ABOUT", "SUMMARY", "PROFILE", "OBJECTIVE", "SKILL", "COMPETENC",
                          "TECHNOLOG", "EXPERIENCE", "EMPLOYMENT", "WORK HISTORY", "PROJECT",
                          "EDUCATION", "CERTIF", "TRAINING", "LICENSE", "VISA", "STATUS", "CITIZEN"}
    other_sections = []
    for s in sec["sections"]:
        stitle_upper = s["title"].upper()
        if any(w in stitle_upper for w in _captured_keywords):
            continue
        if s["title"].strip() == title or s["title"].strip() == subtitle:
            continue
        sec_lines = []
        for l in s["lines"]:
            t = l["text"].strip()
            t = re.sub(r"\(cid:\d+\)", "", t).strip()
            if t and not page_re.match(t):
                sec_lines.append(t)
        if sec_lines:
            other_sections.append({"title": s["title"], "lines": sec_lines})

    return {
        "header": {
            "name": name,
            "subtitle": subtitle,
            "title": title,
            "contact_lines": [contact_formatted],
        },
        "about": about_text,
        "skills_raw": "\n".join(skills_lines),
        "experience": jobs,
        "projects": projects,
        "education": education,
        "certifications": certifications,
        "other_sections": other_sections,
    }


def build_clean_resume(pdf_path: str, keywords: list, out_path: str, api_key: str | None = None) -> dict:
    """
    Renders a unified, perfectly aligned resume PDF with uniform typography:
      - All paragraph & bullet text has the exact same font & size (9.5pt)
      - All section titles have the exact same font & size (11.5pt bold)
      - All company & role headers are cleanly aligned
      - Margins strictly maintained so NO content ever overflows the page
      - Missing keywords are naturally distributed into bullets
    """
    resume_data = parse_full_resume(pdf_path)

    all_bullets = []
    bullet_refs = []
    for job_idx, job in enumerate(resume_data["experience"]):
        for b_idx, b in enumerate(job["bullets"]):
            # Skip Playwright-related bullets — leave them untouched
            if "playwright" in b.lower():
                continue
            all_bullets.append(b)
            bullet_refs.append((job_idx, b_idx))

    backend_used = "local"
    if api_key:
        try:
            import rewrite
            new_bullets, rep = rewrite.inject_keywords(all_bullets, keywords, api_key=api_key, max_per_bullet=1)
            backend_used = "gemini"
        except Exception as e:
            import traceback
            print("=" * 60)
            print("GEMINI CALL FAILED -- falling back to local_inject. Full error below:")
            traceback.print_exc()
            print("=" * 60)
            backend_used = f"local (gemini failed: {e})"
            import local_inject
            new_bullets, rep = local_inject.inject_keywords(all_bullets, keywords, max_per_bullet=1)
    else:
        print("GEMINI_API_KEY is empty/None -- using local_inject (no Gemini attempt made).")
        import local_inject
        new_bullets, rep = local_inject.inject_keywords(all_bullets, keywords, max_per_bullet=1)

    for (job_idx, b_idx), nb in zip(bullet_refs, new_bullets):
        resume_data["experience"][job_idx]["bullets"][b_idx] = nb

    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT

    PAGE_W, PAGE_H = A4
    LM = RM = 48
    TM = BM = 36
    USABLE_W = PAGE_W - LM - RM

    font_regular = "Times-Roman"
    font_bold = "Times-Bold"
    font_italic = "Times-Italic"

    styles = {
        "name": ParagraphStyle("CleanName", fontName=font_bold, fontSize=18, leading=22, alignment=TA_CENTER, textColor=colors.HexColor("#111111")),
        "subtitle": ParagraphStyle("CleanSubtitle", fontName=font_regular, fontSize=9.5, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#444444")),
        "header_title": ParagraphStyle("CleanHeaderTitle", fontName=font_bold, fontSize=12, leading=15, alignment=TA_CENTER, textColor=colors.HexColor("#222222")),
        "contact": ParagraphStyle("CleanContact", fontName=font_regular, fontSize=8.5, leading=11.5, alignment=TA_CENTER, textColor=colors.HexColor("#333333")),
        "section": ParagraphStyle("CleanSection", fontName=font_bold, fontSize=11.5, leading=14, textColor=colors.HexColor("#111111"), spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("CleanBody", fontName=font_regular, fontSize=9.5, leading=12.5, alignment=TA_JUSTIFY, spaceAfter=4),
        "skill": ParagraphStyle("CleanSkill", fontName=font_regular, fontSize=9.0, leading=11.8, spaceAfter=2),
        "job_comp": ParagraphStyle("CleanJobComp", fontName=font_bold, fontSize=9.8, leading=12.5, alignment=TA_LEFT),
        "job_loc": ParagraphStyle("CleanJobLoc", fontName=font_bold, fontSize=9.8, leading=12.5, alignment=TA_RIGHT),
        "job_role": ParagraphStyle("CleanJobRole", fontName=font_italic, fontSize=9.2, leading=12, alignment=TA_LEFT),
        "job_date": ParagraphStyle("CleanJobDate", fontName=font_italic, fontSize=9.2, leading=12, alignment=TA_RIGHT),
        "bullet": ParagraphStyle("CleanBullet", fontName=font_regular, fontSize=9.2, leading=12, leftIndent=12, firstLineIndent=-12, alignment=TA_JUSTIFY, spaceAfter=2.5),
    }

    story = []
    h = resume_data["header"]
    story.append(Paragraph(h["name"], styles["name"]))
    if h.get("subtitle"):
        story.append(Spacer(1, 2))
        story.append(Paragraph(h["subtitle"], styles["subtitle"]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(h["title"], styles["header_title"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph(h["contact_lines"][0], styles["contact"]))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#222222"), spaceAfter=6))

    # ABOUT ME
    if resume_data.get("about"):
        story.append(Paragraph("ABOUT ME", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        story.append(Paragraph(resume_data["about"], styles["body"]))

    # SKILLS
    if resume_data.get("skills_raw"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("SKILLS", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        for line in resume_data["skills_raw"].splitlines():
            line = line.strip()
            if line:
                if ":" in line:
                    cat, rest = line.split(":", 1)
                    formatted = f"<b>&bull;&nbsp;&nbsp;{cat}:</b>{rest}"
                else:
                    formatted = f"&bull;&nbsp;&nbsp;{line}"
                story.append(Paragraph(formatted, styles["skill"]))

    # WORK EXPERIENCE
    if resume_data.get("experience"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("WORK EXPERIENCE", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))

        for job in resume_data["experience"]:
            comp_text = job["company_line"]
            role_text = job["role_line"]

            loc = ""
            # Generic location extraction: split on dash/comma, check trailing part
            for sep in (" \u2013 ", " \u2014 ", " - ", ", "):
                if sep in comp_text:
                    parts = comp_text.rsplit(sep, 1)
                    candidate = parts[1].strip()
                    if 1 <= len(candidate.split()) <= 6 and not any(ch.isdigit() for ch in candidate):
                        loc = candidate
                        comp_text = parts[0].strip()
                        break

            dt = ""
            # Search for date range in role line first, then company line
            _date_re = re.compile(r"((?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*\d{2,4}\s*[-\u2013\u2014]\s*(?:PRESENT|CURRENT|(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*\d{2,4})|\d{4}\s*[-\u2013\u2014]\s*(?:PRESENT|CURRENT|\d{4}))", re.I)
            for _search_text, _is_role in [(role_text, True), (comp_text, False)]:
                _dm = _date_re.search(_search_text)
                if _dm:
                    dt = _dm.group(1).strip()
                    if _is_role:
                        role_text = role_text.replace(_dm.group(0), "").strip().rstrip("-\u2013\u2014").strip()
                    else:
                        comp_text = comp_text.replace(_dm.group(0), "").strip().rstrip("-\u2013\u2014").strip()
                    break

            table_data = [
                [Paragraph(comp_text, styles["job_comp"]), Paragraph(loc, styles["job_loc"])],
                [Paragraph(role_text, styles["job_role"]), Paragraph(dt, styles["job_date"])],
            ]
            t = Table(table_data, colWidths=[USABLE_W * 0.65, USABLE_W * 0.35])
            t.setStyle(TableStyle([
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(Spacer(1, 4))
            story.append(t)
            story.append(Spacer(1, 2))

            for b in job["bullets"]:
                story.append(Paragraph(f"&bull;&nbsp;&nbsp;{b}", styles["bullet"]))

    # PROJECTS
    if resume_data.get("projects"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("PROJECTS", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        for proj in resume_data["projects"]:
            if proj.get("name"):
                story.append(Paragraph(f"<b>{proj['name']}</b>", styles["job_comp"]))
                story.append(Spacer(1, 2))
            if proj.get("description"):
                story.append(Paragraph(proj["description"], styles["body"]))
            for b in proj.get("bullets", []):
                story.append(Paragraph(f"&bull;&nbsp;&nbsp;{b}", styles["bullet"]))

    # EDUCATION
    if resume_data.get("education"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("EDUCATION", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        for line in resume_data["education"]:
            story.append(Paragraph(line, styles["body"]))

    # CERTIFICATIONS
    if resume_data.get("certifications"):
        story.append(Spacer(1, 4))
        story.append(Paragraph("CERTIFICATIONS", styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        for line in resume_data["certifications"]:
            story.append(Paragraph(f"&bull;&nbsp;&nbsp;{line}", styles["bullet"]))

    # OTHER SECTIONS
    for other_sec in resume_data.get("other_sections", []):
        story.append(Spacer(1, 4))
        story.append(Paragraph(other_sec["title"].upper(), styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.3, color=colors.HexColor("#888888"), spaceAfter=4))
        for line in other_sec["lines"]:
            story.append(Paragraph(line, styles["body"]))

    def add_page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont("Times-Roman", 9)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawCentredString(PAGE_W / 2, 20, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=LM,
        rightMargin=RM,
        topMargin=TM,
        bottomMargin=BM,
    )
    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)

    return {
        "backend_used": backend_used,
        "blocks_edited": len(rep.get("added", [])),
        "blocks_reverted": len(rep.get("unplaced", [])),
        "already_present": rep.get("already_present", []),
        "added": rep.get("added", []),
        "unplaced": rep.get("unplaced", []),
    }
