"""A cover letter as a one-page PDF, for forms that take the letter as a file (SPEC FA-8).

Plain PDF 1.4 with the built-in Helvetica fonts: no dependency, and every PDF reader shows it the
same way. Lines are wrapped by the fonts' real character widths.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# Helvetica and Helvetica-Bold widths for ASCII 32..126, in 1/1000 of the font size (Adobe AFM).
_REGULAR = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
            556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
            722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
            278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
            556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
         556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
         722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
         278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
         611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]

PAGE_W, PAGE_H = 595.28, 841.89  # A4 in points
MARGIN = 72.0
BODY_SIZE, LEADING = 11.0, 15.5
ASCII = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "…": "...", " ": " ", " ": " ", "‑": "-", "‐": "-"})


def _plain(text: str) -> str:
    """WinAnsi text: curly quotes and dashes made plain; anything else outside Latin-1 dropped."""
    return text.translate(ASCII).encode("cp1252", "ignore").decode("cp1252")


def width(text: str, size: float, bold: bool = False) -> float:
    table = _BOLD if bold else _REGULAR
    return sum(table[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text) * size / 1000


def wrap(text: str, size: float, max_width: float, bold: bool = False) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and width(candidate, size, bold) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return [*lines, line] if line else lines


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(letter: str, *, name: str, contacts: list[str], when: date | None = None) -> bytes:
    """One A4 page: the name in bold, a contact line, the date, then the letter. Text that would
    run past the page goes on a second page."""
    text_w = PAGE_W - 2 * MARGIN
    ops: list[list[str]] = [[]]
    y = PAGE_H - MARGIN

    def put(s: str, x: float, size: float, bold: bool = False, grey: bool = False) -> None:
        nonlocal y
        if y < MARGIN:
            ops.append([])
            y = PAGE_H - MARGIN
        font = "/F2" if bold else "/F1"
        colour = "0.35 g" if grey else "0 g"
        ops[-1].append(f"BT {colour} {font} {size:g} Tf {x:.2f} {y:.2f} Td ({_escape(_plain(s))}) Tj ET")

    put(name, MARGIN, 18, bold=True)
    y -= 18
    for line in wrap("  |  ".join(c for c in contacts if c), 9.5, text_w):
        put(line, MARGIN, 9.5, grey=True)
        y -= 13
    y -= 22
    put((when or date.today()).strftime("%-d %B %Y"), MARGIN, BODY_SIZE)
    y -= LEADING * 2
    for paragraph in _plain(letter).split("\n\n"):
        for raw in paragraph.split("\n"):
            for line in wrap(raw, BODY_SIZE, text_w) or [""]:
                put(line, MARGIN, BODY_SIZE)
                y -= LEADING
        y -= LEADING * 0.6
    return _assemble(["\n".join(page) for page in ops])


def _assemble(pages: list[str]) -> bytes:
    """Objects: 1 catalog, 2 pages, 3-4 fonts, then a page and its content stream per page."""
    n = len(pages)
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    for i, content in enumerate(pages):
        stream = content.encode("cp1252")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                       f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>")
        objects.append(stream)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        if isinstance(obj, bytes):
            out += f"{number} 0 obj\n<< /Length {len(obj)} >>\nstream\n".encode() + obj + b"\nendstream\nendobj\n"
        else:
            out += f"{number} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_letter(path: Path, letter: str, *, name: str, contacts: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_pdf(letter, name=name, contacts=contacts))
    return path
