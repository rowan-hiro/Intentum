# 18. The output contract's spelling of a column is the header the export writes; datasets keep canonical names

Date: 2026-09-28

## Status

Accepted

## Context and Problem Statement

The transform language writes every name an agent gives (a measure alias, a select alias, a derived name, a rename target) in canonical snake_case, and the validator holds the canonical IR to it; imported columns keep their source spelling. The output contract (MADR 0007) keeps names as declared, and export_result compared them exactly: a dataset column that matched a declared name only after normalization was reported as renamed, with a repair that renames it. A rename to a name that is not snake_case is normalized back, so for a declared FirstProduct the repair renamed firstproduct to firstproduct. An agent following the advice could not produce the declared header inside the language and left it for raw_query. Seen in a DataSpace replay (task_200, a min measure aliased ChiNameAbbr) and reproduced on the sample orders data.

## Decision Drivers

* The header is part of the file handed to another system, as value rendering is (MADR 0005)
* Canonical names inside keep later references and the validator simple, and imported names already reach them through lenient matching
* A repair the agent is offered must change something when it is sent as-is (MADR 0010)

## Considered Options

* Header at export: a declared name that matches a dataset column only after normalization is a match, and the export writes the declared spelling - chosen
* Keep the case of identifier-shaped names in the IR - rejected: it relaxes an invariant held by the resolver, the validator and raw_query, and a declared name with spaces or punctuation (Total Amount) still could not be produced, so the no-op rename would remain for those
* Keep every name exactly as written - rejected: the largest change, and a name with spaces must then be quoted in every later expression, which canonical names were chosen to avoid
* Keep exact matching and only stop proposing renames that normalize back - rejected: honest, but it leaves a declared header undeliverable inside the language

## Decision Outcome

The contract's column names are the file's header. At export, each declared name, carried or organizing, stands for the dataset column of that exact name or, failing that, the one column whose normalized name is the same; that is a match, not a problem. The export reads the matched columns, orders and counts by the matched organizing columns, and writes each carried column under the declared spelling; the contract evidence records every column written under a spelling other than its own. A normalized name that fits several dataset columns remains a mismatch. Datasets and the canonical IR keep their names; without a contract the export writes the dataset's names.

## Consequences

* Good, because a declared header is deliverable without leaving the transform language, and no contract repair proposes a rename that changes nothing
* Good, because declared headers with case or spaces (FirstProduct, Total Amount) are deliverable
* Neutral, because the file's header can differ from the dataset's column names; the export response and the audit evidence show both
* Bad, because an export without a contract still writes the canonical snake_case form of names the agent wrote; the declared spelling needs a declared contract

## Decision History

### 2026-09-28T11:02:42.296Z, outcome 2026-09-28-1056-6j0s

Status: proposed -> accepted

Implemented: match_columns lets a declared name stand for the column of that exact name or the one column with the same normalized name, for carried and organizing columns; export_result projects the matched columns, orders and counts by the matched keys, and writes the declared spelling as the header in csv, formatted csv and parquet, recording written_as in the contract evidence; the renamed contract problem and its rename repair are gone

### 2026-09-28T23:53:01.585Z, outcome 2026-09-28-2352-qrwj

Status: accepted -> accepted

Renumbered from 0013 to 0018 on 2026-09-29. A repository derived from this one, which rebases onto it, already uses 0013-0017 for its own records, so after the rebase that brought this record in, two records carried 0013. 0018 is the first number neither repository uses, and it makes this repository's next record 0019. Nothing else in the record changed. Outcomes 2026-09-28-1056-6j0s and 2026-09-28-1542-dztr, sealed before the change, link this record as 0013.

### 2026-09-29T01:05:17.928Z, outcome 2026-09-29-0104-j9y0

Status: accepted -> accepted

On 2026-09-29, as a one-time exception the user approved, outcomes 2026-09-28-1056-6j0s and 2026-09-28-1542-dztr were edited by hand so that their begin event's decisions list 0018 in place of 0013, which no record carried after the renumbering. Their contract hashes were recomputed with inkan's computeContractHash, which reproduced both stored hashes before the edit: ef6130022ad4d8fe to f784919507320cf4 for 6j0s, b7bda05659ad6f95 to 470d9de1376c55b1 for dztr. Nothing else in the two logs changed; their prose still names MADR 0013 as written when they were sealed. The previous entry's statement that they link this record as 0013 no longer holds.
