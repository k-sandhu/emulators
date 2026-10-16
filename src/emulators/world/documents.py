"""Generators for real, parser-valid seed documents (docx, xlsx, pptx, pdf, csv, md, txt, png).

Why this exists: emulated drives (OneDrive, Google Drive, Box, Dropbox, ...) serve file *bytes*. Code
under test downloads those bytes and runs real parsers (python-docx, openpyxl, python-pptx, pypdf), so a
placeholder blob would fail in a way production never would. Everything here is stdlib-only and
deterministic: identical inputs (including ``created``) give byte-identical output, which keeps content
hashes, ETags, sizes and snapshot tests stable across runs.

Determinism rules: no wall-clock reads (``created`` defaults to a fixed instant), fixed zip entry
timestamps/attributes/order, and checksums/ids derived from content rather than ``uuid4``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import struct
import unicodedata
import zipfile
import zlib
from datetime import UTC, date, datetime
from xml.sax.saxutils import escape

__all__ = [
    "EXTENSION_TO_MIME",
    "MIME_CSV",
    "MIME_DOCX",
    "MIME_MARKDOWN",
    "MIME_PDF",
    "MIME_PNG",
    "MIME_PPTX",
    "MIME_TEXT",
    "MIME_XLSX",
    "make_csv",
    "make_docx",
    "make_markdown",
    "make_pdf",
    "make_png",
    "make_pptx",
    "make_text",
    "make_xlsx",
    "render",
]

MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MIME_PDF = "application/pdf"
MIME_CSV = "text/csv"
MIME_MARKDOWN = "text/markdown"
MIME_TEXT = "text/plain"
MIME_PNG = "image/png"

# Keys are lowercase extensions without the dot; values are what real drives report as the file's mime type.
EXTENSION_TO_MIME: dict[str, str] = {
    "docx": MIME_DOCX,
    "xlsx": MIME_XLSX,
    "pptx": MIME_PPTX,
    "pdf": MIME_PDF,
    "csv": MIME_CSV,
    "md": MIME_MARKDOWN,
    "markdown": MIME_MARKDOWN,
    "txt": MIME_TEXT,
    "png": MIME_PNG,
    "json": "application/json",
    "html": "text/html",
    "htm": "text/html",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "vtt": "text/vtt",
    "zip": "application/zip",
    "eml": "message/rfc822",
    "msg": "application/vnd.ms-outlook",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint",
}

# --------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------

# A fixed default (never "now") is what makes un-parameterised calls reproducible.
_DEFAULT_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_ZIP_MIN = datetime(1980, 1, 1, tzinfo=UTC)
_ZIP_MAX = datetime(2107, 12, 31, 23, 59, 58, tzinfo=UTC)

# Characters XML 1.0 cannot carry at all (C0 controls other than \t \n \r, lone surrogates, U+FFFE/F).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"

_CT = "application/vnd.openxmlformats-officedocument."
_CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_APP = f"{_CT}extended-properties+xml"


def _utc(created: datetime | None) -> datetime:
    """Normalise ``created`` to whole-second UTC; naive values are taken to already be UTC."""
    if created is None:
        return _DEFAULT_CREATED
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.astimezone(UTC).replace(microsecond=0)


def _w3cdtf(stamp: datetime) -> str:
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(text: object) -> str:
    """Drop characters that are illegal in XML (they would make the part unparseable)."""
    return _XML_ILLEGAL.sub("", str(text))


def _esc(text: object) -> str:
    """Escape element text. A bare CR is written as a char ref because parsers normalise it to LF."""
    return escape(_clean(text), {"\r": "&#13;"})


def _attr(text: object) -> str:
    """Escape a value for use inside a double-quoted attribute."""
    return escape(_clean(text), {'"': "&quot;", "\r": "&#13;", "\n": "&#10;", "\t": "&#9;"})


def _xml(root: str) -> str:
    return _XML_DECL + root


def _utf8(text: str) -> bytes:
    """UTF-8 encode, degrading lone surrogates to '?' instead of raising."""
    return text.encode("utf-8", errors="replace")


def _zip_package(parts: list[tuple[str, str | bytes]], stamp: datetime) -> bytes:
    """Write an OPC package deterministically.

    Zip metadata normally leaks the wall clock, the host OS and file modes; pinning all of it is what makes
    the archive bytes a pure function of the inputs. ``[Content_Types].xml`` must come first by convention.
    """
    zstamp = min(max(stamp, _ZIP_MIN), _ZIP_MAX)
    date_time = (zstamp.year, zstamp.month, zstamp.day, zstamp.hour, zstamp.minute, zstamp.second)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            zf.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


def _content_types(overrides: list[tuple[str, str]]) -> str:
    items = "".join(f'<Override PartName="{part}" ContentType="{ctype}"/>' for part, ctype in overrides)
    return _xml(
        f'<Types xmlns="{_NS_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{items}</Types>"
    )


def _rels(entries: list[tuple[str, str, str]]) -> str:
    """Relationships part; ``entries`` are (id, type, target) and short types live under officeDocument."""
    items = ""
    for rid, rtype, target in entries:
        full = rtype if rtype.startswith("http") else f"{_NS_REL}/{rtype}"
        items += f'<Relationship Id="{rid}" Type="{full}" Target="{target}"/>'
    return _xml(f'<Relationships xmlns="{_NS_PKG_REL}">{items}</Relationships>')


def _root_rels(main_target: str) -> str:
    return _rels(
        [
            ("rId1", "officeDocument", main_target),
            ("rId2", _REL_CORE, "docProps/core.xml"),
            ("rId3", "extended-properties", "docProps/app.xml"),
        ]
    )


def _core_xml(title: str, author: str, stamp: datetime) -> str:
    """docProps/core.xml: what Explorer/Finder/Drive show as title, author and dates."""
    ts = _w3cdtf(stamp)
    title_el = f"<dc:title>{_esc(title)}</dc:title>" if title else ""
    return _xml(
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{title_el}<dc:creator>{_esc(author)}</dc:creator>"
        f"<cp:lastModifiedBy>{_esc(author)}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{ts}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{ts}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _app_xml(extra: str = "") -> str:
    return _xml(
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"<Application>emulators</Application><DocSecurity>0</DocSecurity>{extra}"
        "<AppVersion>16.0000</AppVersion></Properties>"
    )


# --------------------------------------------------------------------------------------------------
# DOCX (WordprocessingML)
# --------------------------------------------------------------------------------------------------

_DOCX_STYLES = _xml(
    f'<w:styles xmlns:w="{_NS_W}">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>'
    "</w:rPr></w:rPrDefault>"
    '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    "</w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
    '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/><w:unhideWhenUsed/>'
    "</w:style>"
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/>'
    '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/>'
    '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/>'
    '<w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:uiPriority w:val="10"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="240" w:line="240" w:lineRule="auto"/><w:contextualSpacing/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light" w:eastAsia="Calibri Light" '
    'w:cs="Calibri Light"/><w:spacing w:val="-10"/><w:kern w:val="28"/><w:sz w:val="56"/><w:szCs w:val="56"/>'
    "</w:rPr></w:style>"
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="240" w:after="0"/><w:outlineLvl w:val="0"/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light" w:eastAsia="Calibri Light" '
    'w:cs="Calibri Light"/><w:color w:val="2F5496"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>'
    "</w:styles>"
)

# Without compatibilityMode=15 Word opens the file as "[Compatibility Mode]".
_DOCX_SETTINGS = _xml(
    f'<w:settings xmlns:w="{_NS_W}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/>'
    '<w:characterSpacingControl w:val="doNotCompress"/><w:compat><w:compatSetting w:name="compatibilityMode" '
    'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'
)

_DOCX_SECT = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
    'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
)


def _w_para(text: str, style: str | None = None) -> str:
    """One ``w:p``. Newlines/tabs become ``w:br``/``w:tab`` because raw control chars don't survive in ``w:t``."""
    children: list[str] = []
    for i, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if i:
            children.append("<w:br/>")
        for j, segment in enumerate(line.split("\t")):
            if j:
                children.append("<w:tab/>")
            if segment:
                children.append(f'<w:t xml:space="preserve">{_esc(segment)}</w:t>')
    run = f"<w:r>{''.join(children)}</w:r>" if children else ""
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{run}</w:p>"


