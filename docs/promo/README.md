# Intentum promotional images

Three coordinated raster assets generated with the built-in
`image_gen.imagegen` tool on 2026-09-17. The visual direction uses a dark navy
background, teal signal paths, warm amber accents, and English labels for
project pages and technical presentations.

Each PNG is 1672 × 941 pixels (approximately 16:9), preserved at the tool's
native output resolution without resizing. All three images were visually
reviewed for lettering, layout, stage ordering, and architecture boundaries.

| Asset | Intended use | Content |
| --- | --- | --- |
| [Project hero](intentum-hero.png) | Project cover, presentation opening, announcement | Semantic intent becoming structured data operations. |
| [System architecture](intentum-architecture.png) | Architecture overview, technical introduction | Agent harness, MCP boundary, backend pipeline, and three storage components. |
| [Execution flow](intentum-execution-flow.png) | Process explanation, presentation slide | Six stages of a successful materialized transform, with refusal and compensation notes. |

## Project hero

![Intentum: Semantic intent. Deterministic data operations.](intentum-hero.png)

## System architecture

![Agent harness connects through MCP tools to the Intentum backend, which uses DuckDB, SQLite, and managed files.](intentum-architecture.png)

The six backend stages are Resolve, Canonical IR, Validate, Plan, Execute,
and Commit. Optional perception and measurement belong to the harness. The
three stores are siblings: DuckDB holds analytical tables, SQLite holds
metadata and operations, and managed files retain imported sources.

## Execution flow

![Resolve, Canonical IR, Validate, Plan, Execute, and Commit form the materialized-transform path.](intentum-execution-flow.png)

This image depicts the successful materialization path. Preview and replay
can take different paths. Refusal advice returns to the agent; the diagram
does not imply that the backend automatically executes a repair. The
compensation note refers to handled metadata-commit failures. A process
crash between the two stores can still leave an orphan table.

## Prompts and source grounding

[prompts.json](prompts.json) preserves the complete prompt submitted for each
asset, keyed by its output filename. No input image was supplied. The images
were generated as separate assets using a shared visual specification.

The technical content is grounded in repository revision `2fb73ba`:

- [Project architecture and implementation status](../../README.md#1-architecture).
- [Transform orchestration and failure compensation](../../agent_backend/core/backend.py),
  specifically `_run_transform` and `_commit_materialization`.
- [MADR 0001: two-store commit with compensation](../../.inkan/decisions/0001-two-store-commit-with-compensation-instead-of-a-single-transactional-store.md).
- [MADR 0004: general backend scope](../../.inkan/decisions/0004-intentum-is-a-general-agent-ready-backend-benchmarks-such-as-dataspace-are-validation-scenarios-not-the-product.md).
- [MADR 0007: output contracts](../../.inkan/decisions/0007-output-contracts-the-agent-declares-the-shape-of-its-deliverable-before-working-and-the-backend-holds-the-export-to-it.md).
- [MADR 0009: harness/backend boundary](../../.inkan/decisions/0009-agent-harness-as-a-sibling-package-it-drives-the-backend-only-through-the-mcp-tool-surface-and-perception-of-unstructured-sources-is-its-responsibility.md).
- [MADR 0010: structured refusal advice](../../.inkan/decisions/0010-teach-on-refusal-every-refusal-names-the-shape-the-backend-accepts-and-rewrites-the-request-when-mechanical-the-backend-owns-the-language-and-the-data-facts-the-harness-owns-pacing.md).

The cover illustration is conceptual. These assets make no performance or
benchmark claims. The backend checks operational and declared-contract
consistency; the agent remains responsible for interpreting the user's task.
