# Design: how study documents are read, organized and kept up to date

For Eliot's message of 21 Oct 2026 and the coverage report of the same day. Status: proposal; the parts marked
**built** are in the code now.

## 1. What we want

1. **Contradictions are kept, not settled.** When study documents say different things, no document wins. The bot
   shows both sides, says they differ, and recommends getting the answer from the sponsor.
2. **Nothing is lost for lack of a topic.** Ingestion reports content that fits no topic. The topic list (the
   schema) has versions and can grow, including for rules Huma adds itself.
3. **Changes are deterministic and minimal.** Adding, changing or removing a document changes only what that
   document says. A brief no document touches stays byte-for-byte the same. A brief that is touched changes only as
   much as it must ("evolving consensus"). A lab manual cannot change how enrollment questions are answered, or how
   lab rules taken from other documents are read.
4. **Decisions and principles persist.** Decisions made at sponsor, study or site level (or by Huma) can be entered
   and reviewed, survive edits, and are flagged by the AI when the documents they rest on change.

## 2. Where we are today

- Each document is split into **sections** (`guide` rows, kind `section`), each tagged with topics from a fixed
  list of 16 (`guide.TOPICS`). Each document has a version (its content hash).
- **Brief lines** are written from the sections: one short rule per line, ending with its source section id. The
  per-topic briefs (`brief:<topic>`) that the checker reads are those lines grouped by topic.
- Documents unchanged since the last build keep their brief lines exactly (`packs.stored_lines`). The protocol
  documents are written together, and each manual is written on its own, so adding a manual leaves the protocol's
  lines as they were. This was measured when the four manuals were added: the protocol side was unchanged.
- Countable rules (time windows, orders, counts, lab values) go to a **check list** that code checks.
- **Built now:**
  - The briefs no longer say "Where Regimen G differs from the Master Protocol, Regimen G governs." The pack
    prompt no longer marks a step "replaced by". The checker's instructions say no document outranks another.
  - **Time windows in code.** When documents give the same window differently, code now tells the cases apart:
    - an exact copy, or a side one copy leaves unstated: one rule;
    - a lost "±" from scanning ("112 ± 7" read as "112 - 7"): set aside, with the reason recorded;
    - anything else is a **real disagreement**: code checks the widest window, and a date inside it but outside one
      document's window is reported as *not sure*, quoting every document's window, with "ask the sponsor".
  - Today this finds 2 real disagreements, both inside the Regimen G appendix:
    - the Follow-Up Safety Call: "28 + 3 days" (section 6.8) against "28 ± 3 days" (6.10.12 and the table);
    - the visits after ATE Week 52: "84 days (± 14 days)" in the table against "96 days ±14 days" in footnote 8.
      Section 6.10.11 says "91 days ± 14 days" but is named as another event, so it is not yet grouped with these
      two (see section 9, open questions).
  - When the checker reports that passages disagree, the confirm step now reads every side's section, not only the
    first.
  - **Coverage gate** (`cormon coverage`, run after every build, shown in `cormon status`, written to the audit
    log). See section 5.

What today's model does *not* give us: there is no record of which lines came from which build. The protocol's
lines are rewritten together when any protocol document changes. Topics are fixed in code. Disagreements other
than time windows are only caught if the AI notices them while reading an email. There is no register for
decisions.

## 3. The model

Five layers. Each is built only from the one below it, stored with its inputs' hashes, and rebuilt only when those
hashes change.

```
L0  documents        versioned files, each with a level: sponsor | study | site | huma
L1  sections         per document; each with a content hash
L2  mini-briefs      per document: the rule lines of its sections, each line with a stable id
L3  topic schema     versioned list of topics; tagging of lines to topics
L4  synthesis        code only: briefs per topic = lines tagged with it, plus relations between lines
                     (same rule, disagreement, amendment) and decisions
```

### L0 Documents

- **Built:** `study.yaml documents`, each with id, kind, title, file and version hash.
- **New:**
  - `level` (sponsor, study, site or huma) and, for site documents, the sites they apply to.
  - A document's new version replaces its old one. That is a revision, not a disagreement.
  - Every past version is kept, so an email can still be explained against the version in force when it was sent.
