"""Deterministic credential redaction for diagnostic copies, never business data."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


REDACTED = "[REDACTED]"


def _field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def credential_exception_key(value: Any) -> str:
    """Exact JSON comparison keeps booleans distinct from numeric values."""
    def require_json(item):
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                require_json(child)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise TypeError("credential exception keys must be JSON strings")
                require_json(child)
            return
        raise TypeError("credential exception must be a JSON value")

    require_json(value)
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _assignment_value(raw: str) -> Any:
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return raw[1:-1]
    return raw[1:-1] if raw.startswith("'") and raw.endswith("'") else raw


def json_bytes(value: Any) -> bytes:
    """Canonical bytes used only for diagnostic sizes and integrity hashes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _labeled_json(line: str):
    """Recognize a whole JSON value after one label, not arbitrary prose spans."""
    prefix, separator, body = line.partition(':')
    if (not separator or not prefix.strip() or any(char in prefix for char in '\"{}[]')
            or any(char.isspace() for char in prefix.strip())
            or not body.lstrip().startswith(('{', '['))):
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return (prefix, body, parsed) if isinstance(parsed, (dict, list)) else None


class _Sanitizer:
    def __init__(self, policy: dict, known_values=()) -> None:
        self.max_depth = policy["maxDepth"]
        self.fields = {_field_name(name) for name in policy["credentialFields"]}
        self.string_fields = {_field_name(name) for name in policy["stringCredentialFields"]}
        self.all_fields = self.fields | self.string_fields
        self.value_exceptions = {
            _field_name(field): {credential_exception_key(value) for value in values}
            for field, values in policy["credentialValueExceptions"].items()
        }
        self.patterns = [
            (re.compile(item["pattern"]), item["replacement"])
            for item in policy["credentialTextPatterns"]
        ]
        alternatives = "|".join(
            "[-_ ]*".join(re.escape(char) for char in name)
            for name in sorted(self.all_fields, key=len, reverse=True)
        )
        # The field list controls both structured fields and text assignments.
        self.assignment = re.compile(
            r"(?i)(?<![\w])(?P<prefix>[\"']?(?P<field>" + alternatives
            + r")[\"']?\s*[:=]\s*)(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,;}&<>\]\)]+)"
        ) if alternatives else None
        self.redactions = 0
        self.issues: set[str] = set()
        self.ancestors: set[int] = set()
        # Only values identified under credential fields/flags in this call.
        # Word boundaries avoid changing substrings of ordinary prose or IDs.
        self.known_values = [re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)")
                             for value in sorted(set(known_values), key=len, reverse=True)
                             if isinstance(value, str) and value and value != REDACTED]

    def omitted(self, reason: str) -> dict:
        self.issues.add(reason)
        return {"$omitted": reason}

    def value_exception(self, field: str, value: Any) -> bool:
        allowed = self.value_exceptions.get(_field_name(field))
        if not allowed:
            return False
        try:
            return credential_exception_key(value) in allowed
        except (TypeError, ValueError, OverflowError, RecursionError):
            return False

    def text(self, value: str, depth: int, *, use_known: bool = True) -> str:
        # Tool responses commonly contain JSON as a text block. Parsing only
        # strings which can contain objects avoids treating every prose word
        # as a second serialization layer. Keep the original when unchanged.
        stripped = value.strip()
        if stripped.startswith(("{", "[")) and depth < self.max_depth:
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                before = self.redactions, set(self.issues)
                cleaned = self.walk(parsed, depth + 1)
                if before != (self.redactions, self.issues):
                    return json_bytes(cleaned).decode("utf-8")
                # Container strings have already been visited leaf by leaf.
                # A header regex over the serialized container could consume
                # its closing quotes and unrelated fields on the same line.
                return value
        # Model reports also embed whole JSON values after a short label.
        # Keep their field types and public-value exceptions meaningful; a
        # credential-looking key inside one is not a plain HTTP header.
        segments, plain = [], []
        for line in value.splitlines(keepends=True):
            fragment = _labeled_json(line) if depth < self.max_depth else None
            if fragment is None:
                plain.append(line)
                continue
            prefix, body, parsed = fragment
            segments.append(self._plain_text(''.join(plain), use_known=use_known))
            plain.clear()
            before = self.redactions, set(self.issues)
            if self.value_exception(prefix, parsed):
                cleaned = parsed
            elif _field_name(prefix) in self.fields:
                cleaned = REDACTED
                self.redactions += 1
            else:
                cleaned = self.walk(parsed, depth + 1)
            if before != (self.redactions, self.issues):
                ending = body[len(body.rstrip('\r\n')):]
                body = json_bytes(cleaned).decode('utf-8') + ending
            segments.append(self._plain_text(prefix + ':', use_known=use_known) + body)
        segments.append(self._plain_text(''.join(plain), use_known=use_known))
        return ''.join(segments)

    def _plain_text(self, value: str, *, use_known: bool = True) -> str:
        # Columnar model text separates field names from values. Replace the
        # values already identified in this call before assignment/header
        # patterns can split a JSON container or swallow unrelated row cells.
        for pattern in self.known_values if use_known else ():
            def replace_known(match: re.Match) -> str:
                self.redactions += 1
                return REDACTED
            value = pattern.sub(replace_known, value)
        for pattern, replacement in self.patterns:
            def replace(match: re.Match, template: str = replacement) -> str:
                assignment = self.assignment.match(match.group(0)) if self.assignment else None
                if assignment is not None:
                    # Header patterns consume a whole value, including any
                    # scheme. Only an exact public value is exempt: a suffix
                    # such as the `read` in `Bearer read` cannot qualify.
                    raw = match.group(0)[assignment.end("prefix"):]
                    if self.value_exception(assignment.group("field"), _assignment_value(raw)):
                        return match.group(0)
                result = match.expand(template)
                if result != match.group(0):
                    self.redactions += 1
                return result
            value = pattern.sub(replace, value)
        if self.assignment is not None:
            def replace_assignment(match: re.Match) -> str:
                original = match.group("value")
                if self.value_exception(match.group("field"), _assignment_value(original)):
                    return match.group(0)
                quote = original[0] if original.startswith(("\"", "'")) else ""
                replacement = quote + REDACTED + quote
                if original != replacement:
                    self.redactions += 1
                return match.group("prefix") + replacement
            value = self.assignment.sub(replace_assignment, value)
        return value

    def walk(self, value: Any, depth: int = 0) -> Any:
        if depth > self.max_depth:
            return self.omitted("max_depth")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else self.omitted("non_finite_number")
        if isinstance(value, str):
            return self.text(value, depth)
        if isinstance(value, Path):
            return self.text(str(value), depth)
        if not isinstance(value, (dict, list, tuple)):
            # Do not call repr/str on foreign objects: that may execute code
            # or reveal credentials before redaction has had a chance to run.
            return self.omitted("unsupported_type")
        identity = id(value)
        if identity in self.ancestors:
            return self.omitted("cycle")
        self.ancestors.add(identity)
        try:
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        return self.omitted("non_string_key")
                    cleaned_key = self.text(key, depth + 1, use_known=False)
                    if cleaned_key in result:
                        return self.omitted("redacted_key_collision")
                    if self.value_exception(key, item):
                        # Decode the admitted public JSON value into a private
                        # copy; do not retain aliases to business input objects.
                        result[cleaned_key] = json.loads(credential_exception_key(item))
                    elif (_field_name(key) in self.fields
                            or (_field_name(key) in self.string_fields and isinstance(item, str))):
                        result[cleaned_key] = REDACTED
                        if not (isinstance(item, str) and item == REDACTED):
                            self.redactions += 1
                    else:
                        result[cleaned_key] = self.walk(item, depth + 1)
                return result
            result = []
            credential_flag = None
            for item in value:
                if credential_flag is not None:
                    if self.value_exception(credential_flag, item):
                        result.append(json.loads(credential_exception_key(item)))
                    else:
                        result.append(REDACTED)
                        if not (isinstance(item, str) and item == REDACTED):
                            self.redactions += 1
                    credential_flag = None
                    continue
                result.append(self.walk(item, depth + 1))
                if isinstance(item, str) and item.startswith("--") and "=" not in item:
                    field = _field_name(item[2:])
                    credential_flag = field if field in self.all_fields else None
            return result
        finally:
            self.ancestors.remove(identity)


