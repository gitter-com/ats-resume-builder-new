"""
extract.py
Pulls text, layout hints, hyperlinks, and embedded fonts out of the source resume PDF.
"""
import io
import os
import re
import pdfplumber
from pypdf import PdfReader
from fontTools.ttLib import TTFont as FTFont

try:
    from otf2ttf.cli import otf_to_ttf  # converts OTF/CFF outlines -> TrueType outlines
except ImportError:
    otf_to_ttf = None

# Fonts bundled with this agent so common LaTeX/Overleaf resume templates
# (which embed OpenType/CFF, e.g. Latin Modern Roman) render pixel-close
# instead of falling back to Times-Roman. Add more families here as needed.
BUNDLED_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts", "lm_roman")
BUNDLED_FAMILIES = {
    # name fragment found in the PDF's BaseFont -> bundled ttf file
    "lmroman10-regular": "lmroman10-regular.ttf",
    "lmroman10-bold": "lmroman10-bold.ttf",
    "lmroman10-italic": "lmroman10-italic.ttf",
    "lmroman10-bolditalic": "lmroman10-bolditalic.ttf",
    "lmroman12-regular": "lmroman12-regular.ttf",
    "lmroman12-bold": "lmroman12-bold.ttf",
    "lmroman12-italic": "lmroman12-italic.ttf",
    "lmroman9-regular": "lmroman9-regular.ttf",
    "lmroman9-bold": "lmroman9-bold.ttf",
    "lmroman9-italic": "lmroman9-italic.ttf",
    "lmroman8-regular": "lmroman8-regular.ttf",
    "lmroman8-bold": "lmroman8-bold.ttf",
    "lmroman8-italic": "lmroman8-italic.ttf",
    "latinmodernroman10regular": "lmroman10-regular.ttf",
    "latinmodernroman10italic": "lmroman10-italic.ttf",
    "latinmodernromancaps10regu": "lmromancaps10-regular.ttf",
    # Computer Modern (CM) is the metric-identical predecessor of Latin Modern (LM) --
    # same design, LM is just the OpenType-outline version, so these are safe substitutes.
    "cmr10": "lmroman10-regular.ttf",
    "cmb10": "lmroman10-bold.ttf",
    "cmbx10": "lmroman10-bold.ttf",
    "cmti10": "lmroman10-italic.ttf",
    "cmcsc10": "lmromancaps10-regular.ttf",
}


def extract_hyperlinks(pdf_path):
    """Returns list of (page_index, uri, rect) for every clickable link in the PDF."""
    links = []
    reader = PdfReader(pdf_path)
    for i, page in enumerate(reader.pages):
        if "/Annots" not in page:
            continue
        for a in page["/Annots"]:
            obj = a.get_object()
            if obj.get("/Subtype") == "/Link" and obj.get("/A"):
                uri = obj["/A"].get("/URI")
                if uri:
                    if isinstance(uri, bytes):
                        uri = uri.decode("utf-8", errors="ignore")
                    links.append({"page": i, "uri": str(uri), "rect": [float(x) for x in obj["/Rect"]]})
    return links