def make_docx(
    title: str,
    paragraphs: list[str],
    *,
    author: str = "",
    created: datetime | None = None,
    headings: list[str] | None = None,
) -> bytes:
    """Build a Word document: Title paragraph, then body paragraphs.

    ``headings`` runs parallel to ``paragraphs``; a non-empty entry inserts a Heading 1 before that paragraph
    (entries past the end of ``paragraphs`` are ignored).
    """
    stamp = _utc(created)
    body: list[str] = []
    if title:
        body.append(_w_para(title, "Title"))
    for i, text in enumerate(paragraphs):
        heading = headings[i] if headings and i < len(headings) else ""
        if heading:
            body.append(_w_para(heading, "Heading1"))
        body.append(_w_para(text))
    document = _xml(
        f'<w:document xmlns:w="{_NS_W}" xmlns:r="{_NS_REL}"><w:body>{"".join(body)}{_DOCX_SECT}</w:body></w:document>'
    )
    texts = [title, *paragraphs]
    app_extra = (
        f"<Words>{sum(len(t.split()) for t in texts)}</Words>"
        f"<Characters>{sum(len(''.join(t.split())) for t in texts)}</Characters>"
        f"<CharactersWithSpaces>{sum(len(t) for t in texts)}</CharactersWithSpaces>"
    )
    content_types = _content_types(
        [
            ("/word/document.xml", f"{_CT}wordprocessingml.document.main+xml"),
            ("/word/styles.xml", f"{_CT}wordprocessingml.styles+xml"),
            ("/word/settings.xml", f"{_CT}wordprocessingml.settings+xml"),
            ("/docProps/core.xml", _CT_CORE),
            ("/docProps/app.xml", _CT_APP),
        ]
    )
    return _zip_package(
        [
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", _root_rels("word/document.xml")),
            ("word/document.xml", document),
            (
                "word/_rels/document.xml.rels",
                _rels([("rId1", "styles", "styles.xml"), ("rId2", "settings", "settings.xml")]),
            ),
            ("word/styles.xml", _DOCX_STYLES),
            ("word/settings.xml", _DOCX_SETTINGS),
            ("docProps/core.xml", _core_xml(title, author, stamp)),
            ("docProps/app.xml", _app_xml(app_extra)),
        ],
        stamp,
    )


# --------------------------------------------------------------------------------------------------
# XLSX (SpreadsheetML)
# --------------------------------------------------------------------------------------------------

