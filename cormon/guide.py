"""A protocol prepared as a short, clean guide the AI reads in full.

The guide keeps the protocol's own words. It is split into sections under their headings, every table is written
one line per row, and the schedule tables become one checklist per visit (what must happen, the window, and the
footnotes that apply). The checker picks the sections an email is about and reads them whole, so nothing is lost
by first cutting the protocol into small rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import db, llm, settings
from .config import Study

# Word's Symbol font stores some signs in the private-use area.
_SYMBOLS = {0xB1: "±", 0xB3: "≥", 0xA3: "≤", 0xB0: "°", 0xB4: "×", 0xAE: "→", 0xAC: "←", 0xB7: "•", 0xD7: "·",
            0xB8: "÷", 0xB9: "≠", 0xBB: "≈", 0x6D: "µ", 0xA5: "∞", 0x3C: "<", 0x3E: ">", 0x2D: "−", 0x2B: "+",
            0x3D: "=", 0x20: " ", 0xA7: "•", 0xFC: "✓"}

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.{0,100})$")
_MARK = re.compile(r"^x(?![a-z]).{0,60}$", re.I)
_LEADER = re.compile(r"\.{5,}\s*\d+\s*$")
_CAPTION = re.compile(r"^(table|figure)\s+\d+\s*:", re.I)
# footnote numbers typed after a header ("Safety Call 10", "ET Visit 6,8"), but not "Week 4" or "Day 28"
_FOOTREF = re.compile(r"(?<=[A-Za-z])\s+\d+(\s*,\s*\d+)+$|(?<!week)(?<!day)(?<!month)(?<!year)(?<!visit)(?<!cycle)"
                      r"(?<!period)(?<=[A-Za-z])\s+\d+$", re.I)
_KINDS = {"clinic", "phone", "telephone", "remote", "home", "video", "telemedicine", "in person", "in-person"}


def _symbols(text: str) -> str:
    return "".join(_SYMBOLS.get(ord(ch) - 0xF000, "") if 0xF000 <= ord(ch) <= 0xF0FF else ch for ch in text)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", _symbols(text)).strip()
    return re.sub(r"(?<=[a-z])- (?=[A-Z])", "-", text)          # "Follow- Up" from a broken line


@dataclass
class Section:
    id: str
    number: str
    title: str
    path: list[str]
    level: int
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(p for p in self.parts if p.strip()).strip()


@dataclass
class Visit:
    id: str
    name: str
    schedule: str
    kind: str
    window: str
    activities: list[str]
    notes: list[str]

    @property
    def text(self) -> str:
        head = f"{self.name}" + (f" ({self.kind})" if self.kind else "") + (f", window {self.window}" if self.window else "")
        lines = [f"Visit checklist: {head}. From: {self.schedule}.", "Required at this visit:"]
        lines += [f"- {a}" for a in self.activities]
        if self.notes:
            lines.append("Footnotes that apply:")
            lines += [f"[{n}" for n in self.notes]
        return "\n".join(lines)


# ---------------------------------------------------------------- reading a Word file

def _run_text(run) -> str:
    t = run.text
    if run.font.superscript and re.fullmatch(r"\s*\d+(\s*,\s*\d+)*\s*", t or ""):
        return "".join(f" [{n}]" for n in re.findall(r"\d+", t))
    return t


def _para_text(p) -> str:
    return _clean("".join(_run_text(r) for r in p.runs) or p.text)


def _cell_text(cell) -> str:
    return _clean(" ".join(_para_text(p) for p in cell.paragraphs))


def _grid(tbl) -> list[list[str]]:
    rows = []
    for r in tbl.rows:
        rows.append([_cell_text(c) for c in r.cells])
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    nh = _header_rows(rows) if rows else 0
    return rows[:nh] + [r for r in rows[nh:] if r[0] != rows[0][0]]   # headers repeated after a page break


def _header_rows(rows: list[list[str]]) -> int:
    """Header rows repeat the first cell of the first row (a vertically merged corner cell)."""
    n = 1
    while n < len(rows) and rows[n][0] and rows[n][0] == rows[0][0]:
        n += 1
    return n


def _columns(rows: list[list[str]], nh: int) -> list[str]:
    out = []
    for c in range(len(rows[0])):
        bits = []
        for r in rows[:nh]:
            if r[c] and r[c] not in bits:
                bits.append(r[c])
        out.append(" / ".join(bits))
    return out


def _is_grid(rows: list[list[str]], nh: int) -> tuple[bool, int]:
    """A schedule grid: body cells are X marks or blank after a few label columns."""
    body = rows[nh:]
    if len(body) < 3 or len(rows[0]) < 4:
        return False, 0
    for labels in (1, 2, 3):
        cells = [v for r in body for v in r[labels:]]
        if cells and all(not v or _MARK.match(v) for v in cells) and sum(bool(v) for v in cells) >= len(body):
            return True, labels
    return False, 0


def _dedupe(row: list[str]) -> list[str]:
    out = []
    for v in row:
        if not out or v != out[-1]:
            out.append(v)
    return out


def _plain_table(rows: list[list[str]]) -> list[str]:
    nh = _header_rows(rows) if len(rows) > 2 else 1
    cols = _columns(rows, nh)
    if len(set(cols)) <= 1:          # one column: a titled list
        title = cols[0] if cols else ""
        items = [" ".join(_dedupe(r)) for r in rows[nh:] if any(r)]
        return ([f"Table: {title}"] if title else []) + [f"- {i}" for i in items]
    lines = ["Table columns: " + " | ".join(_dedupe(cols))]
    for r in rows[nh:]:
        if not any(r):
            continue
        cells, seen = [], set()
        for c, v in zip(cols, r):
            if v and (c, v) not in seen:
                seen.add((c, v))
                cells.append(f"{c}: {v}" if c and c != v else v)
        lines.append("- " + " | ".join(cells))
    return lines


def _grid_lines(rows: list[list[str]], nh: int, labels: int) -> list[str]:
    cols = _columns(rows, nh)
    short = [rows[0][c] for c in range(len(rows[0]))]
    lines = []
    for r in rows[nh:]:
        label = " ".join(v for v in _dedupe(r[:labels]) if v)
        where = []
        for c in range(labels, len(r)):
            if r[c]:
                note = _mark_note(r[c])
                where.append(short[c] + (f" {note}" if note else ""))
        lines.append(f"- {label}: done at {', '.join(where) if where else 'no visit marked'}")
    return lines


def _mark_note(v: str) -> str:
    """The text after an X mark; bare trailing numbers are footnote references."""
    rest = v[1:].strip()
    return re.sub(r"^(\d+(?:\s*,\s*\d+)*)$", lambda m: " ".join(f"[{n}]" for n in re.findall(r"\d+", m.group(1))), rest)


def _header_parts(hdr: list[str], row_labels: list[str]) -> tuple[str, str, str]:
    """A visit column's header cells sorted into its name(s), kind (clinic or phone) and window. A header row whose
    label names a timeline ("Study Week in ATE") prefixes its names."""
    names, kind, window = [], "", ""
    for h, lab in zip(hdr, row_labels):
        bare = re.sub(r"\s*\[\d+\]", "", h).strip()
        bare = _FOOTREF.sub("", bare)
        if not bare:
            continue
        if bare.lower() in _KINDS:
            kind = bare
        elif re.search(r"\bdays?\b|^day\s*\d", bare, re.I) and not re.match(r"^(week|visit)", bare, re.I):
            window = re.sub(r"(?i)day(?=\d)", "Day ", bare)
        elif bare not in names:
            lab = re.sub(r"\s*\[\d+\]", "", lab).strip()
            names.append(f"{lab}: {bare}" if re.search(r"\b(week|day|visit|month)\b", lab, re.I) else bare)
    return " = ".join(names), kind, window


def _visits(rows: list[list[str]], nh: int, labels: int, schedule: str, notes: dict[str, str],
            prefix: str) -> list[Visit]:
    out = []
    for c in range(labels, len(rows[0])):
        hdr = [rows[i][c] for i in range(nh)]
        name, kind, window = _header_parts(hdr, [rows[i][labels - 1] for i in range(nh)])
        if not name or any(name == v.name for v in out):
            continue
        acts, refs = [], re.findall(r"\[(\d+)\]", " ".join(hdr))
        refs += [n for h in hdr for m in _FOOTREF.finditer(h) for n in re.findall(r"\d+", m.group(0))]
        for r in rows[nh:]:
            if r[c]:
                parts = [v for v in _dedupe(r[:labels]) if v]
                label = parts[0] + "".join(f" ({v})" for v in parts[1:]) if parts else ""
                extra = _mark_note(r[c])
                acts.append(label + (f" {extra}" if extra else ""))
                refs += re.findall(r"\[(\d+)\]", label + " " + extra)
        seen, used = set(), []
        for n in sorted(refs, key=int):
            if n in notes and n not in seen:
                seen.add(n)
                used.append(f"{n}] {notes[n]}")
        slug = re.sub(r"[^a-z0-9]+", "-", name.split(" = ")[0].split(": ")[-1].lower()).strip("-")[:40]
        out.append(Visit(id=f"{prefix}-{slug}", name=name, schedule=schedule, kind=kind, window=window,
                         activities=acts, notes=used))
    return out


def read_docx(path: Path, doc_id: str) -> tuple[list[Section], list[Visit]]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    sections: list[Section] = [Section(id=f"{doc_id}:front", number="", title="Front matter", path=[], level=0)]
    stack: list[tuple[int, str]] = []
    visits: list[Visit] = []
    counters: dict[tuple, int] = {}
    pending_grid: tuple | None = None    # (rows, nh, labels, schedule) waiting for its footnotes
    footnotes: list[str] = []
    last_rows: list[list[str]] | None = None
    n_sec = 0

    def flush_grid():
        nonlocal pending_grid, footnotes
        if pending_grid:
            rows, nh, labels, sched = pending_grid
            notes = {str(i): re.sub(r"^\[\d+\]\s*", "", t) for i, t in enumerate(footnotes, 1)}
            prefix = f"{doc_id}:v{len({v.schedule for v in visits}) + 1}"
            visits.extend(_visits(rows, nh, labels, sched, notes, prefix))
        pending_grid, footnotes = None, []

    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, d)
            text = _para_text(p)
            if _LEADER.search(text) or (p.style is not None and p.style.name.lower().startswith("toc")):
                continue
            style = p.style.name if p.style is not None else ""
            level = int(style[-1]) if style.startswith("Heading") and style[-1].isdigit() else 0
            m = _NUMBERED.match(text) if not level and len(text) < 100 else None
            if m and "." in m.group(1) and not text.endswith("."):
                level = m.group(1).count(".") + 1
            if level and _CAPTION.match(text):
                level = 0
            if level and text and text.lower() not in ("page", "or"):
                flush_grid()
                last_rows = None
                m = _NUMBERED.match(text)
                number, title = (m.group(1), m.group(2).strip()) if m else ("", text)
                while stack and stack[-1][0] >= level:
                    stack.pop()
                n_sec += 1
                sid = f"{doc_id}:{number or f's{n_sec:03d}'}"
                if any(x.id == sid for x in sections):
                    sid = f"{sid}-{n_sec}"
                sections.append(Section(id=sid, number=number, title=title,
                                        path=[t for _, t in stack], level=level))
                stack.append((level, f"{number} {title}".strip()))
                counters.clear()
                continue
            if not text:
                continue
            num = el.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr")
            if num is not None:
                ilvl = num.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ilvl")
                nid = num.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numId")
                key = (nid.get(nid.attrib and list(nid.attrib)[0]) if nid is not None else "",
                       ilvl.get(list(ilvl.attrib)[0]) if ilvl is not None and ilvl.attrib else "0")
                counters[key] = counters.get(key, 0) + 1
                for k in [k for k in counters if k[0] == key[0] and k[1] > key[1]]:
                    counters.pop(k)
                n = counters[key]
                mark = f"{n}." if key[1] == "0" else f"{chr(96 + n) if n <= 26 else n})"
                if not re.match(r"^\d+\s*\S", text) or key[1] != "0":
                    text = f"{mark} {text}"
                elif not text.startswith(f"{n}."):
                    text = re.sub(r"^(\d+)\s*", r"\1. ", text)
            if pending_grid is not None and not text.lower().startswith("abbreviations"):
                footnotes.append(re.sub(r"^\d+\.\s*", "", text))
            sections[-1].parts.append(text)
            last_rows = None
        elif tag == "tbl":
            rows = _grid(Table(el, d))
            if not rows or sum(any(_LEADER.search(v) for v in r) for r in rows) > len(rows) / 2:
                continue    # empty, or a list of tables with page numbers
            if last_rows is not None and rows[0] == last_rows[0] and pending_grid:
                nh = pending_grid[1]
                pending_grid[0].extend(rows[nh:])
                rows_all = pending_grid[0]
                sections[-1].parts.append("\n".join(_grid_lines(rows[:nh] + rows[nh:], nh, pending_grid[2])))
                last_rows = rows_all
                continue
            flush_grid()
            nh = _header_rows(rows)
            grid, labels = _is_grid(rows, nh)
            if grid:
                sched = sections[-1].title
                pending_grid = (rows, nh, labels, sched)
                sections[-1].parts.append(f"Schedule table ({sched}). Columns: "
                                          + " | ".join(_dedupe(_columns(rows, nh)[labels:])))
                sections[-1].parts.append("\n".join(_grid_lines(rows, nh, labels)))
            else:
                sections[-1].parts.append("\n".join(_plain_table(rows)))
            last_rows = rows
    flush_grid()
    return [s for s in sections if s.text or s.level <= 2], visits


# ---------------------------------------------------------------- reading plain text (an OCR'd scan)

_PAGE_MARK = re.compile(r"^=== PAGE \d+$")
_PAGE_LINE = re.compile(r"^(?:page\s*\d+\s*o[ft]\s*\d+|version(?:\s*date)?\s*[\d./]+|confidential|\s)+$", re.I)
_TOC_TAIL = re.compile(r"[\s.•*·…,_-]*\d{1,3}\s*$")
_TOC_LEAD = re.compile(r"\s*[.•*·…]{2,}[\s.•*·…]*")
_LIST_ITEM = re.compile(r"^(\d{1,2}[.)]|[a-z][.)]|[ivx]+[.)]|[•\-–])\s")


def _furniture(lines: list[str]) -> set[str]:
    """Lines repeated on many pages (running headers and footers), and page-number lines."""
    counts: dict[str, int] = {}
    for ln in lines:
        key = re.sub(r"\d+", "#", ln.strip())
        counts[key] = counts.get(key, 0) + 1
    pages = max(1, sum(1 for ln in lines if _PAGE_MARK.match(ln.strip())))
    return {k for k, n in counts.items() if n >= max(3, pages // 3) and len(k) < 80}


def _toc(lines: list[str]) -> tuple[list[tuple[str, str]], int]:
    """The table of contents as (number, title) pairs, and the index of the first body line after it."""
    start = next((i for i, ln in enumerate(lines) if ln.strip().upper() == "TABLE OF CONTENTS"), None)
    if start is None:
        return [], 0
    entries: list[tuple[str, str]] = []
    i = start + 1
    for i in range(start + 1, len(lines)):
        ln = lines[i].strip()
        if not ln or _PAGE_MARK.match(ln):
            continue
        if len(entries) > 1 and _norm_title(ln) == _norm_title(entries[1][1]) and not _TOC_TAIL.search(ln):
            break                                   # the second entry again, as a bare heading: the body starts
        body = _TOC_LEAD.sub(" ", _TOC_TAIL.sub("", ln)).strip()
        if not body:
            continue
        m = re.match(r"^(\d+(?:\.\d+)*)\s+(.+)$", body) or re.match(r"^(.+?)\s+(\d+(?:\.\d+)+)\s*(.*)$", body)
        if m and m.re.pattern.startswith("^(\\d"):
            entries.append((m.group(1), m.group(2).strip()))
        elif m:
            entries.append((m.group(2), (m.group(1) + " " + m.group(3)).strip()))
        else:
            entries.append(("", body))
    return entries, i


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z]+", " ", t.lower()).strip()


def _title_match(a: str, b: str) -> bool:
    wa, wb = set(_norm_title(a).split()), set(_norm_title(b).split())
    return bool(wa and wb) and len(wa & wb) >= 0.6 * min(len(wa), len(wb))


def _join(lines: list[str]) -> str:
    """Lines broken by the page layout joined back into paragraphs; list items and short lines stay separate."""
    out: list[str] = []
    for ln in lines:
        if out and out[-1] and not re.search(r"[.:;?!]$", out[-1]) and not _LIST_ITEM.match(ln) \
                and (ln[:1].islower() or ln[:1].isdigit() or len(out[-1]) > 60):
            out[-1] += ("" if out[-1].endswith("-") else " ") + ln
        else:
            out.append(ln)
    return "\n".join(out)


def read_text(path: Path, doc_id: str) -> tuple[list[Section], list[Visit]]:
    """Sections of a plain-text protocol (e.g. OCR of a scan). Headings are the table of contents' entries found
    as lines in the body, plus numbered sub-headings under them that the contents left out."""
    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    noise = _furniture(raw)
    toc, body_start = _toc(raw)
    numbered = {n: t for n, t in toc if n}
    bare = {_norm_title(t): t for n, t in toc if not n}
    sections = [Section(id=f"{doc_id}:front", number="", title="Front matter", path=[], level=0)]
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    last = (0,)

    def flush():
        if buf:
            sections[-1].parts.append(_join(buf))
            buf.clear()

    def open_section(number: str, title: str):
        nonlocal last
        flush()
        level = number.count(".") + 1 if number else 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        sid = f"{doc_id}:{number}" if number else f"{doc_id}:{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]}"
        if any(s.id == sid for s in sections):
            sid = f"{sid}-{len(sections)}"
        sections.append(Section(id=sid, number=number, title=title, path=[t for _, t in stack], level=level))
        stack.append((level, f"{number} {title}".strip()))
        if number:
            last = tuple(int(x) for x in number.split("."))

    for ln in raw[body_start:]:
        s = _clean(ln)
        if not s or _PAGE_MARK.match(s) or _PAGE_LINE.match(s) or re.sub(r"\d+", "#", s) in noise:
            continue
        m = re.match(r"^(\d+(?:\.\d+)*)\s+([A-Z0-9][^.]{2,95})$", s)
        flipped = False
        if not m and (r := re.match(r"^([A-Z][^.]{2,95}?)\s+(\d+(?:\.\d+)+)$", s)):
            m, flipped = re.match(r"^(\S+) (.+)$", f"{r.group(2)} {r.group(1)}"), True   # OCR put the number last
        if m:
            num = tuple(int(x) for x in m.group(1).split("."))
            known = m.group(1) in numbered and _title_match(m.group(2), numbered[m.group(1)])
            sub = (not flipped and "." in m.group(1) and num[0] == last[0] and num > last and m.group(2)[0].isalpha()
                   and not re.search(r"\d", m.group(2)[:20]))
            if (known and (num > last or num[0] == last[0])) or sub:
                open_section(m.group(1), m.group(2).strip())
                continue
        if _norm_title(s) in bare and len(s) < 100:
            open_section("", bare.pop(_norm_title(s)))
            continue
        buf.append(s)
    flush()
    return [s for s in sections if s.text or s.level <= 2], []


# ---------------------------------------------------------------- what each section is about

TOPICS = ["eligibility", "concomitant_medications", "dosing", "safety", "contraception_pregnancy", "visit_schedule",
          "assessments", "samples", "drug_supply", "consent", "early_termination", "extension_period",
          "records_reporting", "background", "statistics", "admin"]

ABOUT_SYSTEM = """You index a clinical-trial protocol so a checker can find the sections a site email is about.
For each section give a one-line description of what a site must or must not do according to it (or what it
describes, if it sets no duty), and the topics it covers, from this list: """ + ", ".join(TOPICS) + """.
Use background, statistics or admin only for sections a site's actions can never break (rationale, prior study
results, sample size, signature pages, abbreviations, references, questionnaire forms). records_reporting is for
what a site must record, enter, report or notify, and by when (source documents, eCRF, deviations, logs)."""

ABOUT_SCHEMA = llm.obj({"sections": {"type": "array", "items": llm.obj({
    "id": {"type": "string"}, "about": {"type": "string"},
    "topics": {"type": "array", "items": {"type": "string", "enum": TOPICS}}})}})


def describe(sections: list[Section]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    batch: list[str] = []

    def run():
        if not batch:
            return
        res = llm.structured("ingest", ABOUT_SYSTEM, "\n\n".join(batch), ABOUT_SCHEMA, "about")
        for s in res["sections"]:
            out[s["id"]] = {"about": s["about"], "topics": s["topics"]}
        batch.clear()

    for s in sections:
        body = s.text
        sample = body[:1800] + (" ..." if len(body) > 1800 else "")
        batch.append(f"SECTION id={s.id}\nheading: {' > '.join(s.path + [f'{s.number} {s.title}'.strip()])}\n{sample}")
        if sum(len(b) for b in batch) > 24000:
            run()
    run()
    return out


# ---------------------------------------------------------------- storing and loading

def version_of(study: Study, doc_id: str, path: Path) -> str:
    """The document's stated version plus a hash of the file read, so a changed file shows as a new version."""
    stated = study.doc(doc_id).get("version") or ""
    return f"{stated} sha256:{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}".strip()