- **Huma's own rules** are a document of kind `huma_rules`, level `huma`: a short YAML or Markdown file under
  version control, read like any other.

### L1 Sections

- **Built:** split per document; ids come from section numbers or headings.
- **New:** each section has a hash of its normalized text. A section that is renumbered or moved but has the same
  text keeps its lines: it is matched by hash, not by id.

### L2 Mini-briefs, one per document

- A **line** is `{line_id, doc, section, section_hash, rule, applies_to, schema_version, built_at}`.
  - `line_id` is a hash of (doc, section hash, rule text). It stays the same as long as the line does.
- A document's mini-brief is written from **that document's sections only**. (Today the protocol documents are
  written together; they become separate mini-briefs.) Cross-document context comes only in the synthesis step.
- **Rebuild unit: the section, not the document.** On a new version, only sections whose hash changed are
  rewritten. Lines of unchanged sections are kept exactly.
- **Evolving consensus inside a changed section:** the AI gets the section's old lines and its new text. It keeps
  each old line that still holds, word for word. It changes only lines whose rule changed, and adds or removes lines
  as needed. Code then checks that every kept line is unchanged and that every number in the section is in some
  line (`packs.lost_numbers`, built). The change is written to a review file: kept, changed, added, removed.
- Stored as `guide` rows of kind `mini_brief` (id `brief:<doc>`), and as a JSON file per document under
  `data/guides/`. This is the "shorter, parsable form" Eliot asked for earlier.

### L3 Topic schema, versioned

- The topic list moves from code into `config/topics.yaml`. Each topic has an id, a label, a one-line description
  and a version, and the file has a schema version.
- There is an `other` topic. A line that fits no topic is tagged `other`, so it is never dropped. The coverage gate
  lists `other` lines as a warning, as a sign the schema may need a new topic.
- **Tagging is a separate step from writing.** A line's text never depends on the schema. When the schema changes
  (a topic is added or split), only the tags are recomputed: lines keep their text and ids. The diff report shows
  which lines moved between topics.
- Huma, a sponsor or a study can add topics. Adding a topic is a schema version bump, recorded in the audit log.

### L4 Synthesis (code, no AI)

- `brief:<topic>` is the union of every document's lines tagged with that topic, in a fixed order (document order,
  then section order). It is plain code, so it is deterministic: the same inputs always give the same brief.
- **Relations between lines** come from a cross-document pass, stored with the two line ids they relate. Because
  they are keyed by line ids, they are recomputed only for lines that are new or changed. There are three kinds:
  - `same`: two documents state the same rule. Both lines are kept, and the brief may show one line with both
    sources.
  - `disagree`: they give different values or steps for the same thing. Shown together as *"The documents differ:
    [A] says …; [B] says …. Ask the sponsor which applies."* (The time-window part is built, in code.)
  - `amends`: one document's own words change the other ("at Baseline and Week 24 *rather than* Week 16"). This is
    not a dispute: the amending line is followed, both are shown, and the amendment is quoted. Only the document's
    own words can create this relation, never a ranking of documents.
- **Decisions** (section 7) are added as lines with their own provenance.

## 4. CRUD rules and what they guarantee

| Change | What is rebuilt | What must not change (tested) |
|---|---|---|
| Add a document | its sections and mini-brief; tags for its lines; relations between its lines and others | every other document's lines; any brief none of its lines is tagged with |
| Change a document | only sections whose hash changed; their lines through evolving consensus; relations of changed lines | lines of unchanged sections; briefs those lines are in, unless a changed line is tagged there too |
| Remove a document | nothing is written; its lines, relations and disputes disappear | everything else; decisions resting on it are flagged for review |
| Change the schema | tags only | every line's text and id |
| Add or change a decision | the decision line; the review flag | everything else |

**How we prove it** (tests and a build report, like the measurement done for the manuals):
- Rebuilding twice with no change gives zero difference.
- Adding an unrelated document changes only that document's lines, plus relations that name it.
- Every build writes a **change report**: per brief, the lines kept, added, removed and changed, with the reason
  (document X version Y, section Z). The report is reviewed before deploying.
- AI answers are cached by their exact input, so an unchanged input always gets the same answer. A forced fresh run
  goes through the same change report.

## 5. Coverage

