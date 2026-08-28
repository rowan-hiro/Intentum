"""Parser for the small agent-facing expression language.

Agents may write ``"quantity * unit_price"`` or
``"amount > 100 and region = 'West'"`` instead of nested JSON trees. The parser
turns such strings into the *loose* expression dicts that the resolver accepts
(``{"column": ...}``, ``{"value": ...}``, ``{"op": ..., "left": ..., "right": ...}``,
``{"function": ..., "args": [...]}``, ``{"and": [...]}`` ...). It never
produces SQL; the resolver still validates every identifier and function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidTransformError

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<ident>[^\W\d]\w*)
  | (?P<op>\|\||<=|>=|<>|!=|==|=|<|>|\+|-|\*|/|%|\(|\)|,)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "is", "null", "true", "false"}


@dataclass
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise InvalidTransformError(
                f"Unexpected character {text[pos]!r} at position {pos} in expression {text!r}.",
                field="expression",
                hint='Wrap field names that contain spaces or punctuation in double quotes, e.g. "unit price" * 2.',
            )
        kind = match.lastgroup or ""
        value = match.group(kind)
        pos = match.end()
        if kind == "ws":
            continue
        if kind == "ident" and value.lower() in _KEYWORDS:
            tokens.append(_Token("kw", value.lower(), match.start()))
        else:
            tokens.append(_Token(kind, value, match.start()))
    tokens.append(_Token("eof", "", len(text)))
    return tokens


class ExpressionParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokenize(text)
        self.index = 0

    # -- helpers ---------------------------------------------------------
    def _peek(self) -> _Token:
        return self.tokens[self.index]

    def _advance(self) -> _Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _accept(self, kind: str, text: str | None = None) -> _Token | None:
        token = self._peek()
        if token.kind == kind and (text is None or token.text == text):
            return self._advance()
        return None

    def _expect(self, kind: str, text: str | None = None) -> _Token:
        token = self._accept(kind, text)
        if token is None:
            got = self._peek()
            want = text or kind
            raise InvalidTransformError(
                f"Expected {want!r} at position {got.pos} in expression {self.text!r}, got {got.text or 'end of input'!r}.",
                field="expression",
            )
        return token

    # -- grammar ---------------------------------------------------------
    def parse(self) -> dict[str, Any]:
        expr = self._parse_or()
        if self._peek().kind != "eof":
            token = self._peek()
            raise InvalidTransformError(
                f"Unexpected token {token.text!r} at position {token.pos} in expression {self.text!r}.",
                field="expression",
            )
        return expr

    def _parse_or(self) -> dict[str, Any]:
        left = self._parse_and()
        while self._accept("kw", "or"):
            right = self._parse_and()
            left = {"or": [left, right]}
        return left

    def _parse_and(self) -> dict[str, Any]:
        left = self._parse_not()
        while self._accept("kw", "and"):
            right = self._parse_not()
            left = {"and": [left, right]}
        return left

    def _parse_not(self) -> dict[str, Any]:
        if self._accept("kw", "not"):
            return {"not": self._parse_not()}
        return self._parse_comparison()

    def _parse_comparison(self) -> dict[str, Any]:
        left = self._parse_additive()
        token = self._peek()
        if token.kind == "op" and token.text in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
            self._advance()
            op = {"==": "=", "<>": "!="}.get(token.text, token.text)
            right = self._parse_additive()
            return {"op": op, "left": left, "right": right}
        if token.kind == "kw" and token.text in ("in", "not", "is"):
            negated = False
            if token.text == "not":
                # "x not in (...)"
                self._advance()
                self._expect("kw", "in")
                negated = True
            elif token.text == "in":
                self._advance()
            else:  # is [not] null
                self._advance()
                negated = bool(self._accept("kw", "not"))
                self._expect("kw", "null")
                node: dict[str, Any] = {"function": "is_null", "args": [left]}
                return {"not": node} if negated else node
            self._expect("op", "(")
            values = [self._parse_additive()]
            while self._accept("op", ","):
                values.append(self._parse_additive())
            self._expect("op", ")")
            node = {"op": "in", "left": left, "right": values}
            return {"not": node} if negated else node
        return left

    def _parse_additive(self) -> dict[str, Any]:
        left = self._parse_multiplicative()
        while True:
            token = self._peek()
            if token.kind == "op" and token.text in ("+", "-"):
                self._advance()
                right = self._parse_multiplicative()
                left = {"op": token.text, "left": left, "right": right}
            elif token.kind == "op" and token.text == "||":
                # SQL string concatenation, expressed as the allowlisted concat function.
                self._advance()
                right = self._parse_multiplicative()
                left = {"function": "concat", "args": [left, right]}
            else:
                return left

    def _parse_multiplicative(self) -> dict[str, Any]:
        left = self._parse_unary()
        while True:
            token = self._peek()
            if token.kind == "op" and token.text in ("*", "/", "%"):
                self._advance()
                right = self._parse_unary()
                left = {"op": token.text, "left": left, "right": right}
            else:
                return left

    def _parse_unary(self) -> dict[str, Any]:
        if self._accept("op", "-"):
            operand = self._parse_unary()
            if "value" in operand and isinstance(operand["value"], (int, float)) and len(operand) == 1:
                return {"value": -operand["value"]}
            return {"neg": operand}
        return self._parse_primary()

    def _parse_primary(self) -> dict[str, Any]:
        token = self._advance()
        if token.kind == "number":
            return {"value": float(token.text) if "." in token.text else int(token.text)}
        if token.kind == "string":
            return {"value": token.text[1:-1].replace("''", "'")}
        if token.kind == "kw" and token.text in ("true", "false"):
            return {"value": token.text == "true"}
        if token.kind == "kw" and token.text == "null":
            return {"value": None}
        if token.kind == "qident":
            return {"column": token.text[1:-1].replace('""', '"')}
        if token.kind == "ident":
            if self._accept("op", "("):
                args: list[dict[str, Any]] = []
                if not self._accept("op", ")"):
                    args.append(self._parse_or())
                    while self._accept("op", ","):
                        args.append(self._parse_or())
                    self._expect("op", ")")
                return {"function": token.text.lower(), "args": args}
            return {"column": token.text}
        if token.kind == "op" and token.text == "(":
            inner = self._parse_or()
            self._expect("op", ")")
            return inner
        raise InvalidTransformError(
            f"Unexpected token {token.text or 'end of input'!r} at position {token.pos} in expression {self.text!r}.",
            field="expression",
        )


def parse_expression(text: str) -> dict[str, Any]:
    """Parse an expression string into a loose expression tree."""
    if not text or not text.strip():
        raise InvalidTransformError("Expression must not be empty.", field="expression")
    return ExpressionParser(text).parse()