def prepare(study: Study, doc_id: str, path: Path, log=print) -> dict:
    if path.suffix.lower() == ".docx":
        sections, visits = read_docx(path, doc_id)
    elif path.suffix.lower() in (".txt", ".md"):
        sections, visits = read_text(path, doc_id)
    else:
        raise ValueError(f"guide needs a .docx or text source, got {path.name}")
    version = version_of(study, doc_id, path)
    about = describe(sections)
    db.ex("DELETE FROM guide WHERE study_id=? AND doc_id=?", (study.study_id, doc_id))
    rows = []
    for n, s in enumerate(sections):
        a = about.get(s.id, {"about": "", "topics": []})
        rows.append(dict(id=s.id, kind="section", number=s.number, title=s.title, path=" > ".join(s.path),
                         text=s.text, about=a["about"], topics=a["topics"], ord=n))
    for n, v in enumerate(visits):
        rows.append(dict(id=v.id, kind="visit", number="", title=v.name, path=v.schedule, text=v.text,
                         about=f"what must happen at {v.name} ({v.schedule}), its window and footnotes",
                         topics=["visit_schedule", "assessments"], ord=1000 + n))
    _store(study, doc_id, rows, version)
    out = settings.data_dir() / "guides" / f"{study.study_id}-{doc_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"study_id": study.study_id, "doc_id": doc_id, "source": path.name,
                               "version": version, "items": rows}, indent=1))
    log(f"guide {doc_id}: {len(sections)} sections, {len(visits)} visit checklists, "
        f"{sum(len(r['text']) for r in rows) // 4} tokens -> {out}")
    return {"sections": len(sections), "visits": len(visits), "file": str(out)}


