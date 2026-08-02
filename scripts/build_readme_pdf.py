from __future__ import annotations

import argparse
import hashlib
import html
import re
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "README.md"
DEFAULT_OUTPUT = ROOT / "output" / "pdf" / "reverse_repo_README.pdf"
DEFAULT_HASH = ROOT / "output" / "pdf" / "reverse_repo_README.sha256"
FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_MONO = Path(r"C:\Windows\Fonts\consola.ttf")
TABLE_DIVIDER = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
LINK = re.compile(r"\[([^]]+)]\(([^)]+)\)")


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register_fonts() -> None:
    for path in (FONT_REGULAR, FONT_BOLD, FONT_MONO):
        if not path.is_file():
            raise FileNotFoundError(f"required PDF font is missing: {path}")
    pdfmetrics.registerFont(TTFont("RRText", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("RRBold", str(FONT_BOLD)))
    pdfmetrics.registerFont(TTFont("RRMono", str(FONT_MONO)))


def inline_markup(value: str) -> str:
    links: list[tuple[str, str]] = []

    def save_link(match: re.Match[str]) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    value = LINK.sub(save_link, value)
    value = html.escape(value, quote=False)
    value = re.sub(r"`([^`]+)`", r'<font name="RRText" color="#174A66">\1</font>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"__([^_]+)__", r"<b>\1</b>", value)
    for index, (label, url) in enumerate(links):
        replacement = html.escape(f"{label} ({url})", quote=False)
        value = value.replace(f"\x00LINK{index}\x00", replacement)
    return value


def split_table_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def visual_length(value: str) -> int:
    return sum(2 if ord(character) > 127 else 1 for character in value)


def table_widths(rows: list[list[str]], available: float) -> list[float]:
    columns = max(len(row) for row in rows)
    weights = []
    for column in range(columns):
        maximum = max(
            visual_length(row[column]) if column < len(row) else 0
            for row in rows
        )
        weights.append(max(8, min(maximum, 38)))
    total = sum(weights)
    return [available * weight / total for weight in weights]


def build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName="RRBold",
            fontSize=22,
            leading=30,
            textColor=colors.HexColor("#12384A"),
            alignment=TA_CENTER,
            spaceAfter=12 * mm,
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta",
            parent=base["BodyText"],
            fontName="RRText",
            fontSize=9,
            leading=15,
            textColor=colors.HexColor("#55636A"),
            alignment=TA_CENTER,
        ),
        "body": ParagraphStyle(
            "BodyCN",
            parent=base["BodyText"],
            fontName="RRText",
            fontSize=9.4,
            leading=15,
            textColor=colors.HexColor("#20282C"),
            spaceAfter=3.2 * mm,
            wordWrap="CJK",
        ),
        "bullet": ParagraphStyle(
            "BulletCN",
            parent=base["BodyText"],
            fontName="RRText",
            fontSize=9.2,
            leading=14,
            bulletFontName="RRText",
            bulletFontSize=9.2,
            leftIndent=7 * mm,
            firstLineIndent=-4 * mm,
            spaceAfter=1.3 * mm,
            wordWrap="CJK",
        ),
        "h1": ParagraphStyle(
            "H1CN",
            parent=base["Heading1"],
            fontName="RRBold",
            fontSize=17,
            leading=23,
            textColor=colors.HexColor("#12384A"),
            spaceBefore=2 * mm,
            spaceAfter=5 * mm,
        ),
        "h2": ParagraphStyle(
            "H2CN",
            parent=base["Heading2"],
            fontName="RRBold",
            fontSize=13,
            leading=18,
            textColor=colors.HexColor("#175D78"),
            spaceBefore=5 * mm,
            spaceAfter=3 * mm,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "H3CN",
            parent=base["Heading3"],
            fontName="RRBold",
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#28728D"),
            spaceBefore=3 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
        ),
        "code": ParagraphStyle(
            "CodeBlock",
            fontName="RRText",
            fontSize=7.7,
            leading=11,
            leftIndent=4 * mm,
            rightIndent=4 * mm,
            borderColor=colors.HexColor("#CDD8DD"),
            borderWidth=0.5,
            borderPadding=3 * mm,
            backColor=colors.HexColor("#F4F7F8"),
            textColor=colors.HexColor("#1D343E"),
            spaceBefore=1 * mm,
            spaceAfter=4 * mm,
        ),
        "table": ParagraphStyle(
            "TableCN",
            fontName="RRText",
            fontSize=7.4,
            leading=10,
            textColor=colors.HexColor("#20282C"),
            wordWrap="CJK",
        ),
        "table_header": ParagraphStyle(
            "TableHeaderCN",
            fontName="RRBold",
            fontSize=7.5,
            leading=10,
            textColor=colors.white,
            wordWrap="CJK",
        ),
    }