_EXCEL_EPOCH = datetime(1899, 12, 30)  # Excel's day 0 (compensates for its fictional 1900-02-29)

# cellXfs indices in _XLSX_STYLES below.
_XF_NORMAL, _XF_BOLD, _XF_DATE, _XF_DATETIME, _XF_BOLD_DATE, _XF_BOLD_DATETIME = range(6)

_XLSX_STYLES = _xml(
    f'<styleSheet xmlns="{_NS_S}">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
    '<numFmt numFmtId="165" formatCode="yyyy\\-mm\\-dd\\ hh:mm:ss"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '<xf numFmtId="165" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)

_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


class _SharedStrings:
    """De-duplicated string table; indices are assigned in first-use order so output is deterministic."""

    def __init__(self) -> None:
        self.index: dict[str, int] = {}
        self.refs = 0

    def add(self, text: str) -> int:
        self.refs += 1
        return self.index.setdefault(text, len(self.index))

    def xml(self) -> str:
        items = "".join(f'<si><t xml:space="preserve">{_esc(s)}</t></si>' for s in self.index)
        return _xml(f'<sst xmlns="{_NS_S}" count="{self.refs}" uniqueCount="{len(self.index)}">{items}</sst>')


def _col_letters(index: int) -> str:
    """0-based column index to A, B, ..., Z, AA, ..."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _excel_serial(value: date | datetime) -> float:
    """Excel serial number (days since 1899-12-30); tz-aware datetimes are converted to UTC first."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        delta = value - _EXCEL_EPOCH
        return delta.days + (delta.seconds + delta.microseconds / 1_000_000) / 86_400
    return float((value - _EXCEL_EPOCH.date()).days)


def _sheet_names(raw_names: list[str]) -> list[str]:
    """Apply Excel's sheet-name rules: <=31 chars, none of []:*?/\\, no edge apostrophes, unique ignoring case."""
    used: set[str] = set()
    names: list[str] = []
    for position, raw in enumerate(raw_names, start=1):
        base = _BAD_SHEET_CHARS.sub("_", _clean(raw)).strip().strip("'").strip()[:31] or f"Sheet{position}"
        name, n = base, 1
        while name.lower() in used:
            n += 1
            suffix = f" ({n})"
            name = base[: 31 - len(suffix)] + suffix
        used.add(name.lower())
        names.append(name)
    return names


def _xlsx_cell(ref: str, value: object, header: bool, strings: _SharedStrings) -> tuple[str, int]:
    """Return (cell xml, display width in chars); None yields no cell at all."""
    if value is None:
        return "", 0
    style = _XF_BOLD if header else _XF_NORMAL

    def cell(ctype: str, v: object, s: int = style) -> str:
        s_attr = f' s="{s}"' if s else ""
        t_attr = f' t="{ctype}"' if ctype else ""
        return f'<c r="{ref}"{s_attr}{t_attr}><v>{v}</v></c>'

    if isinstance(value, bool):  # before int: bool is an int subclass
        return cell("b", int(value)), 5
    if isinstance(value, date):  # datetime is a date subclass
        serial = _excel_serial(value)
        if serial >= 1:
            is_dt = isinstance(value, datetime)
            xf = (
                (_XF_BOLD_DATETIME if is_dt else _XF_BOLD_DATE)
                if header
                else (_XF_DATETIME if is_dt else _XF_DATE)
            )
            return cell("", repr(serial), xf), 19 if is_dt else 10
        value = value.isoformat()  # pre-1900 has no Excel serial; keep the information as text
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return cell("e", "#NUM!"), 6
        text = repr(value) if isinstance(value, float) else str(value)
        return cell("", text), len(text)
    text = _clean(value)
    return cell("s", strings.add(text)), max((len(line) for line in text.split("\n")), default=0)


def _sheet_xml(rows: list[list[object]], strings: _SharedStrings, selected: bool) -> str:
    row_xml: list[str] = []
    widths: list[int] = []
    max_cols = 0
    for r, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c, value in enumerate(row):
            xml, width = _xlsx_cell(f"{_col_letters(c)}{r}", value, r == 1, strings)
            if not xml:
                continue
            cells.append(xml)
            max_cols = max(max_cols, c + 1)
            if c >= len(widths):
                widths.extend([0] * (c + 1 - len(widths)))
            widths[c] = max(widths[c], width)
        if cells:
            row_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    dimension = f"A1:{_col_letters(max_cols - 1)}{len(rows)}" if max_cols and rows else "A1"
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{min(max(w + 2, 10), 60)}" customWidth="1"/>'
        for i, w in enumerate(widths, start=1)
    )
    pane = (
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        if row_xml
        else ""
    )
    tab = ' tabSelected="1"' if selected else ""
    return _xml(
        f'<worksheet xmlns="{_NS_S}" xmlns:r="{_NS_REL}"><dimension ref="{dimension}"/>'
        f'<sheetViews><sheetView{tab} workbookViewId="0">{pane}</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{f'<cols>{cols}</cols>' if cols else ''}"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>'
    )