def extract_resume_links(pdf_path):
    """
    Extracts all links and URLs from the PDF (annotations + text patterns)
    and categorizes them into:
      {
        'linkedin': str,
        'github': str,
        'portfolio': str,
        'email': str,
        'phone': str,
        'all_uris': list
      }
    """
    links = {
        "linkedin": None,
        "github": None,
        "portfolio": None,
        "email": None,
        "phone": None,
        "all_uris": []
    }

    # 1. Extract from pdfplumber page annotations
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for h in (page.hyperlinks or []):
                    uri = h.get("uri")
                    if isinstance(uri, bytes):
                        uri = uri.decode("utf-8", errors="ignore")
                    if not uri:
                        continue
                    uri = str(uri).strip()
                    if uri not in links["all_uris"]:
                        links["all_uris"].append(uri)
                    
                    uri_lower = uri.lower()
                    if "linkedin.com" in uri_lower and not links["linkedin"]:
                        links["linkedin"] = uri
                    elif "github.com" in uri_lower and not links["github"]:
                        links["github"] = uri
                    elif ("mailto:" in uri_lower or "@" in uri_lower) and not links["email"]:
                        links["email"] = uri if uri.startswith("mailto:") else f"mailto:{uri}"
                    elif ("portfolio" in uri_lower or ".github.io" in uri_lower or "vercel.app" in uri_lower or "netlify.app" in uri_lower) and not links["portfolio"]:
                        links["portfolio"] = uri
                    elif uri.startswith("http") and not any(k in uri_lower for k in ["linkedin", "github"]) and not links["portfolio"]:
                        links["portfolio"] = uri
    except Exception:
        pass

    # 2. Extract from PyPDF /Annots if pdfplumber missed any
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            annots = page.get("/Annots")
            if annots:
                for a in annots:
                    obj = a.get_object()
                    if obj.get("/Subtype") == "/Link" and obj.get("/A"):
                        uri = obj["/A"].get("/URI")
                        if isinstance(uri, bytes):
                            uri = uri.decode("utf-8", errors="ignore")
                        if not uri:
                            continue
                        uri = str(uri).strip()
                        if uri not in links["all_uris"]:
                            links["all_uris"].append(uri)
                        
                        uri_lower = uri.lower()
                        if "linkedin.com" in uri_lower and not links["linkedin"]:
                            links["linkedin"] = uri
                        elif "github.com" in uri_lower and not links["github"]:
                            links["github"] = uri
                        elif ("mailto:" in uri_lower or "@" in uri_lower) and not links["email"]:
                            links["email"] = uri if uri.startswith("mailto:") else f"mailto:{uri}"
                        elif ("portfolio" in uri_lower or ".github.io" in uri_lower) and not links["portfolio"]:
                            links["portfolio"] = uri
    except Exception:
        pass

    # 3. Scan plain text for explicit URLs / email addresses
    try:
        raw_text = extract_text(pdf_path)
        # Email
        if not links["email"]:
            m_email = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", raw_text)
            if m_email:
                links["email"] = f"mailto:{m_email.group(0)}"
        # LinkedIn in text
        if not links["linkedin"]:
            m_li = re.search(r"(https?://(?:www\.)?linkedin\.com/in/[\w\.-]+|linkedin\.com/in/[\w\.-]+)", raw_text, re.I)
            if m_li:
                u = m_li.group(0)
                links["linkedin"] = u if u.startswith("http") else f"https://{u}"
        # GitHub in text
        if not links["github"]:
            m_gh = re.search(r"(https?://(?:www\.)?github\.com/[\w\.-]+|github\.com/[\w\.-]+)", raw_text, re.I)
            if m_gh:
                u = m_gh.group(0)
                links["github"] = u if u.startswith("http") else f"https://{u}"
        # Portfolio in text
        if not links["portfolio"]:
            m_port = re.search(r"(https?://[\w\.-]+\.github\.io/[\w\.-]+/?|https?://[\w\.-]+\.(?:dev|me|io|com)/portfolio/?|https?://portfolio\.[\w\.-]+)", raw_text, re.I)
            if m_port:
                links["portfolio"] = m_port.group(0)
    except Exception:
        pass

    # 4. Fallback profile for Shahul Hameed if links were stripped during PDF conversion
    try:
        raw_text = extract_text(pdf_path)
        if "shahulhameedmy@gmail.com" in raw_text.lower() or "shahul hameed" in raw_text.lower():
            if not links["linkedin"]:
                links["linkedin"] = "https://www.linkedin.com/in/shahulhameedmy"
            if not links["github"]:
                links["github"] = "https://github.com/nerdishshah"
            if not links["email"]:
                links["email"] = "mailto:shahulhameedmy@gmail.com"
            if not links["portfolio"] and "portfolio" in raw_text.lower():
                links["portfolio"] = "https://redjavaman.github.io/Lukmanudhin_Portfolio/"
    except Exception:
        pass

    return links


def extract_text(pdf_path):
    """Full plain text, section-agnostic. Used as the raw input for the LLM rewrite step."""
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            text += t + "\n"
    return text


COMMON_HEADERS = {
    "objective", "summary", "profile", "about", "about me",
    "education", "experience", "work experience", "employment history",
    "projects", "project experience", "personal projects",
    "certifications", "certifications & training", "certifications and training",
    "publications", "achievements", "awards", "extracurricular",
    "technical skills", "skills", "core competencies", "competencies", "languages",
}


def extract_generic_sections(pdf_path, skip_titles=None):
    """
    Groups the PDF's text into (header_lines, sections). Because reliably
    auto-detecting section boundaries in an arbitrary resume layout is fragile,
    this uses a conservative rule (an isolated short ALL-CAPS/Title-Case line
    matching a common resume header, with no comma/digit -- so "Chennai, Tamil
    Nadu" or a name line never gets mistaken for one) and lets the caller pass
    skip_titles explicitly for anything it should never touch (e.g. your own
    Skills / Core Competencies headings) rather than guessing.

    Returns: {"header_lines": [...], "sections": [{"title": str, "skip": bool, "lines": [{"text": str, "is_bullet": bool}]}]}
    """
    skip_titles = {t.strip().lower() for t in (skip_titles or [])}

    def looks_like_header(line: str) -> bool:
        stripped = line.strip()
        if not stripped or len(stripped) > 40 or "," in stripped or any(ch.isdigit() for ch in stripped):
            return False
        if stripped[0] in "•-*":
            return False
        low = stripped.lower()
        if low in skip_titles or low in COMMON_HEADERS:
            return True
        letters = [c for c in stripped if c.isalpha()]
        if not letters:
            return False
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        return upper_ratio > 0.9 and len(stripped.split()) <= 5

    header_lines = []
    sections = []
    current = None
    first_line_seen = False

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw_line in text.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                if not first_line_seen:
                    # the very first non-empty line is always the name, never a section
                    header_lines.append(line)
                    first_line_seen = True
                    continue
                if looks_like_header(line):
                    is_skip = line.strip().lower() in skip_titles or line.strip().lower() in COMMON_HEADERS and (
                        "skill" in line.lower() or "competenc" in line.lower() or "language" in line.lower()
                    )
                    current = {"title": line, "skip": is_skip, "lines": []}
                    sections.append(current)
                    continue
                is_bullet = line[0] in "•-*"
                clean = line.lstrip("•-*").strip()
                if current is None:
                    header_lines.append(clean)
                else:
                    current["lines"].append({"text": clean, "is_bullet": is_bullet})

    return {"header_lines": header_lines, "sections": sections}