def markdown_story(source: str, styles: dict[str, ParagraphStyle], width: float) -> list[object]:
    lines = source.splitlines()
    story: list[object] = []
    paragraph: list[str] = []
    in_code = False
    code_lines: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            story.append(Paragraph(inline_markup(" ".join(paragraph)), styles["body"]))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            if in_code:
                story.append(Preformatted("\n".join(code_lines), styles["code"]))
                code_lines.clear()
            in_code = not in_code
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and TABLE_DIVIDER.match(lines[index + 1]):
            flush_paragraph()
            rows = [split_table_row(line)]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(split_table_row(lines[index]))
                index += 1
            columns = max(len(row) for row in rows)
            normalized = [row + [""] * (columns - len(row)) for row in rows]
            cells = []
            for row_number, row in enumerate(normalized):
                style = styles["table_header"] if row_number == 0 else styles["table"]
                cells.append([Paragraph(inline_markup(cell), style) for cell in row])
            table = Table(
                cells,
                colWidths=table_widths(normalized, width),
                repeatRows=1,
                hAlign="LEFT",
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#175D78")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9C8CE")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F7F8")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.extend([table, Spacer(1, 4 * mm)])
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            story.append(Paragraph(inline_markup(heading.group(2)), styles[f"h{level}"]))
            index += 1
            continue
        bullet = re.match(r"^\s*([-*]|\d+\.)\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            marker = "•" if bullet.group(1) in {"-", "*"} else bullet.group(1)
            story.append(Paragraph(inline_markup(bullet.group(2)), styles["bullet"], bulletText=marker))
            index += 1
            continue
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        paragraph.append(stripped)
        index += 1
    flush_paragraph()
    if code_lines:
        story.append(Preformatted("\n".join(code_lines), styles["code"]))
    return story


def build_pdf(source_path: Path, output_path: Path, hash_path: Path) -> None:
    register_fonts()
    source = source_path.read_text(encoding="utf-8")
    digest = source_sha256(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = build_styles()
    page_width, page_height = A4
    left = right = 21 * mm
    top = 19 * mm
    bottom = 18 * mm
    available = page_width - left - right

    def decorate(canvas: object, document: object) -> None:
        canvas.saveState()
        canvas.setFont("RRText", 7.5)
        canvas.setFillColor(colors.HexColor("#6A767B"))
        canvas.drawString(left, 10 * mm, "miniQMT 国债逆回购自动执行")
        canvas.drawRightString(page_width - right, 10 * mm, f"第 {document.page} 页")
        canvas.setStrokeColor(colors.HexColor("#D8E0E3"))
        canvas.line(left, 13 * mm, page_width - right, 13 * mm)
        canvas.restoreState()

    document = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=right,
        leftMargin=left,
        topMargin=top,
        bottomMargin=bottom,
        title="miniQMT 国债逆回购自动执行",
        author="reverse_repo",
        subject=f"README.md SHA256: {digest}",
        creator="reverse_repo/scripts/build_readme_pdf.py",
    )
    story: list[object] = [
        Spacer(1, 42 * mm),
        Paragraph("miniQMT 国债逆回购自动执行", styles["cover_title"]),
        Paragraph("README 完整版", styles["cover_meta"]),
        Spacer(1, 10 * mm),
        Paragraph(
            f"源文件：reverse_repo/README.md<br/>生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}<br/>源文件SHA-256：{digest}",
            styles["cover_meta"],
        ),
        PageBreak(),
    ]
    story.extend(markdown_story(source, styles, available))
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    hash_path.write_text(digest + "\n", encoding="ascii")


def check_pdf(source_path: Path, output_path: Path, hash_path: Path) -> None:
    if not output_path.is_file():
        raise SystemExit(f"README PDF is missing: {output_path}")
    if not hash_path.is_file():
        raise SystemExit(f"README PDF hash is missing: {hash_path}")
    expected = source_sha256(source_path)
    recorded = hash_path.read_text(encoding="ascii").strip().lower()
    if recorded != expected:
        raise SystemExit(
            "README PDF is stale. Run reverse_repo/build_readme_pdf.ps1."
        )
    if output_path.stat().st_size < 10_000:
        raise SystemExit("README PDF is unexpectedly small or incomplete.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the complete reverse_repo README PDF.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--hash-output", type=Path, default=DEFAULT_HASH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_pdf(args.source, args.output, args.hash_output)
    else:
        build_pdf(args.source, args.output, args.hash_output)
        check_pdf(args.source, args.output, args.hash_output)
        print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
