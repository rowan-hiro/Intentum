"""Resolve loose dataset references to concrete datasets.

Resolution considers ids, exact names, aliases, normalized names, description
and column tokens, temporal words ("today", "yesterday"), origin words
("imported", "result"), recency words ("latest", "earlier") and optional
context hints. Material ambiguity raises ``AmbiguousReferenceError``; the
backend renders that as a ``needs_resolution`` response.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timedelta
from typing import Any, Callable

from ..errors import AmbiguousReferenceError, InvalidIntentError, NotFoundError
from ..models.entities import Dataset, DatasetStatus
from ...storage.metadata.interface import MetadataStore
from .common import ResolutionNote, normalize, tokens

STOPWORDS = {
    "the", "a", "an", "i", "we", "my", "our", "of", "that", "this", "those", "these", "one", "ones",
    "s", "which", "from", "in", "on", "at", "to", "it", "is", "was", "were", "with", "by", "for",
    "dataset", "datasets", "data", "table", "tables", "file", "files", "set",
}
RECENCY_WORDS = {"latest", "recent", "recently", "newest", "last", "earlier", "previous", "just", "now"}
IMPORT_WORDS = {"imported", "import", "loaded", "load", "uploaded", "upload", "ingested"}
DERIVED_WORDS = {"result", "results", "created", "made", "built", "derived", "computed", "generated", "materialized", "output"}
PUBLISHED_WORDS = {"published", "stable"}
TODAY_WORDS = {"today", "todays"}
YESTERDAY_WORDS = {"yesterday", "yesterdays"}
HINT_WORDS = RECENCY_WORDS | IMPORT_WORDS | DERIVED_WORDS | PUBLISHED_WORDS | TODAY_WORDS | YESTERDAY_WORDS


class DatasetResolver:
    def __init__(self, store: MetadataStore, clock: Callable[[], datetime]) -> None:
        self.store = store
        self.clock = clock

    # -- public ----------------------------------------------------------
    def resolve(
        self,
        reference: Any,
        *,
        field: str = "source",
        context: dict[str, Any] | None = None,
        notes: list[ResolutionNote] | None = None,
    ) -> Dataset:
        text = self._reference_text(reference, field)
        datasets = self.store.list_datasets(include_deleted=True)
        active = [d for d in datasets if d.status != DatasetStatus.DELETED]

        exact = self._exact_match(text, active)
        if exact is not None:
            return exact

        deleted_hit = self._exact_match(text, [d for d in datasets if d.status == DatasetStatus.DELETED])
        if deleted_hit is not None:
            raise NotFoundError(
                f"Dataset {text!r} ({deleted_hit.id}) was deleted and is not available.",
                field=field,
                details={"dataset_id": deleted_hit.id, "name": deleted_hit.name, "status": "deleted", "restorable": True},
                hint=f"Call restore_dataset with dataset={deleted_hit.id!r} to bring it back.",
            )

        scored = self._score(text, active, context or {})
        ranked = sorted(scored, key=lambda item: (-item[1], item[0].created_at, item[0].id))
        reasons = {d.id: why for d, _, why in ranked}
        ranked = [(d, s) for d, s, _ in ranked]
        positive = [(d, s) for d, s in ranked if s > 0]
        if not positive:
            raise NotFoundError(
                f"No dataset matches {text!r}.",
                field=field,
                candidates=self._suggestions(text, active),
                details={"available": [{"id": d.id, "name": d.name} for d in active[:20]]},
            )

        best, best_score = positive[0]
        contenders = [(d, s) for d, s in positive if s >= best_score - 0.5]
        if len(contenders) == 1:
            if notes is not None:
                notes.append(ResolutionNote(field, text, f"{best.name} ({best.id})", "; ".join(reasons[best.id]) or "lenient match"))
            return best

        recency = bool(set(tokens(text)) & RECENCY_WORDS) or bool((context or {}).get("prefer_recent"))
        if recency:
            newest = max(contenders, key=lambda item: (item[0].created_at, item[0].id))[0]
            if notes is not None:
                notes.append(
                    ResolutionNote(
                        field, text, f"{newest.name} ({newest.id})",
                        "; ".join(reasons[newest.id] + ["most recently created among " + ", ".join(d.name for d, _ in contenders)]),
                    )
                )
            return newest

        raise AmbiguousReferenceError(
            f"The {field} {text!r} matches multiple datasets.",
            field=field,
            candidates=[self._candidate(d) for d, _ in contenders[:8]],
            hint="Repeat the request with the dataset id or exact name.",
        )

    # -- internals -------------------------------------------------------
    @staticmethod
    def _reference_text(reference: Any, field: str) -> str:
        if isinstance(reference, str) and reference.strip():
            return reference.strip()
        if isinstance(reference, dict):
            for key in ("dataset_id", "id", "name", "dataset", "ref", "reference"):
                value = reference.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise InvalidIntentError(
            f"The {field} reference must be a dataset name, id, or a description such as 'yesterday's sales'.",
            field=field,
        )

    @staticmethod
    def _exact_match(text: str, datasets: list[Dataset]) -> Dataset | None:
        lowered = text.lower()
        for d in datasets:
            if d.id.lower() == lowered or d.name.lower() == lowered:
                return d
        for d in datasets:
            if lowered in (a.lower() for a in d.aliases):
                return d
        norm = normalize(text)
        hits = [d for d in datasets if normalize(d.name) == norm or norm in (normalize(a) for a in d.aliases)]
        return hits[0] if len(hits) == 1 else None

    def _score(self, text: str, datasets: list[Dataset], context: dict[str, Any]) -> list[tuple[Dataset, float, list[str]]]:
        words = tokens(text)
        content = [w for w in words if w not in STOPWORDS and w not in HINT_WORDS]
        hints = set(words) & HINT_WORDS
        now = self.clock()
        today = now.date()
        yesterday = today - timedelta(days=1)
        preferred = set(context.get("recent", []) or [])

        results: list[tuple[Dataset, float, list[str]]] = []
        for d in datasets:
            name_tokens = set(tokens(d.name))
            alias_tokens = set(t for a in d.aliases for t in tokens(a))
            desc_tokens = set(tokens(d.description))
            column_tokens = set(t for c in d.columns for t in tokens(c.name))
            score = 0.0
            why: list[str] = []
            for w in content:
                if w in name_tokens or w in alias_tokens:
                    score += 3
                    why.append(f"name/alias contains {w!r}")
                elif w in desc_tokens:
                    score += 1.5
                    why.append(f"description mentions {w!r}")
                elif w in column_tokens:
                    score += 1
                    why.append(f"has a column {w!r}")
                else:
                    close = difflib.get_close_matches(w, list(name_tokens | alias_tokens), n=1, cutoff=0.8)
                    if close:
                        score += 2
                        why.append(f"{w!r} is close to {close[0]!r}")
                    elif any(w in t or t in w for t in name_tokens if len(w) >= 4 and len(t) >= 4):
                        score += 1.5
                        why.append(f"{w!r} partially matches the name")
            if content and score == 0:
                results.append((d, 0.0, why))
                continue

            created_day = d.created_at.date()
            if hints & TODAY_WORDS:
                score += 2 if created_day == today else -3
                why.append("created today" if created_day == today else "not created today")
            if hints & YESTERDAY_WORDS:
                if created_day == yesterday or yesterday.isoformat().replace("-", "_") in d.name or yesterday.isoformat() in d.name:
                    score += 2
                    why.append("dated yesterday")
                else:
                    score -= 3
            if hints & IMPORT_WORDS:
                score += 1 if d.origin == "import" else -3
                if d.origin == "import":
                    why.append("was imported")
            if hints & DERIVED_WORDS:
                score += 1 if d.origin == "materialize" else -3
                if d.origin == "materialize":
                    why.append("is a materialized result")
            if hints & PUBLISHED_WORDS:
                score += 1 if d.status == DatasetStatus.PUBLISHED else -3
            if d.id in preferred:
                score += 0.4
                why.append("listed in context.recent")
            if not content:
                # Pure hint reference ("the one I imported today"): every dataset
                # passing the hint filters is a candidate.
                score += 1
            results.append((d, score, why))
        return results

    @staticmethod
    def _candidate(dataset: Dataset) -> dict[str, Any]:
        return {
            "id": dataset.id,
            "name": dataset.name,
            "description": dataset.description,
            "created_at": dataset.created_at.isoformat(),
            "origin": dataset.origin,
        }

    @staticmethod
    def _suggestions(text: str, datasets: list[Dataset]) -> list[dict[str, Any]]:
        names = {d.name: d for d in datasets}
        close = difflib.get_close_matches(normalize(text), list(names), n=5, cutoff=0.5)
        return [{"id": names[n].id, "name": n} for n in close]
