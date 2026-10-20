"""Topic packs: everything a site must know about one topic, gathered from every study document.

A site email about dosing is checked against the dosing pack, not against each document in turn. A pack holds the
documents' own words (verbatim excerpts, each tagged with its source section), grouped under short headings, with
rationale and boilerplate left out. Where a study-specific appendix replaces a Master Protocol step, the pack keeps
both and says which one governs.

Every excerpt is found in its source by code, and the rule-like sentences of each source section that no excerpt
covers are offered to a second pass, so a requirement is not silently left out. Each pack then also carries the
other packs' sub-topics whose rules apply when something in it goes wrong (an overdose's reporting duty in the
dosing pack). Packs record the versions of the documents they were built from.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import db, guide, ingest, llm, settings, textutil
from .config import Study

NOT_CHECKABLE = {"background", "statistics", "admin"}
PACK_TOPICS = [t for t in guide.TOPICS if t not in NOT_CHECKABLE]
LABELS = {
    "eligibility": "Eligibility (who can join)", "concomitant_medications": "Other medicines (allowed, restricted, banned)",
    "dosing": "Study drug dosing and dose changes", "safety": "Safety, adverse events and reporting",
    "contraception_pregnancy": "Contraception and pregnancy", "visit_schedule": "Visit schedule and windows",
    "assessments": "Assessments and procedures", "samples": "Samples: collection, timing, handling, shipping",
    "drug_supply": "Study drug supply, storage, dispensing and accountability", "consent": "Informed consent",
    "early_termination": "Stopping treatment and early termination", "extension_period": "Extension period (ATE)",
    "records_reporting": "Records, data entry, deviations and notifications",
}

PACK_SYSTEM = """You build one topic pack for a clinical-trial monitor: everything a study site must know about the
topic {topic} ({label}), gathered from the protocol sections given. The sections come from more than one document.
Copy, as verbatim excerpts, every requirement, limit, timing, window, condition, exception, allowance, list item and
required step about this topic: whole sentences, exactly as written, keeping numbers, units and footnote text. When a
list item only makes sense with its lead-in sentence ("Participants who ..., must:"), include the lead-in as its own
excerpt just before the items. Include items that are mainly about another topic only where they set a rule for this
one. Leave out rationale, background, results of other studies, statistics and administrative boilerplate.
Group the excerpts under short headings, one sub-concept each (for example "dose timing", "missed doses", "dose
interruption"), so that everything about a sub-concept is together even when it comes from different documents.
For each excerpt give its source section id, applies_to (who or what period it applies to, e.g. "all participants",
"women of childbearing potential", "ATE only", "" if general) and note (empty unless needed). {override}
Do not reword, summarise or merge excerpts."""

GAP_SYSTEM = """You complete the topic packs of a clinical-trial monitor. These protocol sentences are in no pack
yet. For each sentence that states something a study site must do, must not do, or may do (a requirement, limit,
timing, condition, exception or required step), give every pack it sets a rule for, and a short heading for it
(reuse a pack's heading when one fits). Give no packs for rationale, background, statistics, definitions with no
duty, or boilerplate. Packs: """ + "; ".join(f"{t} ({LABELS[t]})" for t in PACK_TOPICS)

GAP_SCHEMA = llm.obj({"sentences": {"type": "array", "items": llm.obj({
    "n": {"type": "integer"},
    "packs": {"type": "array", "items": {"type": "string", "enum": PACK_TOPICS}},
    "heading": {"type": "string"}})}})

LINK_SYSTEM = """You link the topic packs of a clinical-trial monitor. A site email about one topic often breaks a
rule kept in another topic's pack: a double dose (dosing) must also be reported as an overdose (safety); a dose given
in clinic (dosing) needs the second-person check (drug_supply); a missed sample (samples) must be recorded as a
deviation (records_reporting). You get one pack in full and the sub-topics of the other packs (id | heading | first
rule). Give every other sub-topic whose rules a site must also follow when something in this pack happens or goes
wrong: reporting duties, required checks, steps that must come first or after, deviations to record, who must be
told. Only links a monitor would need to catch a problem; at most 8. For each, say in one short sentence when it
applies."""

LINK_SCHEMA = llm.obj({"links": {"type": "array", "items": llm.obj({
    "piece": {"type": "string"},
    "when": {"type": "string"}})}})

BRIEF_SYSTEM = """You write the brief of a clinical-trial protocol for a monitor who checks site emails: every rule a
study site must follow, as short lines a monitor can hold in mind all at once. For each section given, write one line
per distinct requirement, limit, window, condition, exception, allowance or required step, in plain words, at most
about 30 words. Keep every number, unit, window, count, drug name, test name and visit name exactly as in the source,
and keep conditions ("only if", "unless", "for participants who") and what to do when something goes wrong (report to
whom and when, record a deviation, consult the medical monitor). A line must stand alone: name the visit, drug or
procedure it is about. Do not repeat a rule already written for an earlier section given here. Leave out rationale,
background, results of other studies, statistics, definitions with no duty, and administrative boilerplate.
For each line give: section (the id in brackets), applies_to (who or what period, "" if everyone), and topics: every
topic a site would need this rule for, including when something in that topic goes wrong (an overdose's reporting
duty is dosing and safety; recording a missed sample as a deviation is samples and records_reporting; the second
staff member confirming a clinic dose is dosing and drug_supply). Topics: """ + "; ".join(
    f"{t} ({LABELS[t]})" for t in PACK_TOPICS)

BRIEF_SCHEMA = llm.obj({"lines": {"type": "array", "items": llm.obj({
    "section": {"type": "string"},
    "rule": {"type": "string"},
    "applies_to": {"type": "string"},
    "topics": {"type": "array", "items": {"type": "string", "enum": PACK_TOPICS}}})}})

PACK_SCHEMA = llm.obj({"groups": {"type": "array", "items": llm.obj({
    "heading": {"type": "string"},
    "items": {"type": "array", "items": llm.obj({
        "source": {"type": "string"},
        "excerpt": {"type": "string"},
        "applies_to": {"type": "string"},
        "note": {"type": "string"}})}})}})

_RULE_WORDS = re.compile(
    r"\b(must|should|shall|will be|will not|may not|not be|only|within|prior to|before|after|at least|no more|no later|"
    r"required|requires|prohibited|excluded|exclusion|inclusion|allowed|permitted|hours?|days?|weeks?|months?)\b|[≤≥<>±]",
    re.I)


def _override_note(study: Study, docs: list[str]) -> str:
    bits = []
    for d in docs:
        for o in study.doc(d).get("overrides") or []:
            if o in docs:
                bits.append(f"Where {study.doc(d)['title']} ({d}) changes a step of {study.doc(o)['title']} ({o}), "
                            f"{d} governs: include both and set note on the {o} excerpt to \"replaced by {d}: ...\".")
    return " ".join(bits)


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _block(sec: dict, titles: dict[str, str]) -> str:
    return f"[{sec['id']}] {titles.get(sec['doc_id'], sec['doc_id'])}: {sec['number']} {sec['title']}\n{sec['text']}"


def _locate(excerpt: str, text: str) -> tuple[int, int] | None:
    return textutil.find_quote(excerpt, text) or ingest.repair_quote(excerpt, text)


def _verify(groups: list[dict], by_id: dict[str, dict], spans: dict[str, list], dropped: list) -> list[dict]:
    """Excerpts replaced by the exact source text; ones not found in their (or any given) section are dropped."""
    out = []
    for g in groups:
        items = []
        for it in g["items"]:
            src = by_id.get(it["source"].strip("[] "))
            cands = [src] if src else []
            cands += [s for s in by_id.values() if s is not src]
            hit = None
            for s in cands:
                span = _locate(it["excerpt"], s["text"])
                if span:
                    hit = (s, span)
                    break
            if not hit:
                dropped.append(it)
                continue
            s, (a, b) = hit
            if any(x[0] == a and x[1] == b for x in spans.setdefault(s["id"], [])):
                continue                                    # the same excerpt twice
            spans[s["id"]].append((a, b))
            items.append(dict(it, source=s["id"], excerpt=s["text"][a:b]))
        if items:
            out.append({"heading": g["heading"], "items": items})
    return out


def _uncovered(secs: list[dict], spans: dict[str, list]) -> list[tuple[str, str]]:
    """Rule-like sentences of the sections that no excerpt overlaps by half or more. Schedule-grid rows are left
    to the visit checklists."""
    out = []
    for s in secs:
        text = s["text"]
        pos = 0
        for sent in re.split(r"(?<=[.;])\s+|\n+", text):
            start = text.find(sent, pos)
            if start < 0:
                continue
            pos = start + len(sent)
            if len(sent) < 30 or not _RULE_WORDS.search(sent) or re.match(r"^- .*: done at ", sent):
                continue
            end = start + len(sent)
            cov = sum(max(0, min(end, b) - max(start, a)) for a, b in spans.get(s["id"], []))
            if cov < 0.5 * len(sent):
                out.append((s["id"], sent))
    return out


def _add(groups: list[dict], heading: str, item: dict) -> None:
    for g in groups:
        if g["heading"].lower() == heading.lower():
            g["items"].append(item)
            return
    groups.append({"heading": heading, "items": [item]})


def fill_gaps(packs: list[dict], secs: list[dict], spans: dict[str, list], use_cache: bool = True) -> list[dict]:
    """Rule-like sentences in no pack, offered to the AI to place; the sentence itself is copied by code. Returns
    the sentences it judged not to be rules, for the report."""
    by_topic = {p["topic"]: p for p in packs}
    gaps = _uncovered(secs, spans)
    text = {s["id"]: s["text"] for s in secs}
    skipped = []
    for k in range(0, len(gaps), 80):
        batch = gaps[k:k + 80]
        heads = "\n".join(f"{p['topic']}: " + "; ".join(g["heading"] for g in p["groups"]) for p in packs)
        user = (f"PACK HEADINGS:\n{heads}\n\nSENTENCES (number, source section, sentence):\n"
                + "\n".join(f"{n}. [{sid}] {t}" for n, (sid, t) in enumerate(batch, 1)))
        res = llm.structured("ingest", GAP_SYSTEM, user, GAP_SCHEMA, "pack_gaps", use_cache=use_cache)
        placed = {r["n"]: r for r in res["sentences"] if r["packs"]}
        for n, (sid, sent) in enumerate(batch, 1):
            r = placed.get(n)
            if not r:
                skipped.append({"source": sid, "sentence": sent})
                continue
            a = text[sid].find(sent)
            spans.setdefault(sid, []).append((a, a + len(sent)))
            for t in r["packs"]:
                if t in by_topic:
                    _add(by_topic[t]["groups"], r["heading"] or "Other rules",
                         {"source": sid, "excerpt": sent, "applies_to": "", "note": ""})
    return skipped


def link(packs: list[dict], use_cache: bool = True) -> int:
    """Copy into each pack the sub-topics of other packs whose rules also apply when something in it happens (an
    overdose's reporting duty into dosing, say). The excerpts are copied by code, with their sources. Returns the
    number of links made."""
    pieces = {f"{p['topic']}:{k}": (p, g) for p in packs for k, g in enumerate(p["groups"], 1)}
    made = 0
    for p in packs:
        others = "\n".join(f"{pid} | {g['heading']} | {g['items'][0]['excerpt'][:140]}"
                           for pid, (q, g) in pieces.items() if q is not p and g["items"])
        user = (f"THIS PACK: {p['topic']} ({p['label']})\n{render(p['topic'], p['groups'])}\n\n"
                f"OTHER PACKS' SUB-TOPICS (id | heading | first rule):\n{others}")
        res = llm.structured("ingest_links", LINK_SYSTEM, user, LINK_SCHEMA, "pack_links", use_cache=use_cache,
                             model=llm.model_for("ingest"))
        linked = []
        for ln in res["links"][:8]:
            hit = pieces.get(ln["piece"].strip())
            if not hit or hit[0] is p:
                continue
            q, g = hit
            linked.append({"heading": f"Also applies, from {q['label']}: {g['heading']} ({ln['when']})",
                           "items": [dict(it) for it in g["items"]], "linked_from": ln["piece"]})
        p["groups"] = [g for g in p["groups"] if not g.get("linked_from")]
        p["links"] = linked
        made += len(linked)
    for p in packs:
        p["groups"] = p["groups"] + p.pop("links")
    return made


def _batches(secs: list[dict], limit: int = 24000) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for s in secs:
        if cur and size + len(s["text"]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s["text"])
    return out + ([cur] if cur else [])


def brief_lines(secs: list[dict], titles: dict[str, str], use_cache: bool = True,
                groups: list[list[dict]] | None = None) -> list[dict]:
    """Every source section's rules as short lines, each written once and tagged with every topic it matters for;
    a line tagged with two topics is how a rule reaches both (an overdose's reporting duty: dosing and safety).
    groups: the sections split for building (guide.build_groups); batches never cross a group."""
    by_id = {s["id"]: s for s in secs}

    def one(batch: list[dict]) -> list[dict]:
        user = "PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(s, titles) for s in batch)
        res = llm.structured("ingest_brief", BRIEF_SYSTEM, user, BRIEF_SCHEMA, "brief_lines", use_cache=use_cache,
                             model=llm.model_for("ingest"))
        ids = {s["id"] for s in batch}
        return [dict(ln, section=ln["section"].strip("[] "), rule=ln["rule"].strip(),
                     topics=[t for t in ln["topics"] if t in PACK_TOPICS])
                for ln in res["lines"] if ln["rule"].strip() and ln["section"].strip("[] ") in ids and ln["topics"]]

    batches = [b for g in (groups or [secs]) for b in _batches(g)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        lines = [ln for part in pool.map(one, batches) for ln in part]
    order = {s["id"]: n for n, s in enumerate(secs)}
    lines.sort(key=lambda ln: order[ln["section"]])
    for ln in lines:
        ln["doc"] = by_id[ln["section"]]["doc_id"]
    return lines


def _numbers(text: str) -> set[str]:
    """Numbers in rule text, section ids in brackets left out: a brief that loses one may have lost a limit."""
    return set(re.findall(r"\d+(?:\.\d+)?", re.sub(r"\[[^\]]*\]", " ", text)))


_UNCONFIRMED = re.compile(r"\[[^\]]*\b(?:confirm\w*|tbd|placeholder)\b[^\]]*\]", re.I)


def unconfirmed(secs: list[dict]) -> dict[str, set[str]]:
    """Per section, the numbers on its source lines marked as not yet confirmed ("[... Sponsor to confirm ...]"):
    a brief line using one of them would turn a placeholder into a rule."""
    out = {}
    for s in secs:
        nums = set().union(*(_numbers(re.sub(r"^\s*(?:-\s*)?\d+[.)]\s", "", ln))   # not a list item's own number
                             for ln in s["text"].splitlines() if _UNCONFIRMED.search(ln)))
        if nums:
            out[s["id"]] = nums
    return out


def lost_numbers(secs: list[dict], lines: list[dict]) -> dict[str, list[str]]:
    """Per section, numbers in its rule-like sentences that none of its lines keeps."""
    have: dict[str, set] = {}
    for ln in lines:
        have.setdefault(ln["section"], set()).update(_numbers(ln["rule"]))
    out = {}
    for s in secs:
        rule_text = " ".join(t for _, t in _uncovered([s], {}))
        miss = _numbers(rule_text) - have.get(s["id"], set())
        if miss:
            out[s["id"]] = sorted(miss)
    return out


def stored_lines(study: Study, versions: dict[str, str]) -> tuple[dict[str, list[dict]], set[str]]:
    """The stored briefs' lines per topic, in their order, and the documents those briefs were built from that are
    still at the same version (their lines can be kept as they are)."""
    rows = db.q("SELECT id, text, version FROM guide WHERE study_id=? AND kind='brief'", (study.study_id,))
    if not rows:
        return {}, set()
    built = dict(p.split("=", 1) for p in rows[0]["version"].split("; ") if "=" in p)
    same = {d for d, v in built.items() if versions.get(d) and versions[d] == v}
    by_topic = {}
    for r in rows:
        out = []
        for ln in r["text"].splitlines():
            m = re.match(r"^- (.*) \[([^\]]+)\]$", ln)
            if m:
                out.append({"rule": m.group(1), "section": m.group(2), "doc": m.group(2).split(":")[0]})
        by_topic[r["id"].removeprefix("brief:")] = out
    return by_topic, same


def render_brief(lines: list[dict]) -> str:
    return "\n".join(f"- {ln['rule']}" + (f" ({ln['applies_to']})" if ln.get("applies_to") else "")
                     + f" [{ln['section']}]" for ln in lines)


def render(topic: str, groups: list[dict]) -> str:
    lines = []
    for g in groups:
        lines.append(f"## {g['heading']}")
        for it in g["items"]:
            tail = "".join(f" ({k.replace('_', ' ')}: {it[k]})" for k in ("applies_to", "note") if it.get(k))
            lines.append(f"- {it['excerpt']} [{it['source']}]{tail}")
    return "\n".join(lines)


def build_one(study: Study, topic: str, secs: list[dict], titles: dict[str, str], spans: dict[str, list],
              use_cache: bool = True) -> dict:
    by_id = {s["id"]: s for s in secs}
    docs = sorted({s["doc_id"] for s in secs})
    system = PACK_SYSTEM.format(topic=topic, label=LABELS[topic], override=_override_note(study, docs))
    user = "PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(s, titles) for s in secs)
    res = llm.structured("ingest", system, user, PACK_SCHEMA, "pack", use_cache=use_cache)
    mine: dict[str, list] = {}
    dropped: list = []
    groups = _verify(res["groups"], by_id, mine, dropped)
    for sid, sp in mine.items():
        spans.setdefault(sid, []).extend(sp)
    return {"topic": topic, "label": LABELS[topic], "groups": groups, "sources": [s["id"] for s in secs],
            "dropped": [d["excerpt"] for d in dropped]}


def build(study: Study, log=print, use_cache: bool = True, reuse: bool = True) -> dict:
    """Build and store every topic pack from the study's guide sections (all documents). The brief lines of
    documents unchanged since the stored briefs are kept (reuse=False writes them all again), so adding a manual
    leaves what the checker reads from the protocol as it was."""
    docs = guide.documents(study)
    items = [i for i in guide.load(study.study_id) if i["kind"] == "section" and i["doc_id"] in docs
             and i["text"].strip() and not set(i["topics"]) <= NOT_CHECKABLE]
    if not items:
        raise RuntimeError(f"{study.study_id}: no guide sections; run `cormon guide --study {study.study_id}`")
    titles = {d: study.doc(d)["title"] for d in docs}
    versions = {r["doc_id"]: r["version"] for r in
                db.q("SELECT DISTINCT doc_id, version FROM guide WHERE study_id=? AND kind IN ('section', 'visit')",
                     (study.study_id,))}
    version = "; ".join(f"{d}={versions.get(d) or '?'}" for d in docs)
    old, same = stored_lines(study, versions) if reuse else ({}, set())
    db.ex("DELETE FROM guide WHERE study_id=? AND kind IN ('pack', 'piece', 'brief')", (study.study_id,))
    packs, spans = [], {}
    for topic in PACK_TOPICS:
        secs = [i for i in items if topic in i["topics"]]
        if secs:
            packs.append(build_one(study, topic, secs, titles, spans, use_cache))
    rule_sents = len(_uncovered(items, {}))
    first = 1 - len(_uncovered(items, spans)) / max(1, rule_sents)
    not_rules = fill_gaps(packs, items, spans, use_cache)
    links = link(packs, use_cache)
    log(f"{links} links between packs (rules from another topic that also apply)")
    groups = guide.build_groups(study, items)
    fresh = [g for g in groups if not {s["doc_id"] for s in g} <= same]
    new = brief_lines([s for g in fresh for s in g], titles, use_cache, fresh)
    kept_docs = {s["doc_id"] for g in groups if g not in fresh for s in g}
    if kept_docs:
        log(f"brief lines kept from the stored briefs for unchanged {', '.join(sorted(kept_docs))}")
    place = {s["doc_id"]: n for n, g in enumerate(groups) for s in g}
    marked = unconfirmed(items)
    placeholder = lambda ln: bool(_numbers(ln["rule"]) & marked.get(ln["section"], set()))  # noqa: E731
    by_topic = {t: sorted([ln for ln in old.get(t, []) if ln["doc"] in kept_docs and not placeholder(ln)]
                          + [ln for ln in new if t in ln["topics"] and not placeholder(ln)],
                          key=lambda ln: place[ln["doc"]])
                for t in PACK_TOPICS}
    left_out = sorted({ln["rule"] for ln in new + [x for v in old.values() for x in v] if placeholder(ln)})
    if left_out:
        log(f"brief: {len(left_out)} lines left out, their numbers are marked as not yet confirmed: "
            + " | ".join(left_out))
    lines, seen = [], set()
    for t in PACK_TOPICS:
        for ln in by_topic[t]:
            if (ln["rule"], ln["section"]) not in seen:
                seen.add((ln["rule"], ln["section"]))
                lines.append(dict(ln, topics=[u for u in PACK_TOPICS if ln in by_topic[u]]))
    lost = lost_numbers(items, lines)
    governs = " ".join(f"Where {study.doc(d)['title']} differs from {_and(ws)}, {study.doc(d)['title']} governs."
                       for d in docs if (ws := [study.doc(o)["title"] for o in study.doc(d).get("overrides") or []
                                                if o in docs]))
    rows = []
    for n, topic in enumerate(PACK_TOPICS):
        mine = by_topic[topic]
        if not mine:
            continue
        text = (governs + "\n" if governs else "") + render_brief(mine)
        rows.append(dict(id=f"brief:{topic}", kind="brief", number="", title=LABELS[topic], path="", text=text,
                         about=LABELS[topic], topics=[topic],
                         ord=4000 + n))
        log(f"brief {topic}: {len(mine)} lines, {len(text) // 4} tokens")
    all_text = render_brief(lines)
    log(f"brief: {len(lines)} lines in all, {len(all_text) // 4} tokens once each; "
        f"{sum(len(v) for v in lost.values())} numbers in {len(lost)} sections not kept in any line (in the file)")
    briefs = {"lines": lines, "numbers_not_kept": lost}
    for n, p in enumerate(packs):
        text = render(p["topic"], p["groups"])
        rows.append(dict(id=f"pack:{p['topic']}", kind="pack", number="", title=p["label"], path="", text=text,
                         about="; ".join(g["heading"] for g in p["groups"]), topics=[p["topic"]], ord=2000 + n))
        for k, g in enumerate(p["groups"], 1):
            rows.append(dict(id=f"piece:{p['topic']}:{k}", kind="piece", number="", title=f"{p['label']} > {g['heading']}",
                             path=p["label"], text=render(p["topic"], [g]), about=g["items"][0]["excerpt"][:140],
                             topics=[p["topic"]], ord=3000 + 100 * n + k))
        log(f"pack {p['topic']}: {len(p['sources'])} sections, {len(p['groups'])} pieces, "
            f"{sum(len(g['items']) for g in p['groups'])} excerpts, "
            f"{len(text) // 4} tokens, {len(p['dropped'])} excerpts not found in the source and dropped")
    guide._store(study, "packs", rows, version)
    report = {"rule_sentences": rule_sents, "links": links, "in_a_pack_first_pass": round(first, 3),
              "in_a_pack": round(1 - len(not_rules) / max(1, rule_sents), 3), "judged_not_rules": not_rules}
    out = settings.data_dir() / "guides" / f"{study.study_id}-packs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"study_id": study.study_id, "built_from": version, "coverage": report,
                               "packs": packs, "briefs": briefs}, indent=1, ensure_ascii=False))
    log(f"{len(packs)} packs, {sum(len(r['text']) for r in rows if r['kind'] == 'pack') // 4} tokens; "
        f"rule sentences in a pack: "
        f"{first:.0%} after the first pass, {report['in_a_pack']:.0%} after the gap pass "
        f"({len(not_rules)} judged not rules, listed in the file) -> {out}")
    return {"packs": len(packs), "file": str(out), "coverage": report}