def make_xlsx(
    sheets: dict[str, list[list[object]]],
    *,
    author: str = "",
    created: datetime | None = None,
) -> bytes:
    """Build a workbook from ``{sheet name: rows}``.

    The first row of each sheet is a bold, frozen header. str -> shared string, bool -> boolean,
    int/float -> number, date/datetime -> serial with a date format, None -> empty cell.
    """
    stamp = _utc(created)
    items = list(sheets.items()) or [("Sheet1", [])]
    names = _sheet_names([name for name, _ in items])
    strings = _SharedStrings()
    sheet_parts = [
        (f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows, strings, selected=i == 1))
        for i, (_, rows) in enumerate(items, start=1)
    ]
    count = len(sheet_parts)
    workbook = _xml(
        f'<workbook xmlns="{_NS_S}" xmlns:r="{_NS_REL}">'
        '<bookViews><workbookView xWindow="0" yWindow="0" windowWidth="16000" windowHeight="9000"/></bookViews>'
        "<sheets>"
        + "".join(
            f'<sheet name="{_attr(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(names, start=1)
        )
        + "</sheets></workbook>"
    )
    workbook_rels = _rels(
        [(f"rId{i}", "worksheet", f"worksheets/sheet{i}.xml") for i in range(1, count + 1)]
        + [
            (f"rId{count + 1}", "styles", "styles.xml"),
            (f"rId{count + 2}", "sharedStrings", "sharedStrings.xml"),
        ]
    )
    content_types = _content_types(
        [("/xl/workbook.xml", f"{_CT}spreadsheetml.sheet.main+xml")]
        + [(f"/xl/worksheets/sheet{i}.xml", f"{_CT}spreadsheetml.worksheet+xml") for i in range(1, count + 1)]
        + [
            ("/xl/styles.xml", f"{_CT}spreadsheetml.styles+xml"),
            ("/xl/sharedStrings.xml", f"{_CT}spreadsheetml.sharedStrings+xml"),
            ("/docProps/core.xml", _CT_CORE),
            ("/docProps/app.xml", _CT_APP),
        ]
    )
    return _zip_package(
        [
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", _root_rels("xl/workbook.xml")),
            ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", workbook_rels),
            *sheet_parts,
            ("xl/styles.xml", _XLSX_STYLES),
            ("xl/sharedStrings.xml", strings.xml()),
            ("docProps/core.xml", _core_xml("", author, stamp)),
            ("docProps/app.xml", _app_xml()),
        ],
        stamp,
    )


# --------------------------------------------------------------------------------------------------
# PPTX (PresentationML)
# --------------------------------------------------------------------------------------------------

_PPT_NS = f'xmlns:a="{_NS_A}" xmlns:r="{_NS_REL}" xmlns:p="{_NS_P}"'
_PPT_GROUP = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
    '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)
_PPT_CLR_MAP = (
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
    'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
)
_PPT_LVL_ATTRS = 'algn="l" defTabSz="914400" rtl="0" eaLnBrk="1" latinLnBrk="0" hangingPunct="1"'
_PPT_FONT_REFS = '<a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/>'
_PPT_TEXT_FILL = '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'


def _ppt_theme() -> str:
    """Office-like theme. The format scheme needs >=3 entries per list, which solid placeholders satisfy."""
    colors = "".join(
        f'<a:{name}><a:srgbClr val="{rgb}"/></a:{name}>'
        for name, rgb in (
            ("dk2", "44546A"),
            ("lt2", "E7E6E6"),
            ("accent1", "4472C4"),
            ("accent2", "ED7D31"),
            ("accent3", "A5A5A5"),
            ("accent4", "FFC000"),
            ("accent5", "5B9BD5"),
            ("accent6", "70AD47"),
            ("hlink", "0563C1"),
            ("folHlink", "954F72"),
        )
    )
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    lines = "".join(
        f'<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr">{fill}<a:prstDash val="solid"/><a:miter lim="800000"/></a:ln>'
        for w in (6350, 12700, 19050)
    )
    return _xml(
        f'<a:theme xmlns:a="{_NS_A}" name="Office Theme"><a:themeElements>'
        '<a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
        f'<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>{colors}</a:clrScheme>'
        '<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/>'
        '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
        '<a:cs typeface=""/></a:minorFont></a:fontScheme>'
        f'<a:fmtScheme name="Office"><a:fillStyleLst>{fill * 3}</a:fillStyleLst>'
        f"<a:lnStyleLst>{lines}</a:lnStyleLst>"
        f"<a:effectStyleLst>{'<a:effectStyle><a:effectLst/></a:effectStyle>' * 3}</a:effectStyleLst>"
        f"<a:bgFillStyleLst>{fill * 3}</a:bgFillStyleLst></a:fmtScheme>"
        "</a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>"
    )


def _ppt_sp_pr(x: int, y: int, cx: int, cy: int) -> str:
    return (
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
    )


def _ppt_shape(shape_id: int, name: str, ph: str, sp_pr: str, body: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f"<p:nvPr><p:ph {ph}/></p:nvPr></p:nvSpPr>{sp_pr}<p:txBody>{body}</p:txBody></p:sp>"
    )


def _ppt_prompt(text: str, level: int = 0) -> str:
    ppr = f'<a:pPr lvl="{level}"/>' if level else ""
    return f'<a:p>{ppr}<a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r></a:p>'


