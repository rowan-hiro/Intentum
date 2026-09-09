# 12. The output contract separates what the answer carries from what it is organized by, so a column named in an adverbial role has somewhere to go other than the payload

Date: 2026-09-02

## Status

Accepted

## Context and Problem Statement

Every DataSpace measurement since 2026-08-31 failed the same way: the values are right and in gold order, and the prediction carries one column too many. The extra column is always one the question names adverbially - the sort key of 'in treatment id order', the grain of 'the daily maximum', the join key of 'for that patient'. The backend never sees the question (MADR 0004, 0007, 0008), so it cannot correct a misreading. But declare_output had one list, columns, and it meant 'what the file carries', so a model that knew it needed a sort key had nowhere to put it but the payload. The failure is not the model failing to read; it is the grammar giving one word two jobs.

## Decision Drivers

* The backend must not read the question: it stays a general backend and the trust model has no basis for judging a reading
* Every observed failure is one shape, and that shape has a structural signature the backend can see
* A refusal the agent cannot act on teaches nothing; a grammar it cannot misuse needs no refusal

## Considered Options

* Let the backend read the question and check the declared shape against it - rejected: MADR 0004, the backend would grow benchmark-shaped, and MADR 0008 gives it no standing to overrule the agent's reading
* Say it in the framing instead - already there since 2026-08-31 ('do not include helper, grouping or identifier columns'), and the failure survived six measurements: a prompt tells the model what not to do without giving the sort key a home
* Have the export drop whatever the deliverable does not need - impossible and wrong: the backend has no gold table, and silently dropping columns is exactly what the contract exists to prevent
* Refuse a declaration that carries a column unique per row - rejected: sometimes the answer is an identifier; the fact is reported, the judgement is not
* Let organizing keys be declared without being in the dataset - rejected: then the backend can neither sort by them nor verify the grain, and the declaration stops being checkable

## Decision Outcome

A contract names two kinds of column. columns is what the answer carries, in order. order_by and the one_per keys are what it is organized by: they may name columns the answer does not carry, they are expected in the dataset at export so the backend can sort and count by them, and they are left out of the file. rows becomes required, so the grain question is answered in the declaration rather than left open. A declaration is answered with what the workspace holds under each of its names - dataset, type, role, and whether the column is unique per row, which is what tells a locator apart from a value - as facts, never as a refusal, because the backend cannot know whether a given answer is meant to be an identifier.

## Consequences

* columns has one meaning, and the model's real need for a sort or grain key is met without touching it
* The final sort-then-project step is the backend's now: it writes the carried columns in the declared order, sorted the declared way, which removes a class of CONTRACT_MISMATCH at export
* A one_per key no longer has to be a declared column; that refusal is gone
* rows is required, which changes declare_output for every caller; the refusal carries advice naming the three forms (MADR 0010)
* The effect on reading is a prediction, not a measurement: early declarations should stop carrying the sort key. It is testable on task_44 and task_329 against the recorded fresh and informed arms, and is the next outcome

## Decision History
