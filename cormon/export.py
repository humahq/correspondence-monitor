"""Export the study model for people to read: a Word document of the rules, a spreadsheet of the same, and copies
of the documents the rules were made from."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from . import db, ingest, prune
from .config import Study

WORDS = {"lt": "below", "le": "at or below", "gt": "above", "ge": "at or above", "eq": "equal to",
         "ne": "not equal to"}


def _n(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _test(t: dict | None) -> str:
    if not t:
        return ""
    p = t["parameter"].replace("_", " ")
    if t["operator"] == "outside":
        return f"{p} outside {_n(t['value'])} to {_n(t['value2'])}"
    if t["operator"] == "between":
        return f"{p} between {_n(t['value'])} and {_n(t['value2'])}"
    return f"{p} {WORDS.get(t['operator'], t['operator'])} {_n(t['value'])}"


def describe_check(r: dict) -> tuple[str, str, str, str]:
    """(how it is checked, alert when, applies when, exception) in plain words."""
    if r["check_type"] != "numeric":
        return "Judgment: read by the AI, then confirmed against the document", "", "", ""
    alert = _test({"parameter": r["parameter"], "operator": r["operator"], "value": r["value"],
                   "value2": r["value2"]})
    unless = r.get("unless") or []
    exc = " and ".join("an exception in the document applies (judged by the AI)"
                       if t["parameter"] == "exception_applies" else _test(t) for t in unless)
    return "Number: checked by code", alert, _test(r.get("condition")), exc


def _rows(study: Study) -> list[dict]:
    order = {d["id"]: i for i, d in enumerate(study.documents)}
    classes = {c["id"]: i for i, c in enumerate(study.pattern_classes)}
    out = []
    for r in db.q("SELECT * FROM rules WHERE study_id=? ORDER BY doc_id, pattern_class, order_idx", (study.study_id,)):
        d = dict(r)
        d["condition"] = db.unjs(d["condition"])
        d["unless"] = db.unjs(d.get("unless"), [])
        out.append(d)
    return sorted(out, key=lambda r: (order.get(r["doc_id"], 99), classes.get(r["pattern_class"], 99), r["order_idx"]))


def _checklist(study: Study, rows: list[dict]) -> list[tuple[str, str, str, str]]:
    """(document, criterion, status, rule ids or reason)"""
    out = []
    for doc in study.documents:
        if doc["kind"] not in ("protocol", "lab_manual"):
            continue
        items = ingest.checklist_items(study.study_id, doc["id"], doc["kind"])
        for item in items:
            hits = [r for r in rows if ingest._covers(r["source_quote"], item)
                    and not ingest.bundles_criteria(r["source_quote"], items)]
            live = [r["id"] for r in hits if r["active"]]
            if live:
                out.append((doc["title"], item, "Has a rule", ", ".join(live)))
            elif any("duplicate" in (r["review"] or "") for r in hits):
                out.append((doc["title"], item, "Has a rule", "(covered by a rule with the same meaning)"))
            elif hits:
                out.append((doc["title"], item, "No rule, with reason", hits[0]["review"] or ""))
            else:
                out.append((doc["title"], item, "MISSING", ""))
    return out


def _why_not_used(review: str | None) -> str:
    text = (review or "").strip()
    for prefix in ("dropped by review", "dropped:", "dropped"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip(" :")
            break
    return text or "not used"


def word(study: Study, rows: list[dict], checklist: list[tuple], path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    active = [r for r in rows if r["active"]]
    unused = [r for r in rows if not r["active"]]
    numbers = [r for r in active if r["check_type"] == "numeric"]
    labels = {p["id"]: p["label"] for p in study.pattern_classes}
    titles = {d["id"]: d["title"] for d in study.documents}

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    t = doc.add_heading(f"{study.short_name} study rules", 0)
    t.alignment = WD_ALIGN_PARAGRAPH.LEFT
    doc.add_paragraph(f"{study.title} ({study.study_id})\nExported {dt.date.today():%d %B %Y} from the "
                      f"correspondence monitor's study model.")

    doc.add_heading("1. What this document is", 1)
    for line in [
        "The monitor reads the study documents and turns every requirement into a short rule. It then checks "
        "each email against these rules. This document lists every rule it uses, and the exact words in the "
        "study document that each rule comes from.",
        "There are two kinds of rule. Number rules (for example \"eGFR below 45\") are checked by code, so they "
        "give the same answer every time. Judgment rules (for example \"consent must be signed before any study "
        "procedure\") are read by the AI. Every alert from either kind is confirmed against the original "
        "document before it is sent.",
        "Every rule's quote was checked word for word against the document. Rules whose quote could not be "
        "found were not used.",
    ]:
        doc.add_paragraph(line)
    covered = sum(1 for c in checklist if c[2] == "Has a rule")
    reasoned = sum(1 for c in checklist if c[2] == "No rule, with reason")
    missing = sum(1 for c in checklist if c[2] == "MISSING")
    doc.add_paragraph(f"In use: {len(active)} rules ({len(numbers)} number rules, {len(active) - len(numbers)} "
                      f"judgment rules). Considered and not used: {len(unused)} (reasons in section 5).")
    doc.add_paragraph(f"Coverage: of {len(checklist)} inclusion/exclusion criteria and lab manual steps, "
                      f"{covered} have a rule, {reasoned} have none with a reason, {missing} are missing "
                      f"(section 4).")
    if any("placeholder" in d["title"].lower() for d in study.documents):
        p = doc.add_paragraph()
        r = p.add_run("Note: no lab manual for this study is public, so the lab manual used here is a placeholder "
                      "written for the demo. Its rules show how the monitor works; they are not the sponsor's.")
        r.italic = True

    doc.add_heading("2. Number rules at a glance", 1)
    tbl = doc.add_table(rows=1, cols=5)
    tbl.style = "Light Grid Accent 1"
    for c, h in zip(tbl.rows[0].cells, ["Value", "Alert when", "Only for", "Not when", "Rule"]):
        c.text = h
    for r in numbers:
        _, alert, band, exc = describe_check(r)
        cells = tbl.add_row().cells
        cells[0].text = r["parameter"].replace("_", " ")
        cells[1].text = alert
        cells[2].text = band
        cells[3].text = exc
        cells[4].text = r["id"]

    doc.add_heading("3. All rules in use", 1)
    last = None
    for r in active:
        key = (r["doc_id"], r["pattern_class"])
        if key != last:
            doc.add_heading(f"{titles.get(r['doc_id'], r['doc_id'])}: {labels.get(r['pattern_class'], r['pattern_class'])}", 2)
            last = key
        how, alert, band, exc = describe_check(r)
        p = doc.add_paragraph(style="List Number")
        p.add_run(r["text"]).bold = True
        detail = [how]
        if alert:
            detail.append(f"alert when {alert}")
        if band:
            detail.append(f"only for {band}")
        if exc:
            detail.append(f"not when {exc}")
        meta = doc.add_paragraph()
        meta.paragraph_format.left_indent = Pt(18)
        run = meta.add_run(f"{'; '.join(detail)}.  Rule {r['id']}, page {r['source_page'] or '-'}.")
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        q = doc.add_paragraph()
        q.paragraph_format.left_indent = Pt(18)
        qr = q.add_run(f"\u201c{r['source_quote']}\u201d")
        qr.italic = True
        qr.font.size = Pt(9)

    doc.add_heading("4. Coverage checklist", 1)
    doc.add_paragraph("Every bullet of the protocol's inclusion and exclusion criteria, and every requirement "
                      "sentence of the lab manual, with the rule that covers it or the reason it has none.")
    tbl = doc.add_table(rows=1, cols=3)
    tbl.style = "Light Grid Accent 1"
    for c, h in zip(tbl.rows[0].cells, ["Criterion or step", "Status", "Rule(s) or reason"]):
        c.text = h
    for _, item, status, detail in checklist:
        cells = tbl.add_row().cells
        cells[0].text = item
        cells[1].text = status
        cells[2].text = detail if status == "Has a rule" else _why_not_used(detail)

    doc.add_heading("5. Rules considered and not used", 1)
    doc.add_paragraph("Candidate rules the monitor wrote and then left out, with the reason. Shown so nothing is "
                      "dropped silently.")
    for r in unused:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(r["text"][:300])
        why = p.add_run(f"  Why not used: {_why_not_used(r['review'])}")
        why.italic = True
        why.font.size = Pt(9)
    doc.save(path)


def sheet(study: Study, rows: list[dict], checklist: list[tuple], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    labels = {p["id"]: p["label"] for p in study.pattern_classes}
    titles = {d["id"]: d["title"] for d in study.documents}
    made = prune.decisions(study.study_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Rules in use"
    ws.append(["Rule", "Document", "Problem type", "Rule text", "Check", "Alert when", "Only for", "Not when",
               "Page", "Exact words in the document", "AI suggests", "Why", "Team decision", "Team note"])
    for r in rows:
        removed = (r["review"] or "").startswith(prune.REMOVED)
        if r["active"] or removed:
            how, alert, band, exc = describe_check(r)
            d = made.get(prune.rule_key(r), {})
            ws.append([r["id"], titles.get(r["doc_id"], r["doc_id"]), labels.get(r["pattern_class"], r["pattern_class"]),
                       r["text"], how, alert, band, exc, r["source_page"], r["source_quote"],
                       d.get("suggest") or "", d.get("why") or "", d.get("decision") or "", d.get("note") or ""])
    nu = wb.create_sheet("Not used")
    nu.append(["Rule", "Document", "Problem type", "Rule text", "Why not used", "Exact words in the document"])
    for r in rows:
        if not r["active"] and not (r["review"] or "").startswith(prune.REMOVED):
            nu.append([r["id"], titles.get(r["doc_id"], r["doc_id"]), labels.get(r["pattern_class"], r["pattern_class"]),
                       r["text"], _why_not_used(r["review"]), r["source_quote"]])
    cl = wb.create_sheet("Coverage checklist")
    cl.append(["Document", "Criterion or step", "Status", "Rule(s) or reason"])
    for d, item, status, detail in checklist:
        cl.append([d, item, status, detail if status == "Has a rule" else _why_not_used(detail)])
    widths = {"A": 28, "B": 30, "C": 22, "D": 60, "E": 30, "F": 26, "G": 22, "H": 26, "I": 6, "J": 70,
              "K": 12, "L": 50, "M": 14, "N": 40}
    for w in (ws, nu, cl):
        for cell in w[1]:
            cell.font = Font(bold=True)
        for col, width in widths.items():
            w.column_dimensions[col].width = width
        for row in w.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        w.freeze_panes = "A2"
    wb.save(path)


def lab_manual_word(src: Path, path: Path) -> None:
    from docx import Document

    doc = Document()
    for line in src.read_text().splitlines():
        if line.startswith("# "):
            doc.add_heading(line[2:], 0)
        elif line.startswith("## "):
            doc.add_heading(line[3:], 1)
        elif line.startswith(("- ", "* ")):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.strip():
            doc.add_paragraph(line)
    doc.save(path)


def export(study: Study, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    rows = _rows(study)
    checklist = _checklist(study, rows)
    stamp = f"{dt.date.today():%Y-%m-%d}"
    made = [out / f"{study.short_name} rules {stamp}.docx", out / f"{study.short_name} rules {stamp}.xlsx"]
    word(study, rows, checklist, made[0])
    sheet(study, rows, checklist, made[1])
    docs = out / "Source documents"
    docs.mkdir(exist_ok=True)
    for d in study.documents:
        src = study.doc_path(d["id"])
        name = d["title"].replace("/", "-")
        if src.suffix == ".md":
            made.append(docs / f"{name}.docx")
            lab_manual_word(src, made[-1])
        else:
            made.append(docs / f"{name}{src.suffix}")
            shutil.copy2(src, made[-1])
    return made
