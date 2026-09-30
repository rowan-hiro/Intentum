# 19. Units in agent_backend/core are compared as declared and followed through transforms; a conflict in an addition, subtraction or comparison is reported on the successful response, never refused or converted

Date: 2026-09-30

## Status

Accepted

## Context and Problem Statement

Columns can carry a declared unit (an import schema hint, update_metadata, or attach_metadata from a knowledge document), and describe_dataset shows it, but nothing used it: a derive of sales (USD) + return_rate (percent) succeeded with 105.0 and no signal, since both operands pass numeric type validation (issue 17, item 2). A materialized column kept a unit only when it traced back to one source column, so a derived margin lost its unit and the next transform over it could not be checked either. Units are free text written by people and agents: USD, usd, percent, %, yuan, ten-thousand yuan. The backend has no unit registry, and a question such as whether an amount is in yuan or in ten-thousand yuan is exactly the kind of reading it must not make for the agent (MADR 0004, 0010).

## Decision Drivers

* Report data facts the backend holds and never read the task (MADR 0010): a declared unit is such a fact, what it means is not
* Keep current behavior: a warning on the response costs nothing to a transform that meant what it did
* Keep the rule small enough to state in a few lines and predictable for an agent that reads it

## Considered Options

* Report conflicts as advice, compare units as written (chosen)
* Refuse a transform whose operands have different declared units (rejected for now: units are free text entered by agents and knowledge documents, a spelling difference would block valid work, and the issue asks that stronger semantic declarations decide separately whether conflicts become refusals)
* A unit registry with synonyms, dimensions and conversion factors (rejected: every entry is a reading of meaning the backend would then own, and a conversion inferred from a name is the silent error this check exists to surface)
* Unit algebra for multiplication and division, such as USD per pcs (deferred: no measured need, and a derived unit written by the backend would be a new spelling agents must match)

## Decision Outcome

agent_backend/core/ir/units.py follows declared units through a canonical transform, and the backend reports what it finds. Two units are the same unit when their written forms are equal after case folding and whitespace collapsing; nothing else is equated, and nothing is converted, neither currencies nor scale factors, so USD and EUR, yuan and ten-thousand yuan, and % and percent all differ. An empty unit is unknown, never dimensionless, and an unknown operand never produces a signal. A column keeps its unit through select, filter, sort, limit, rename and semi_join; a join brings each right column's own unit; sum, avg, min and max keep their field's unit and count gives none; in an expression a literal has no unit, + and - give the unit both operands agree on, abs, round, floor, ceil, negation and a cast to a number keep their operand's unit, and every other operation gives none; raw_query output columns have none. The checked operations are addition, subtraction and the comparisons, in a derive or a filter: when both operands have declared units that differ, the successful response carries unit_mismatch advice naming the step, the operands and their units, and the result has no unit. A materialized dataset stores the unit inferred for each of its columns, so later transforms over it are checked too. No transform is refused and no value changes.

## Consequences

* A conflict between two spellings of one unit is reported; the advice says to give them one spelling with update_metadata
* A derived or aggregated column carries a unit only when its inputs agree, so provenance of units stays explainable step by step
* Whether a stronger semantic declaration (item 3 of README section 7, metric definitions) should turn a conflict into a refusal is left to that work

## Decision History