**Built (the gate):**
- It checks that the documents the checker reads match the study's document list, and that every document has
  sections, brief lines and checks.
- It lists sections with no topic.
- It finds every rule-like sentence (background and admin sections included) whose words are in no brief line.
  Then the AI reads each of those sentences with the six most similar brief lines and says one of three things:
  - *covered* (in other words);
  - *missing*, saying what the lines lack;
  - *not a site rule* (rationale, statistics, a pointer such as "details will be in the RSA", or a sponsor or
    committee duty).
- *Missing* sentences are the warnings. Everything is saved as evidence in `data/guides/<study>-coverage.json`.

**Next:**
- Run the gate per document as part of building its mini-brief. Its *missing* sentences go back to the writer once,
  then to a person.
- Lines tagged `other` are listed as possible schema gaps.
- Sentences marked "Sponsor to confirm" or "placeholder" are kept out of the rules (built, `packs.unconfirmed`)
  and listed as open questions for the sponsor.

## 6. Contradictions

- **Finding them:**
  - Numbers in code: time windows (built), then lab limits, doses and counts in the same way.
  - Everything else by the AI, in the synthesis pass: for each new or changed line, compare it with lines on the
    same topic from other documents (and other sections of the same document; Regimen G contradicts itself).
- **Telling false disagreements apart:**
  - scan damage (built for "±");
  - an exact copy;
  - one side left unstated;
  - a newer version of the same document;
  - an explicit amendment.
  Each is recorded with its reason, never silently dropped.
- **Storing them:** `{dispute_id, line_a, line_b, kind, found_by, status: open | answered, answer_decision_id}`.
  When the sponsor answers, the answer becomes a decision (section 7) and the dispute is closed. Both lines stay;
  the decision line says which applies and who said so.
- **At run time** (the code-window part is built):
  - A message that fits one side but not the other gets a *not sure* finding: both quotes, "ask the sponsor which
    applies".
  - A message outside both sides is a clear finding.
  - Answers to questions present both sides.
- **Level-aware:** a site document that differs from the protocol is shown only for that site. A Huma rule that
  differs from a sponsor document is shown as Huma's.

## 7. Decisions and principles

- **A record:** `{id, level: sponsor | study | site | huma, scope (topics, documents or sections), text,
  made_by, date, basis (the line ids and document versions it rests on), status: active | needs_review |
  retired}`.
- **Entering them:** `cormon decide add …` (and later from the chat, by the CRA or ClinOps lead), with a reason.
  Each one is recorded in the audit log.
- **Reviewing them:** `cormon decide list` and a weekly digest section.
- **They persist across edits** because they are stored apart from the documents. In the synthesis step they are
  added as lines: *"Decision (study level, 21 Oct 2026, John Huma): …"*.
- **When circumstances change:** after every build, each decision whose basis lines changed or disappeared, or whose
  topics gained new lines, goes to the AI. It gets the decision, the old basis and the new lines, and is asked: "Does
  this decision still hold, or have circumstances changed? Say what changed." The decision is marked
  `needs_review`, with that note, for a person. It is **never changed automatically**.
- **Precedence:** a decision does not silently beat a document. Where they differ, both are shown, unless the
  decision is the sponsor's answer to that exact dispute.

## 8. Corner cases

Eliot's cases first, then others.

