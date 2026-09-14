"""
coord_rebuild.py
Takes the blocks from coord_extract.py, sends the eligible ones to an LLM for
natural keyword injection, then redraws ONLY those blocks back onto the
ORIGINAL PDF pages at their exact original bounding box. Everything else --
including every hyperlink annotation -- is left byte-identical, because we
never touch the original page content; we only paint over specific boxes.
"""
import io
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import white, black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import extract


def _font_for(fontname: str, registered: dict) -> str:
    clean = extract._strip_subset_prefix(fontname).lower().replace(" ", "")
    for orig, safe in registered.items():
        if extract._strip_subset_prefix(orig).lower().replace(" ", "") == clean:
            return safe
    # loose fallback: match by bold/italic hints
    for orig, safe in registered.items():
        lo = orig.lower()
        if ("bold" in fontname.lower()) == ("bold" in lo) and ("italic" in fontname.lower()) == ("italic" in lo):
            return safe
    return "Times-Roman"


def _fit_text(text, font, size, box_w, box_h, min_size=6.0):
    """Shrinks font size in small steps until the text wraps to fit inside box_h."""
    from reportlab.lib.utils import simpleSplit
    s = size
    while s >= min_size:
        lines = simpleSplit(text, font, s, box_w)
        if len(lines) * (s * 1.22) <= box_h + s * 0.3:
            return s, lines
        s -= 0.25
    return min_size, simpleSplit(text, font, min_size, box_w)


def _measures_fit(text, font, size, box_w, box_h, min_size=8.0):
    """Returns True if `text` can be wrapped to fit within box_h at >= min_size."""
    from reportlab.lib.utils import simpleSplit
    lines = simpleSplit(text, font, min_size, box_w)
    return len(lines) * (min_size * 1.22) <= box_h + min_size * 0.3


def rebuild(pdf_path, edited_blocks: list, registered_fonts: dict, out_path: str, all_blocks: list = None):
    """
    edited_blocks: list of dicts {page, top, bottom, x0, x1, fontname, size, new_text}
    for every block whose text should be replaced. Everything not listed here
    is left completely untouched.

    all_blocks (optional but recommended): the FULL block list from
    coord_extract.get_blocks() for this PDF. Used to cap how far each edited
    block is allowed to grow downward -- it can use empty space up to the
    next block's top, but never past it, so a longer rewrite can never bleed
    into untouched content below it.

    Safety: if a block's new_text genuinely cannot fit in the space available
    (even at the smallest allowed font size), that block is reverted to its
    ORIGINAL text rather than risking silent truncation/overlap. Returns the
    list of block indices (into edited_blocks) that had to be reverted.
    """
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    reverted = []

    # For each edited block, find the top of the next block on the same page
    # (any block, edited or not) so growth has a hard, safe ceiling.
    next_top_by_id = {}
    if all_blocks:
        by_page_all = {}
        for b in all_blocks:
            by_page_all.setdefault(b["page"], []).append(b)
        for page_idx, blist in by_page_all.items():
            blist_sorted = sorted(blist, key=lambda b: b["top"])
            for i, b in enumerate(blist_sorted):
                nxt = blist_sorted[i + 1]["top"] if i + 1 < len(blist_sorted) else None
                next_top_by_id[(page_idx, round(b["top"], 1), round(b["x0"], 1))] = nxt

    by_page = {}
    for i, b in enumerate(edited_blocks):
        by_page.setdefault(b["page"], []).append((i, b))

    for page_idx, page in enumerate(reader.pages):
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)
        edits = by_page.get(page_idx, [])

        if edits:
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(page_w, page_h))
            rendered_count = 0
            for i, b in edits:
                key = (b["page"], round(b["top"], 1), round(b["x0"], 1))
                next_top = next_top_by_id.get(key)
                max_bottom = max(b["bottom"], (next_top - 2) if next_top else (b["bottom"] + 60))

                box_x = b["x0"] - 1
                box_w = min((b["x1"] - b["x0"]) + 40, page_w - box_x - 4)
                usable_bottom = min(max_bottom, b["bottom"] + 60)
                box_h = usable_bottom - b["top"]
                box_y_top = page_h - usable_bottom

                font = _font_for(b["fontname"], registered_fonts)

                if not _measures_fit(b["new_text"], font, b["size"], box_w - 6, box_h, min_size=7.5):
                    # Doesn't safely fit even at the smallest allowed size --
                    # skip this edit entirely rather than risk corrupting the page.
                    reverted.append(i)
                    continue

                c.setFillColor(white)
                c.rect(box_x, box_y_top, min(box_w, page_w - box_x - 4), box_h + 3, fill=1, stroke=0)

                size, lines = _fit_text(b["new_text"], font, b["size"], box_w - 6, box_h, min_size=7.5)
                c.setFillColor(black)
                c.setFont(font, size)
                y = page_h - b["top"] - size
                for line in lines:
                    c.drawString(b["x0"], y, line)
                    y -= size * 1.22
                rendered_count += 1

            if rendered_count > 0:
                c.save()
                buf.seek(0)
                overlay_reader = PdfReader(buf)
                if len(overlay_reader.pages) > 0:
                    page.merge_page(overlay_reader.pages[0])

        writer.add_page(page)

    with open(out_path, "wb") as f:
        writer.write(f)

    return reverted