def _ppt_master() -> str:
    body_levels = "".join(
        f'<a:lvl{k}pPr marL="{marl}" indent="-228600" {_PPT_LVL_ATTRS}><a:lnSpc><a:spcPct val="90000"/></a:lnSpc>'
        '<a:spcBef><a:spcPts val="1000"/></a:spcBef>'
        '<a:buFont typeface="Arial" panose="020B0604020202020204" pitchFamily="34" charset="0"/>'
        f'<a:buChar char="&#8226;"/><a:defRPr sz="{sz}" kern="1200">{_PPT_TEXT_FILL}'
        f"{_PPT_FONT_REFS}</a:defRPr></a:lvl{k}pPr>"
        for k, marl, sz in (
            (1, 228600, 2800),
            (2, 685800, 2400),
            (3, 1143000, 2000),
            (4, 1600200, 1800),
            (5, 2057400, 1800),
        )
    )
    title_style = (
        f'<a:lvl1pPr {_PPT_LVL_ATTRS}><a:lnSpc><a:spcPct val="90000"/></a:lnSpc>'
        '<a:spcBef><a:spcPct val="0"/></a:spcBef><a:buNone/>'
        f'<a:defRPr sz="4400" kern="1200">{_PPT_TEXT_FILL}<a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/>'
        '<a:cs typeface="+mj-cs"/></a:defRPr></a:lvl1pPr>'
    )
    other_style = (
        f'<a:defPPr><a:defRPr lang="en-US"/></a:defPPr><a:lvl1pPr marL="0" {_PPT_LVL_ATTRS}>'
        f'<a:defRPr sz="1800" kern="1200">{_PPT_TEXT_FILL}{_PPT_FONT_REFS}</a:defRPr></a:lvl1pPr>'
    )
    title = _ppt_shape(
        2,
        "Title Placeholder 1",
        'type="title"',
        _ppt_sp_pr(838200, 365125, 10515600, 1325563),
        '<a:bodyPr vert="horz" lIns="91440" tIns="45720" rIns="91440" bIns="45720" rtlCol="0" anchor="ctr">'
        "<a:normAutofit/></a:bodyPr><a:lstStyle/>" + _ppt_prompt("Click to edit Master title style"),
    )
    body = _ppt_shape(
        3,
        "Text Placeholder 2",
        'type="body" idx="1"',
        _ppt_sp_pr(838200, 1825625, 10515600, 4351338),
        '<a:bodyPr vert="horz" lIns="91440" tIns="45720" rIns="91440" bIns="45720" rtlCol="0"><a:normAutofit/>'
        "</a:bodyPr><a:lstStyle/>"
        + _ppt_prompt("Click to edit Master text styles")
        + "".join(
            _ppt_prompt(label, level) for level, label in enumerate(("Second level", "Third level"), start=1)
        ),
    )
    return _xml(
        f'<p:sldMaster {_PPT_NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>'
        f"<p:spTree>{_PPT_GROUP}{title}{body}</p:spTree></p:cSld>{_PPT_CLR_MAP}"
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/><p:sldLayoutId id="2147483650" r:id="rId2"/>'
        "</p:sldLayoutIdLst>"
        f"<p:txStyles><p:titleStyle>{title_style}</p:titleStyle><p:bodyStyle>{body_levels}</p:bodyStyle>"
        f"<p:otherStyle>{other_style}</p:otherStyle></p:txStyles></p:sldMaster>"
    )