def requires_credential_labels(rows: list[dict], policy: dict) -> bool:
    """Keep non-string credentials keyed; global number/bool replacement is unsafe."""
    rules = _Sanitizer(policy)
    return any(isinstance(key, str) and not isinstance(value, str) and _field_name(key) in rules.fields
               and not rules.value_exception(key, value)
               for row in rows for key, value in row.items())


def credential_values(value: Any, policy: dict) -> set[str]:
    """Identify explicit credential values in memory for this call's errors.

    The returned set must never be persisted. It lets an argument parser echo
    a previously identified value without its field label without leaking it
    into the diagnostic copy. It never reads files or discovers prose secrets.
    """
    rules = _Sanitizer(policy)
    found, ancestors = set(), set()

    def secret_parts(item, depth, *, container=True):
        if depth > rules.max_depth:
            return
        if isinstance(item, str) and item and item != REDACTED:
            found.add(item)
            # A rendered JSON cell keeps escapes, unlike its structured twin.
            found.add(json.dumps(item, ensure_ascii=False)[1:-1])
            return
        if not isinstance(item, (dict, list, tuple)) or id(item) in ancestors:
            return
        if container and item:
            try:
                found.add(json_bytes(item).decode('utf-8'))
            except (TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
                pass
        ancestors.add(id(item))
        try:
            children = item.values() if isinstance(item, dict) else item
            for child in children:
                # A number/bool alone is not a global secret. The exact outer
                # container covers these cells without changing prose counts.
                secret_parts(child, depth + 1, container=False)
        finally:
            ancestors.remove(id(item))

    def add(field, item, depth):
        if not rules.value_exception(field, item):
            secret_parts(item, depth)

    def visit(item, depth):
        if depth > rules.max_depth:
            return
        if isinstance(item, str):
            if item.strip().startswith(("{", "[")):
                try:
                    parsed = json.loads(item)
                except (ValueError, RecursionError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    visit(parsed, depth + 1)
                    return
            spans, offset = [], 0
            for line in item.splitlines(keepends=True):
                fragment = _labeled_json(line) if depth < rules.max_depth else None
                if fragment is not None:
                    prefix, _, parsed = fragment
                    visit({prefix: parsed} if _field_name(prefix) in rules.all_fields else parsed, depth + 1)
                    spans.append((offset, offset + len(line)))
                offset += len(line)
            if rules.assignment:
                for match in rules.assignment.finditer(item):
                    if not any(start <= match.start() < end for start, end in spans):
                        add(match.group("field"), _assignment_value(match.group("value")), depth)
            return
        if not isinstance(item, (dict, list, tuple)) or id(item) in ancestors:
            return
        ancestors.add(id(item))
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(key, str) and rules.value_exception(key, child):
                        continue
                    if (isinstance(key, str) and (_field_name(key) in rules.fields
                            or (_field_name(key) in rules.string_fields and isinstance(child, str)))):
                        add(key, child, depth + 1)
                    else:
                        visit(child, depth + 1)
            else:
                previous = None
                for child in item:
                    if isinstance(previous, str) and previous.startswith("--") and "=" not in previous and _field_name(previous[2:]) in rules.all_fields:
                        add(previous[2:], child, depth + 1)
                    visit(child, depth + 1)
                    previous = child
        finally:
            ancestors.remove(id(item))

    visit(value, 0)
    return found


def sanitize(value: Any, policy: dict, *, known_values=()) -> dict:
    """Return a JSON-safe redacted copy, with explicit capture limitations.

    ``originalBytes`` is the UTF-8 size of the original JSON representation,
    not a model token count. It is unknown for non-JSON inputs. Known secrets
    are hidden before any caller receives serializable diagnostic content.
    """
    try:
        original_bytes = len(json_bytes(value))
    except (TypeError, ValueError, RecursionError, UnicodeError, OverflowError):
        original_bytes = None
    sanitizer = _Sanitizer(policy, known_values)
    cleaned = sanitizer.walk(value)
    try:
        json_bytes(cleaned)
    except (UnicodeError, OverflowError, RecursionError):
        cleaned = sanitizer.omitted("unserializable_text")
    return {
        "value": cleaned,
        "originalBytes": original_bytes,
        "redactions": sanitizer.redactions,
        "complete": not sanitizer.issues,
        "issues": sorted(sanitizer.issues),
    }
