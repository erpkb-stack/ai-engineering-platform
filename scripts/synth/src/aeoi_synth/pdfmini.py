"""Tiny deterministic PDF writer (text only, Helvetica, A4). No dependencies.

Why not a PDF library: we need byte-identical output for the same input (the eval corpus is
committed and checked by hash), plain text pages, nothing else. ~60 lines beat a dependency
that embeds timestamps and fonts.
"""

from __future__ import annotations

import textwrap

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN, LEADING, SIZE, WRAP = 56, 14, 10, 95
LINES_PER_PAGE = (PAGE_H - 2 * MARGIN) // LEADING


def _escape(s: str) -> str:
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        out.extend(textwrap.wrap(para, WRAP) or [""])
    return out


def render_pdf(title: str, body: str) -> bytes:
    lines = _lines(body)
    pages = [lines[i : i + LINES_PER_PAGE] for i in range(0, max(len(lines), 1), LINES_PER_PAGE)]
    objects: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("latin-1") if isinstance(obj, str) else obj)
        return len(objects)

    catalog = add("")  # placeholder 1
    pages_obj = add("")  # placeholder 2
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids = []
    for page in pages:
        ops = [f"BT /F1 {SIZE} Tf {LEADING} TL {MARGIN} {PAGE_H - MARGIN} Td"]
        ops += [f"({_escape(line)}) Tj T*" for line in page]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    info = add(f"<< /Title ({_escape(title)}) /Producer (aeoi_synth.pdfmini) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        info,
        xref,
    )
    return bytes(out)