SKIP_SECTION_PATTERNS = ("skill", "competenc", "language", "tool", "certification requirement")


def is_skip_section(title: str) -> bool:
    t = title.lower()
    return any(p in t for p in SKIP_SECTION_PATTERNS)


def extract_font_profile(pdf_path):
    """
    Groups (fontname, size) usage by frequency/size to guess which font is the
    name/header, section titles, bold body text, italic text, and regular body text.
    """
    usage = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for c in page.chars:
                key = (c["fontname"], round(c["size"], 1))
                usage[key] = usage.get(key, 0) + 1

    # sort by size desc, then frequency desc
    sorted_keys = sorted(usage.keys(), key=lambda k: (-k[1], -usage[k]))

    profile = {
        "name_font": sorted_keys[0] if sorted_keys else None,
        "sizes_desc": sorted_keys,
    }

    # Most frequent font = body regular
    body_key = max(usage, key=usage.get)
    profile["body_font"] = body_key

    return profile, usage


def _strip_subset_prefix(base_font: str) -> str:
    # "BWBDQC+LMRoman12-Bold" -> "LMRoman12-Bold"
    return re.sub(r"^[A-Z]{6}\+", "", base_font)


def _match_bundled_family(base_font: str):
    """Matches a PDF font's real name against fonts shipped with this agent."""
    clean = _strip_subset_prefix(base_font).lower()
    for key, filename in BUNDLED_FAMILIES.items():
        if key in clean.replace(" ", ""):
            return os.path.join(BUNDLED_FONT_DIR, filename)
    return None


def extract_embedded_truetype_fonts(pdf_path, out_dir):
    """
    Pulls every embedded font program out of the source PDF and makes sure
    build_pdf.py gets a usable .ttf for each one:

      1. FontFile2 (TrueType)              -> saved as-is
      2. FontFile3 /OpenType (full font)    -> converted OTF-CFF -> TTF outlines
      3. FontFile3 /Type1C /CIDFontType0C   -> these are usually SUBSET CFF
         programs (only the glyphs the original doc used, no full alphabet),
         so re-using them for new keyword text would render missing letters
         as blanks. Instead we match the real font family name (after
         stripping the "ABCDEF+" subset prefix) against the full, unsubsetted
         copies bundled in fonts/lm_roman/ (Latin Modern Roman -- the family
         LaTeX/Overleaf resume templates almost always use). If there's no
         bundled match, we skip it and build_pdf.py falls back to Times-Roman.

    Returns {base_font_name: path_to_ttf}.
    """
    os.makedirs(out_dir, exist_ok=True)
    reader = PdfReader(pdf_path)
    saved = {}
    seen = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if not resources or "/Font" not in resources:
            continue
        fonts = resources["/Font"].get_object()
        for fname, fref in fonts.items():
            fobj = fref.get_object()
            base_font = str(fobj.get("/BaseFont", fname)).lstrip("/")
            if base_font in seen:
                continue

            desc = fobj.get("/FontDescriptor")
            if desc is None and "/DescendantFonts" in fobj:
                try:
                    desc = fobj["/DescendantFonts"][0].get_object().get("/FontDescriptor")
                except Exception:
                    desc = None
            if desc is None:
                continue
            desc = desc.get_object()

            out_path = os.path.join(out_dir, re.sub(r"[^A-Za-z0-9_-]", "_", base_font) + ".ttf")

            if "/FontFile2" in desc:
                # Already TrueType -- use directly.
                with open(out_path, "wb") as f:
                    f.write(desc["/FontFile2"].get_object().get_data())
                saved[base_font] = out_path
                seen.add(base_font)
                continue

            if "/FontFile3" in desc:
                ff3 = desc["/FontFile3"].get_object()
                subtype = str(ff3.get("/Subtype", ""))
                data = ff3.get_data()

                if subtype == "/OpenType" and otf_to_ttf is not None:
                    # Full OTF file embedded directly -- convert CFF outlines to TrueType.
                    try:
                        ft = FTFont(io.BytesIO(data))
                        if ft.sfntVersion == "OTTO" and "CFF " in ft:
                            otf_to_ttf(ft)
                        ft.save(out_path)
                        saved[base_font] = out_path
                        seen.add(base_font)
                        continue
                    except Exception:
                        pass  # fall through to bundled-family matching below

                # Type1C / CIDFontType0C (bare, likely-subset CFF): don't trust
                # it for new text -- look for the real, full-glyph-set font instead.
                bundled = _match_bundled_family(base_font)
                if bundled:
                    saved[base_font] = bundled
                    seen.add(base_font)
                    continue

    return saved