def _ppt_layout_title() -> str:
    title = _ppt_shape(
        2,
        "Title 1",
        'type="ctrTitle"',
        _ppt_sp_pr(1524000, 1122363, 9144000, 2387600),
        '<a:bodyPr anchor="b"><a:normAutofit/></a:bodyPr><a:lstStyle><a:lvl1pPr algn="ctr">'
        '<a:defRPr sz="6000"/></a:lvl1pPr></a:lstStyle>' + _ppt_prompt("Click to edit Master title style"),
    )
    subtitle = _ppt_shape(
        3,
        "Subtitle 2",
        'type="subTitle" idx="1"',
        _ppt_sp_pr(1524000, 3602038, 9144000, 1655762),
        '<a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle><a:lvl1pPr marL="0" indent="0" algn="ctr"><a:buNone/>'
        '<a:defRPr sz="2400"/></a:lvl1pPr></a:lstStyle>' + _ppt_prompt("Click to edit Master subtitle style"),
    )
    return _xml(
        f'<p:sldLayout {_PPT_NS} type="title" preserve="1"><p:cSld name="Title Slide"><p:spTree>{_PPT_GROUP}'
        f"{title}{subtitle}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


def _ppt_layout_content() -> str:
    title = _ppt_shape(
        2,
        "Title 1",
        'type="title"',
        "<p:spPr/>",
        "<a:bodyPr/><a:lstStyle/>" + _ppt_prompt("Click to edit Master title style"),
    )
    body = _ppt_shape(
        3,
        "Content Placeholder 2",
        'idx="1"',
        "<p:spPr/>",
        "<a:bodyPr/><a:lstStyle/>" + _ppt_prompt("Click to edit Master text styles"),
    )
    return _xml(
        f'<p:sldLayout {_PPT_NS} type="obj" preserve="1"><p:cSld name="Title and Content"><p:spTree>{_PPT_GROUP}'
        f"{title}{body}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )


def _a_para(text: str) -> str:
    """One DrawingML paragraph; embedded newlines become ``a:br`` (a raw LF in ``a:t`` is not a line break)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    runs = '<a:br><a:rPr lang="en-US"/></a:br>'.join(
        f'<a:r><a:rPr lang="en-US" dirty="0"/><a:t>{_esc(line)}</a:t></a:r>' if line else "" for line in lines
    )
    return f"<a:p>{runs}</a:p>" if runs else '<a:p><a:endParaRPr lang="en-US"/></a:p>'


def _ppt_slide(shapes: str) -> str:
    return _xml(
        f"<p:sld {_PPT_NS}><p:cSld><p:spTree>{_PPT_GROUP}{shapes}</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def make_pptx(
    title: str,
    slides: list[tuple[str, list[str]]],
    *,
    author: str = "",
    created: datetime | None = None,
) -> bytes:
    """Build a 16:9 deck: a title slide (author as subtitle) then one Title-and-Content slide per entry."""
    stamp = _utc(created)
    plain = "<a:bodyPr/><a:lstStyle/>"
    title_shapes = _ppt_shape(2, "Title 1", 'type="ctrTitle"', "<p:spPr/>", plain + _a_para(title))
    if author:
        title_shapes += _ppt_shape(
            3, "Subtitle 2", 'type="subTitle" idx="1"', "<p:spPr/>", plain + _a_para(author)
        )
    slide_xml = [(_ppt_slide(title_shapes), 1)]
    for slide_title, bullets in slides:
        shapes = _ppt_shape(2, "Title 1", 'type="title"', "<p:spPr/>", plain + _a_para(slide_title))
        # A txBody must hold at least one paragraph, even for a slide with no bullets.
        body = "".join(_a_para(b) for b in bullets) or _a_para("")
        shapes += _ppt_shape(3, "Content Placeholder 2", 'idx="1"', "<p:spPr/>", plain + body)
        slide_xml.append((_ppt_slide(shapes), 2))
    n = len(slide_xml)

    presentation = _xml(
        f'<p:presentation {_PPT_NS} saveSubsetFonts="1">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>'
        + "".join(f'<p:sldId id="{255 + i}" r:id="rId{1 + i}"/>' for i in range(1, n + 1))
        + '</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
        f'<p:defaultTextStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr><a:lvl1pPr marL="0" {_PPT_LVL_ATTRS}>'
        f'<a:defRPr sz="1800" kern="1200">{_PPT_TEXT_FILL}{_PPT_FONT_REFS}</a:defRPr></a:lvl1pPr>'
        "</p:defaultTextStyle></p:presentation>"
    )
    presentation_rels = _rels(
        [("rId1", "slideMaster", "slideMasters/slideMaster1.xml")]
        + [(f"rId{1 + i}", "slide", f"slides/slide{i}.xml") for i in range(1, n + 1)]
        + [
            (f"rId{n + 2}", "presProps", "presProps.xml"),
            (f"rId{n + 3}", "viewProps", "viewProps.xml"),
            (f"rId{n + 4}", "theme", "theme/theme1.xml"),
            (f"rId{n + 5}", "tableStyles", "tableStyles.xml"),
        ]
    )
    content_types = _content_types(
        [
            ("/ppt/presentation.xml", f"{_CT}presentationml.presentation.main+xml"),
            ("/ppt/slideMasters/slideMaster1.xml", f"{_CT}presentationml.slideMaster+xml"),
            ("/ppt/slideLayouts/slideLayout1.xml", f"{_CT}presentationml.slideLayout+xml"),
            ("/ppt/slideLayouts/slideLayout2.xml", f"{_CT}presentationml.slideLayout+xml"),
        ]
        + [(f"/ppt/slides/slide{i}.xml", f"{_CT}presentationml.slide+xml") for i in range(1, n + 1)]
        + [
            ("/ppt/theme/theme1.xml", f"{_CT}theme+xml"),
            ("/ppt/presProps.xml", f"{_CT}presentationml.presProps+xml"),
            ("/ppt/viewProps.xml", f"{_CT}presentationml.viewProps+xml"),
            ("/ppt/tableStyles.xml", f"{_CT}presentationml.tableStyles+xml"),
            ("/docProps/core.xml", _CT_CORE),
            ("/docProps/app.xml", _CT_APP),
        ]
    )
    master_rels = _rels(
        [
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", "slideLayout", "../slideLayouts/slideLayout2.xml"),
            ("rId3", "theme", "../theme/theme1.xml"),
        ]
    )
    layout_rels = _rels([("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")])
    parts: list[tuple[str, str | bytes]] = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", _root_rels("ppt/presentation.xml")),
        ("ppt/presentation.xml", presentation),
        ("ppt/_rels/presentation.xml.rels", presentation_rels),
        ("ppt/slideMasters/slideMaster1.xml", _ppt_master()),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels),
        ("ppt/slideLayouts/slideLayout1.xml", _ppt_layout_title()),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels),
        ("ppt/slideLayouts/slideLayout2.xml", _ppt_layout_content()),
        ("ppt/slideLayouts/_rels/slideLayout2.xml.rels", layout_rels),
    ]
    for i, (xml, layout) in enumerate(slide_xml, start=1):
        parts.append((f"ppt/slides/slide{i}.xml", xml))
        parts.append(
            (
                f"ppt/slides/_rels/slide{i}.xml.rels",
                _rels([("rId1", "slideLayout", f"../slideLayouts/slideLayout{layout}.xml")]),
            )
        )
    parts += [
        ("ppt/theme/theme1.xml", _ppt_theme()),
        ("ppt/presProps.xml", _xml(f"<p:presentationPr {_PPT_NS}/>")),
        ("ppt/viewProps.xml", _xml(f"<p:viewPr {_PPT_NS}/>")),
        (
            "ppt/tableStyles.xml",
            _xml(f'<a:tblStyleLst xmlns:a="{_NS_A}" def="{{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}}"/>'),
        ),
        ("docProps/core.xml", _core_xml(title, author, stamp)),
        ("docProps/app.xml", _app_xml(f"<Slides>{n}</Slides>")),
    ]
    return _zip_package(parts, stamp)


# --------------------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------------------

# Helvetica advance widths (1/1000 em) for ASCII 32..126, from the standard AFM. Only used to wrap lines
# before the right margin, so an approximation for non-ASCII characters (556) is good enough.
# fmt: off
_HELVETICA_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,  # space .. /
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,  # 0 .. ?
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,  # @ A .. O
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,  # P .. _
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,  # ` a .. o
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,  # p .. ~
)
# fmt: on

_PDF_PAGE_W, _PDF_PAGE_H = 612, 792  # US Letter in points
_PDF_MARGIN = 72
_PDF_BODY_SIZE, _PDF_BODY_LEADING = 11, 14
_PDF_TITLE_SIZE, _PDF_TITLE_LEADING = 20, 26
_PDF_PARA_GAP = 7
_PDF_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")


def _pdf_clean(text: str) -> str:
    """Coerce text into WinAnsi (cp1252); anything the font can't encode becomes '?'."""
    text = unicodedata.normalize("NFC", text).replace("\t", "    ")
    text = _PDF_CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _text_width(text: str, size: float, bold: bool = False) -> float:
    total = sum(_HELVETICA_WIDTHS[ord(c) - 32] if 32 <= ord(c) < 127 else 556 for c in text)
    return total * size / 1000 * (1.1 if bold else 1.0)  # bold is ~10% wider; good enough for wrapping


def _wrap(text: str, size: float, max_width: float, bold: bool = False) -> list[str]:
    """Greedy word wrap by real glyph widths. Newlines force breaks; over-long words are split."""
    lines: list[str] = []
    for block in _pdf_clean(text).split("\n"):
        current = ""
        for word in block.split():
            pieces = [word]
            if _text_width(word, size, bold) > max_width:
                pieces, chunk = [], ""
                for ch in word:
                    if chunk and _text_width(chunk + ch, size, bold) > max_width:
                        pieces.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                pieces.append(chunk)
            for piece in pieces:
                trial = f"{current} {piece}" if current else piece
                if current and _text_width(trial, size, bold) > max_width:
                    lines.append(current)
                    current = piece
                else:
                    current = trial
        lines.append(current)
    return lines


def _pdf_literal(text: str) -> bytes:
    """Page-content string: cp1252 bytes, with ( ) \\ and every non-ASCII byte backslash-escaped."""
    out = bytearray(b"(")
    for byte in text.encode("cp1252", errors="replace"):
        if byte in b"()\\":
            out += b"\\" + bytes([byte])
        elif 32 <= byte < 127:
            out.append(byte)
        else:
            out += b"\\%03o" % byte
    return bytes(out + b")")


def _pdf_text_string(text: str) -> bytes:
    """Info-dictionary text string: plain literal if ASCII, else UTF-16BE with BOM (PDF text-string rules)."""
    text = _clean(text)
    if text.isascii():
        return _pdf_literal(text)
    return (
        b"<" + (b"\xfe\xff" + text.encode("utf-16-be", errors="replace")).hex().upper().encode("ascii") + b">"
    )


def _pdf_layout(title: str, paragraphs: list[str]) -> list[bytes]:
    """Lay text out onto Letter pages and return one content stream per page."""
    text_width = _PDF_PAGE_W - 2 * _PDF_MARGIN
    pages: list[list[bytes]] = [[]]
    y = float(_PDF_PAGE_H - _PDF_MARGIN)  # top of the next line

    def emit(font: str, size: int, leading: int, line: str) -> None:
        nonlocal y
        if y - leading < _PDF_MARGIN and pages[-1]:
            pages.append([])
            y = float(_PDF_PAGE_H - _PDF_MARGIN)
        if line:
            op = b"BT /%s %d Tf %d %.2f Td %s Tj ET" % (
                font.encode(),
                size,
                _PDF_MARGIN,
                y - size,
                _pdf_literal(line),
            )
            pages[-1].append(op)
        y -= leading

    if title:
        for line in _wrap(title, _PDF_TITLE_SIZE, text_width, bold=True):
            emit("F2", _PDF_TITLE_SIZE, _PDF_TITLE_LEADING, line)
        y -= _PDF_PARA_GAP
    for paragraph in paragraphs:
        for line in _wrap(paragraph, _PDF_BODY_SIZE, text_width):
            emit("F1", _PDF_BODY_SIZE, _PDF_BODY_LEADING, line)
        y -= _PDF_PARA_GAP
    return [b"\n".join(ops) for ops in pages]


def make_pdf(
    title: str,
    paragraphs: list[str],
    *,
    author: str = "",
    created: datetime | None = None,
) -> bytes:
    """Build a PDF 1.4 file: Helvetica/WinAnsi text on US Letter pages with automatic page breaks.

    Content streams are left uncompressed so the text is also recoverable by naive tools (grep, ``strings``).
    """
    stamp = _utc(created)
    streams = _pdf_layout(title, paragraphs)
    first_page_obj = 6
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(len(streams)))
    pdf_date = f"D:{stamp.year:04d}{stamp.month:02d}{stamp.day:02d}{stamp.hour:02d}{stamp.minute:02d}{stamp.second:02d}Z"
    info = (
        b"<< /Title %s /Author %s /Creator (emulators) /Producer (emulators) /CreationDate (%s) /ModDate (%s) >>"
        % (
            _pdf_text_string(title),
            _pdf_text_string(author),
            pdf_date.encode(),
            pdf_date.encode(),
        )
    )
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids.encode(), len(streams)),
        font % b"Helvetica",
        font % b"Helvetica-Bold",
        info,
    ]
    for i, stream in enumerate(streams):
        contents_obj = first_page_obj + 2 * i + 1
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> /ProcSet [/PDF /Text] >> >>"
            % (_PDF_PAGE_W, _PDF_PAGE_H, contents_obj)
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # high-bit comment marks the file as binary
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_offset = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset  # each entry is exactly 20 bytes
    doc_id = hashlib.md5(bytes(out), usedforsecurity=False).hexdigest().encode()
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R /ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        doc_id,
        doc_id,
        xref_offset,
    )
    return bytes(out)


