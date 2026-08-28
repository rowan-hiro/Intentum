"""Unicode-aware identifier rules shared by the IR, validator and resolvers.

Identifiers may be written in any script (Chinese table and column names are
common in real workspaces). A *normalized* name is casefolded with every run
of separators or punctuation collapsed to a single underscore, e.g.
``"公募基金经理(新)"`` → ``"公募基金经理_新"`` and ``"Regional Sales"`` →
``"regional_sales"``. This module has no dependencies inside the package
except the error types, so it can be imported from anywhere.
"""

from __future__ import annotations

import re

from .errors import InvalidIntentError

# Runs of letters/digits in any script (underscore is treated as a separator).
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Scripts without word boundaries: CJK ideographs, kana, hangul.
_CJK_RUN_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
_SCRIPT_SPLIT_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+"
    r"|[^぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+"
)


def normalize(text: str) -> str:
    """Casefolded snake_case form used for lenient name matching."""
    return "_".join(w.casefold() for w in _WORD_RE.findall(text))


def tokens(text: str) -> list[str]:
    """Tokens for scoring.

    Latin/digit runs become one token each. CJK runs contribute the whole run
    plus its character bigrams, so partial matches ("基金" in "基金简称") work
    without a dictionary.
    """
    out: list[str] = []
    for word in _WORD_RE.findall(text):
        for part in _SCRIPT_SPLIT_RE.findall(word.casefold()):
            if _CJK_RUN_RE.fullmatch(part):
                out.append(part)
                if len(part) > 2:
                    out.extend(part[i : i + 2] for i in range(len(part) - 1))
            else:
                out.append(part)
    return out


def is_cjk(text: str) -> bool:
    return bool(_CJK_RUN_RE.search(text))


def slugify(text: str) -> str:
    """Derive a canonical identifier. Idempotent: ``slugify(slugify(x)) == slugify(x)``."""
    slug = normalize(text)
    if not slug:
        raise InvalidIntentError(f"Cannot derive a valid name from {text!r}.", field="name")
    if slug[0].isdigit():
        slug = f"d_{slug}"
    return slug


def is_identifier(name: str) -> bool:
    """True when ``name`` is already in canonical form."""
    if not name:
        return False
    try:
        return slugify(name) == name
    except InvalidIntentError:
        return False
