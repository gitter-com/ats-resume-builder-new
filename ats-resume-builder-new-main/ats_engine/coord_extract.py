"""
coord_extract.py
Position-based resume parsing: instead of re-flowing text through a template
(fragile, as extract_generic_sections() showed), this reads each line's exact
(x0, top, x1, bottom) box plus font info, and groups wrapped lines into
"blocks" (one bullet or one paragraph = one block, spanning 1+ visual lines).

coord_rebuild.py then edits only the blocks that need new keywords, drawn
back into their *original* bounding box on top of the *original* PDF page --
everything else, including all hyperlink annotations, is left byte-identical.
"""
import pdfplumber

import re

BOLD_MARKERS = ("bold", "bx", "black", "csc", "caps")
BULLET_MARKERS = ("•", "-", "*", "–", "—", "\u2022", "\u2023", "\u25e6", "\u25aa", "\u25ab", "\u25cf", "\u25cb", "\uf0b7", "\uf0a7", "\x7f")
BULLET_REGEX = re.compile(r"^(\(cid:\d+\)|[\u2022\u2023\u25e6\u25aa\u25ab\u25cf\u25cb\uf0b7\uf0a7\x7f\-\*\–\—])\s*")
PAGE_NUM_PATTERN = re.compile(r"^\s*(page\s+)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)


def _is_bold(fontname: str) -> bool:
    low = fontname.lower()
    return any(m in low for m in BOLD_MARKERS)


def _is_bullet_text(text: str) -> bool:
    return bool(BULLET_REGEX.match(text.strip()))


def get_lines(pdf_path):
    """One entry per visual line of text, with its bounding box and dominant font."""
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            words = page.extract_words(x_tolerance=1.5, extra_attrs=["fontname", "size"])
            words.sort(key=lambda w: (w["top"], w["x0"]))

            # Cluster by top with a tolerance, not exact rounding: a bullet
            # glyph ("•") commonly sits 1-3pt off the baseline of the text it
            # marks, which would otherwise get misread as its own tiny line.
            clusters = []
            for w in words:
                placed = False
                for cluster in clusters:
                    if abs(cluster["top"] - w["top"]) <= 3.2:
                        cluster["words"].append(w)
                        cluster["top"] = min(cluster["top"], w["top"])
                        placed = True
                        break
                if not placed:
                    clusters.append({"top": w["top"], "words": [w]})

            for cluster in clusters:
                ws = sorted(cluster["words"], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in ws)
                is_bullet = bool(ws) and (_is_bullet_text(ws[0]["text"]) or _is_bullet_text(text))
                sized = [(w["fontname"], w["size"]) for w in ws if not _is_bullet_text(w["text"])]
                fontname, size = max(set(sized), key=sized.count) if sized else (ws[0]["fontname"], ws[0]["size"])
                lines.append({
                    "page": page_idx,
                    "top": min(w["top"] for w in ws),
                    "bottom": max(w["bottom"] for w in ws),
                    "x0": min(w["x0"] for w in ws),
                    "x1": max(w["x1"] for w in ws),
                    "text": text,
                    "fontname": fontname,
                    "size": size,
                    "is_bullet_line": is_bullet,
                })
    lines.sort(key=lambda l: (l["page"], l["top"]))
    return lines


def get_blocks(pdf_path, skip_titles=None):
    """
    Groups lines into blocks (one bullet or paragraph = one block spanning
    1+ wrapped lines), and tags each block "skip": True if it falls under a
    heading that matches skip_titles (e.g. Skills / Core Competencies) or
    if it's a page number.
    """
    skip_titles = {t.strip().lower() for t in (skip_titles or [])}
    lines = get_lines(pdf_path)
    if not lines:
        return []

    long_lines = [l for l in lines if len(l["text"].split()) > 5]
    if long_lines:
        body_size = max(set([round(l["size"], 1) for l in long_lines]), key=[round(l["size"], 1) for l in long_lines].count)
    else:
        sizes = [round(l["size"], 1) for l in lines]
        body_size = max(set(sizes), key=sizes.count)

    blocks = []
    current = None
    current_skip = False

    for line in lines:
        raw_text = line["text"].strip()
        words = raw_text.split()
        word_count = len(words)

        if PAGE_NUM_PATTERN.match(raw_text):
            blocks.append({**line, "is_bullet": False, "is_heading": False, "is_page_num": True, "skip": True, "lines": [line]})
            current = None
            continue

        clean_text = BULLET_REGEX.sub("", raw_text).strip()
        is_cont = bool(clean_text and clean_text[0].islower())
        ends_punct = bool(clean_text and clean_text[-1] in (".", ",", ";"))

        is_heading = (
            not line["is_bullet_line"]
            and not is_cont
            and not ends_punct
            and word_count <= 5
            and (
                clean_text.lower() in skip_titles
                or line["size"] > body_size + 1.0
                or (_is_bold(line["fontname"]) and line["size"] >= body_size)
                or (sum(1 for c in clean_text if c.isupper()) / max(1, sum(1 for c in clean_text if c.isalpha())) > 0.85 and word_count <= 4)
            )
            and not any(ch.isdigit() for ch in raw_text)
        )

        if is_heading:
            current_skip = clean_text.lower() in skip_titles or any(
                p in clean_text.lower() for p in ("skill", "competenc", "language")
            )
            blocks.append({**line, "is_bullet": False, "is_heading": True, "skip": current_skip, "lines": [line]})
            current = None
            continue

        starts_new = (
            current is None
            or line["is_bullet_line"]
            or line["page"] != current["page"]
            or (line["top"] - current["bottom"]) > (line["size"] * 1.6)
        )

        if starts_new:
            current = {
                **line, "is_bullet": line["is_bullet_line"], "is_heading": False,
                "skip": current_skip, "lines": [line],
            }
            blocks.append(current)
        else:
            current["text"] += " " + line["text"]
            current["bottom"] = line["bottom"]
            current["x1"] = max(current["x1"], line["x1"])
            current["lines"].append(line)

    return blocks