# --------------------------------------------------------------------------------------------------
# CSV / Markdown / plain text
# --------------------------------------------------------------------------------------------------


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"  # Excel's spelling
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    return str(value)


def make_csv(rows: list[list[object]]) -> bytes:
    """UTF-8 CSV with CRLF row endings and minimal quoting, as Excel writes it (no BOM, so parsers see clean headers)."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    return _utf8(buf.getvalue())


def make_markdown(title: str, paragraphs: list[str], *, headings: list[str] | None = None) -> bytes:
    """``# title`` then paragraphs, with an optional ``## heading`` before paragraph i when ``headings[i]`` is set."""
    blocks = [f"# {title}"] if title else []
    for i, text in enumerate(paragraphs):
        heading = headings[i] if headings and i < len(headings) else ""
        if heading:
            blocks.append(f"## {heading}")
        blocks.append(text)
    return _utf8("\n\n".join(blocks) + "\n" if blocks else "")


def make_text(title: str, paragraphs: list[str]) -> bytes:
    """Plain UTF-8 text: title line, then blank-line separated paragraphs."""
    blocks = ([title] if title else []) + list(paragraphs)
    return _utf8("\n\n".join(blocks) + "\n" if blocks else "")


# --------------------------------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(width: int = 64, height: int = 64, *, seed: int = 0) -> bytes:
    """Valid 8-bit RGB PNG: a seed-coloured diagonal gradient with a checker overlay, so different seeds
    give visibly different (but reproducible) images."""
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")
    digest = hashlib.sha256(f"emulators-png:{seed}".encode()).digest()
    start, end = tuple(digest[0:3]), tuple(digest[3:6])
    cell = max(4, min(width, height) // 8)
    span = max(width + height - 2, 1)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) for this scanline
        for x in range(width):
            t = x + y
            pixel = [a + (b - a) * t // span for a, b in zip(start, end, strict=True)]
            if (x // cell + y // cell) & 1:
                pixel = [max(v - 32, 0) for v in pixel]
            raw += bytes(pixel)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------------

_KIND_ALIASES = {"markdown": "md", "text": "txt"}
_BULLETS_PER_SLIDE = 4


def _slide_title(index: int) -> str:
    return "Overview" if index == 0 else "Details" if index == 1 else f"Details {index}"


def render(
    kind: str,
    title: str,
    paragraphs: list[str],
    *,
    author: str = "",
    created: datetime | None = None,
    table: list[list[object]] | None = None,
) -> tuple[bytes, str]:
    """Build a document by extension-like ``kind`` and return ``(content, mime)``.

    xlsx/csv use ``table`` when given, otherwise a single-column table with one row per paragraph. pptx splits
    paragraphs into slides of up to four bullets titled "Overview", "Details", "Details 2", ...
    """
    key = kind.strip().lower().lstrip(".")
    key = _KIND_ALIASES.get(key, key)
    rows = table if table is not None else [[p] for p in paragraphs]
    if key == "docx":
        content = make_docx(title, paragraphs, author=author, created=created)
    elif key == "xlsx":
        content = make_xlsx({"Sheet1": rows}, author=author, created=created)
    elif key == "pptx":
        chunks = [
            paragraphs[i : i + _BULLETS_PER_SLIDE] for i in range(0, len(paragraphs), _BULLETS_PER_SLIDE)
        ]
        slides = [(_slide_title(i), chunk) for i, chunk in enumerate(chunks)]
        content = make_pptx(title, slides, author=author, created=created)
    elif key == "pdf":
        content = make_pdf(title, paragraphs, author=author, created=created)
    elif key == "csv":
        content = make_csv(rows)
    elif key == "md":
        content = make_markdown(title, paragraphs)
    elif key == "txt":
        content = make_text(title, paragraphs)
    elif key == "png":
        content = make_png(seed=int.from_bytes(hashlib.sha256(_utf8(title)).digest()[:4], "big"))
    else:
        raise ValueError(
            f"unsupported document kind {kind!r}; expected docx, xlsx, pptx, pdf, csv, md, txt or png"
        )
    return content, EXTENSION_TO_MIME[key]
