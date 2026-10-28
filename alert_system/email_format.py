"""Render email content as tables: an HTML part (Gmail-safe inline styles)
plus an aligned plain-text part for clients that do not show HTML.

Content is described as blocks:
  ("title", text) | ("heading", text) | ("para", text) | ("note", text)
  ("warning", text) | ("table", headers, rows, align)   align: "l"/"r" per column
Cells may be plain values or Cell(text, color) for green/red highlighting.
"""
from dataclasses import dataclass
from html import escape

GREEN, RED, GREY = "#137333", "#b3261e", "#5f6368"
FONT = "font-family:Arial,Helvetica,sans-serif;"


@dataclass(frozen=True)
class Cell:
    text: str
    color: str = None
    bold: bool = False


def pct_cell(value, digits=2):
    if value is None:
        return Cell("n/a", GREY)
    return Cell(f"{value:+.{digits}f}%", GREEN if value > 0 else RED if value < 0 else None)


def money(v):
    return "n/a" if v is None else f"₹{v:,.2f}"


def _text(c):
    return c.text if isinstance(c, Cell) else ("" if c is None else str(c))


def render_text(blocks):
    out = []
    for b in blocks:
        kind = b[0]
        if kind == "title":
            out += [b[1], "=" * len(b[1]), ""]
        elif kind == "heading":
            out += ["", b[1].upper(), "-" * len(b[1])]
        elif kind in ("para", "note"):
            out.append(b[1])
        elif kind == "warning":
            out.append(f"*** {b[1]} ***")
        elif kind == "table":
            headers, rows, align = b[1], b[2], b[3]
            cells = [[_text(c) for c in r] for r in rows]
            widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

            def fmt(vals):
                return "  ".join(v.rjust(w) if a == "r" else v.ljust(w)
                                 for v, w, a in zip(vals, widths, align)).rstrip()
            out += [fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in cells]
    return "\n".join(out).strip() + "\n"


def _td(c, align, header=False):
    style = f"padding:6px 10px;border-bottom:1px solid #e0e0e0;text-align:{'right' if align == 'r' else 'left'};"
    if header:
        style += "background:#1f3a5f;color:#ffffff;font-weight:bold;white-space:nowrap;"
    text = _text(c)
    if isinstance(c, Cell):
        if c.color:
            style += f"color:{c.color};"
        if c.bold:
            style += "font-weight:bold;"
    tag = "th" if header else "td"
    return f'<{tag} style="{style}">{escape(text)}</{tag}>'


def render_html(blocks):
    parts = [f'<div style="{FONT}font-size:14px;color:#202124;max-width:900px;">']
    for b in blocks:
        kind = b[0]
        if kind == "title":
            parts.append(f'<h2 style="{FONT}color:#1f3a5f;margin:0 0 12px;">{escape(b[1])}</h2>')
        elif kind == "heading":
            parts.append(f'<h3 style="{FONT}color:#1f3a5f;margin:22px 0 8px;border-bottom:2px solid #1f3a5f;'
                         f'padding-bottom:4px;">{escape(b[1])}</h3>')
        elif kind == "para":
            parts.append(f'<p style="margin:6px 0;">{escape(b[1])}</p>')
        elif kind == "note":
            parts.append(f'<p style="margin:4px 0;color:{GREY};font-size:12px;">{escape(b[1])}</p>')
        elif kind == "warning":
            parts.append(f'<p style="margin:8px 0;padding:8px 12px;background:#fdecea;border-left:4px solid {RED};'
                         f'color:{RED};font-weight:bold;">{escape(b[1])}</p>')
        elif kind == "table":
            headers, rows, align = b[1], b[2], b[3]
            parts.append('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;'
                         'border:1px solid #d0d7de;margin:4px 0 8px;font-size:13px;">')
            parts.append("<tr>" + "".join(_td(h, a, header=True) for h, a in zip(headers, align)) + "</tr>")
            for i, r in enumerate(rows):
                bg = "#f6f8fa" if i % 2 else "#ffffff"
                parts.append(f'<tr style="background:{bg};">' + "".join(_td(c, a) for c, a in zip(r, align)) + "</tr>")
            parts.append("</table>")
    parts.append("</div>")
    return "\n".join(parts)