| Case | What happens | Covered by |
|---|---|---|
| A lab manual is added; enrollment answers must not change | The manual's lines are its own mini-brief. The eligibility brief changes only if a manual line is tagged eligibility, and the change report shows that line. | L2, L4, change report |
| A lab manual is added; lab rules from the protocol must not be re-read | Protocol lines are never rewritten by another document's build. Only relation markers (same, disagree) can be added, and they are visible. | L2, L4 |
| A document is removed | Its lines and disputes go. Decisions resting on it are flagged. | section 4 |
| One section of the protocol is revised | Only that section is rewritten, through evolving consensus. | L1 hash, L2 |
| A section is renumbered but has the same text | Matched by hash; its lines are kept. | L1 |
| Two documents say the same thing | Both kept, related as `same`. | L4 |
| Two documents disagree | Both shown, *not sure*, ask the sponsor. | section 6; windows built |
| A document explicitly amends another ("rather than Week 16") | Followed, with both shown; not a dispute. | L4 `amends` |
| A document contradicts itself (Regimen G: 84 against 96 days) | The same dispute path. | built for windows |
| A scanned page loses "±" and looks like a conflict | Recorded as scan damage, with evidence. | built |
| A new version of the same document | A revision, not a dispute; old version kept for older emails. | L0 |
| A site SOP differs from the protocol | Shown only for that site. | section 6 |
| A Huma rule differs from the sponsor's | Both shown; the Huma rule is labelled as Huma's. | section 6 |
| A new kind of document whose content fits no topic | Tagged `other`, listed by the gate; the schema can gain a topic. | L3, section 5 |
| The schema gains a topic | Tags only; line text and ids unchanged. | L3 |
| A number marked "Sponsor to confirm" | Not a rule; listed as an open question. | built |
| The AI gives a different answer on a forced rerun | Cached by input; a rerun goes through the change report. | section 4 |
| A decision's basis changes | Flagged `needs_review` with the AI's note. | section 7 |
| A sponsor decision and a site decision differ | Both shown, unless the sponsor answered that exact dispute. | section 7 |
| An email sent before a new version came into force | Judged against the version in force at the time. | L0 (open question) |
| The same event named two ways in the check list (6.10.11's "next ATE visit") | The windows are not grouped, so the disagreement is missed by code. | open question |
| A rule split across two sections (a list and its lead-in) | Written as one line citing both sections (the pack prompt already keeps the lead-in). | L2 |

## 9. Open questions for Eliot

1. Should an explicit amendment ("rather than", "instead of") be followed, or also be shown as "ask the sponsor"?
   The current checker follows it and quotes both.
2. Should emails be judged against the document version in force on the email's date, or always the newest?
3. Should a decision ever outrank a document without a sponsor answer, for example a Huma principle such as "always
   report X"?
4. Event names in the check list are merged by the AI. Should merging two names (such as "next ATE visit" and "next
   ATE visit after Week 52") need a person's approval? It decides which windows are compared.

## 10. Measured on a practice copy (22 Oct 2026)

- **Rebuild:**
  - Sections, visit checklists and vocabulary: unchanged.
  - Every brief: lost only its "… governs" line.
  - Check list: no limit that code checks changed; only the new "stated" and "disputed" fields were added.
- **Manuals set (40 emails):**
  - Old code: 25 of 40 right, 16 of 18 problems caught.
  - New code: 25 of 40 right, 15 of 18 caught.
  - The emails that changed have nothing to do with disagreements; they are the usual run-to-run variation of
    the AI.
- **Six older sets (26 emails):** the same problems were found. A few changed their problem type or gained or lost
  a second type, which is the same variation.
- **Disagreement emails:**
  - A safety call on Day 26 (inside "28 ± 3", outside "28 + 3"): *not sure, the documents disagree*, with both
    sides.
  - Day 28 (inside both): no alert.
  - Day 35 (outside both): a clear "late" alert.
  - A visit 100 days after ATE Week 52 (inside "96 ± 14", outside "84 ± 14"): reported only as late against
    "84 ± 14", which is the open event-naming question 4. The fact reader called the start of the gap "the Week 52
    visit", not "the previous ATE visit", so code did not compare the windows. The AI read only the table.
- **Coverage gate:** 891 rule-like sentences, 248 not plainly in a brief line. Of those, 63 are covered in other
  words, 172 are not site rules and 13 are missing. Examples of missing ones: completing the EDC adverse-event form
  to alert the safety gateway, marking a temperature excursion on receipt in IRT, and "dark ink is required".

## 11. Plan

1. **Done:** no ranking of documents; time-window disagreements kept and shown as *not sure*; both sides read in
   the confirm step; coverage gate; tests.
2. Mini-briefs per document, with section-hash reuse and the change report. The synthesis keeps today's
   `brief:<topic>` format, so the checker does not change.
3. Topic schema in `config/topics.yaml` with versions and an `other` topic; tagging as its own step.
4. The cross-document relation pass (same, disagree, amends) for non-window rules. The briefs then render disputes
   together.
5. The decisions register and its review pass.
6. For each step: rebuild twice (zero difference), add an unrelated document (difference limited to it), and run the
   test sets before and after.