def _store(study: Study, doc_id: str, rows: list[dict], version: str) -> None:
    for r in rows:
        db.ex("INSERT INTO guide(study_id, doc_id, id, kind, number, title, path, text, about, topics, ord, version) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (study.study_id, doc_id, r["id"], r["kind"], r["number"], r["title"], r["path"], r["text"], r["about"],
               json.dumps(r["topics"]), r["ord"], version))


def checker(study: Study) -> dict:
    """The study's checker settings: route "reader" reads the guide; anything else uses the rules.
    CORMON_ROUTE overrides the route, for comparing the two on a test run."""
    c = dict(study.raw.get("checker") or {})
    if os.environ.get("CORMON_ROUTE"):
        c["route"] = os.environ["CORMON_ROUTE"]
    return c


def documents(study: Study) -> list[str]:
    return list(checker(study).get("documents") or [d["id"] for d in study.documents if d.get("guide_file")])


def source(study: Study, doc_id: str) -> Path:
    d = study.doc(doc_id)
    return study.dir / (d.get("guide_file") or d["file"])


def load(study_id: str) -> list[dict]:
    rows = db.q("SELECT * FROM guide WHERE study_id=? ORDER BY doc_id, ord", (study_id,))
    return [dict(r, topics=json.loads(r["topics"] or "[]")) for r in rows]
