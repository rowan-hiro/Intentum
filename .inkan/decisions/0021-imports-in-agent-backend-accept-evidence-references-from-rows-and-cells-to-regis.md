# 21. Imports in agent_backend accept evidence references from rows and cells to registered artifacts; the backend checks what it holds facts for, records the rest as given, and carries the association to later datasets by column only

Date: 2026-09-30

## Status

Accepted

## Context and Problem Statement

import_dataset(rows=...) keeps the rows an agent read from a document, an image or a video as a content-addressed JSON artifact, and get_provenance names that source as rows written inline with no inputs and no upstream. Where each value came from, a page of a report, a passage of a text, a moment of a video, lived only in the dataset description or outside the backend (issue 17, item 4). The backend does not read PDFs or media and must not interpret them (MADR 0009): perception belongs to the harness. What the backend does hold are artifact records (path, kind, content hash), the imported rows and columns, the text of markdown and text documents it has registered, and the lineage and canonical IR of every later operation. The trust model (MADR 0008) accepts the agent's fresh output as given and checks what it re-enters against records.

## Decision Drivers

* Keep perception and interpretation outside the backend (MADR 0009): the backend links, it never reads a PDF or a frame
* Check what the backend holds facts for, record the rest as given, and say which is which (MADR 0008)
* Retain associations only where the backend can say they still hold, with no hidden columns in the data

## Considered Options

* References on import, checked where possible, column-level carry-over through column ids (chosen)
* Free-form metadata in the dataset description (rejected: unchecked, unstructured, and lost to every derived dataset)
* A hidden row-identity column carried through every transform for cell-level lineage (deferred: it changes every schema, every compiled step and every export, and aggregates and joins would still need rules for merged and repeated rows)
* A separate attach_evidence tool for existing datasets (deferred: attaching at import keeps the reference inside the operation that entered the values, and the first cut needs only that)
* Verifying pages, PDF text or media times in the backend (rejected: that is document and media reading, the harness's job)

## Decision Outcome

import_dataset, with rows or a path, takes an optional evidence list. Each reference names an artifact by id, name or file path (a file that is not registered yet is registered with its content hash, as attach_metadata does), and optionally content_hash, rows (0-based positions of the imported rows, in the order the source is read: rows as written, file order, a SQLite table's own order), columns, page (1 or more), span ([start, end) character offsets in the artifact's text), quote, time_s (seconds, or [start, end]) and note. No rows means every row; no columns means every column. Validation happens before any table is created where it can and right after the load for row positions, and refuses with advice: a content_hash that differs from the registered one, a row outside the import, an unknown column, a page below 1 or on audio or video, a time on anything but audio, video or an unknown kind, a malformed span or time, an unknown key. For a markdown or text artifact whose file still has its registered hash, the backend checks a span against the text length and a quote against the text at the span with whitespace collapsed, and locates a quote given without a span when it occurs exactly once. Everything else (a page, a time, a span or quote in a PDF, a file that changed since it was registered) is recorded as given. Every stored reference lists what was checked and what was recorded as given; neither means the agent read the evidence correctly. References are stored in an evidence_refs table, one row per reference as given, with the artifact's hash at the time, the canonical locator and a snapshot of the referenced cells' values; they are written in the import's metadata transaction, so a failed commit leaves none (MADR 0001), and they are part of the import's canonical IR and idempotency fingerprint when present. They belong to the dataset version the import created. get_provenance returns them. For a materialized dataset it returns the references of every upstream dataset: a reference reaches a column of the new dataset when that column carries a referenced column unchanged, which the canonical IR records as a column id through select, filter, sort, limit, rename, join, semi_join and group_by keys, followed through every materialization in between; any other reference reaches it through lineage only. Row positions are never carried past the import: a filtered, sorted or joined dataset does not say which of its rows a reference covers, and a derived column, a measure and a raw_query output carry no reference.

## Consequences

* import_dataset grows one optional argument and the metadata store one table; an import without evidence is unchanged, including its replay fingerprint
* Storage is linear in the references and the cells they name; nothing is stored per unreferenced row
* A question such as which rows of a filtered result a reference covers cannot be answered yet; a row-identity mechanism would be a new decision
* The harness can attach its recorded observations (frame ids, segment ids, timestamps) as references when it imports what it read; that change belongs to agent_harness

## Decision History
