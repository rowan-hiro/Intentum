"""Teach on refusal: structured advice for the agent (MADR 0010)."""

from .advice import Advice, advise_contract, advise_empty_result, advise_error, advise_text_comparison, call

__all__ = ["Advice", "advise_contract", "advise_empty_result", "advise_error", "advise_text_comparison", "call"]
