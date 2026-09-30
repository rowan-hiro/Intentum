"""Evidence references from imported rows and cells to registered artifacts (MADR 0021)."""

from .spec import EVIDENCE_HINT, EvidenceSpec, TextCheck, check_kind, check_text, parse_evidence

__all__ = ["EVIDENCE_HINT", "EvidenceSpec", "TextCheck", "check_kind", "check_text", "parse_evidence"]
