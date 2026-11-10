"""Parse the YAML subset that policy.yml needs, with the standard library only.

Supported: block mappings and sequences, one-line flow `[...]` and `{...}`,
plain, single- and double-quoted scalars, ints, floats, true/false/null, `#` comments.
Rejected with YamlError: tabs, anchors, aliases, tags, block scalars (`|`, `>`),
multi-line flow, several documents, duplicate keys. A plain scalar may not start
with `*`, `&`, `!`, `|`, `>`, `%` or `@` (quote globs such as "*.md").
"""

import json
import re


class YamlError(ValueError):
    pass


_INT = re.compile(r"-?\d+$")
_FLOAT = re.compile(r"-?\d+\.\d+$")
_RESERVED_START = set("*&!|>%@`")


def loads(text):
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        if "\t" in body[: indent + 1]:
            raise YamlError(f"line {number}: tabs are not allowed for indentation")
        if body.strip() in ("---", "..."):
            if lines:
                raise YamlError(f"line {number}: only one document is allowed")
            continue
        lines.append([indent, body.strip(), number])
    if not lines:
        raise YamlError("the file is empty")
    value, end = _block(lines, 0, lines[0][0])
    if end != len(lines):
        raise YamlError(f"line {lines[end][2]}: unexpected indentation")
    return value


def _strip_comment(line):
    """Cut a `#` comment. Quotes open a quoted value only where a value starts, as in YAML."""
    quote, escaped, depth, prev, prev_i = None, False, 0, None, -1
    first = len(line) - len(line.lstrip())
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote, prev, prev_i = None, ch, i
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        value_start = prev is None or prev == ":" or (prev == "-" and prev_i == first) or (depth and prev in "[{,")
        if ch in "\"'" and value_start:
            quote = ch
        elif ch in "[{" and value_start:
            depth += 1
        elif ch in "]}" and depth:
            depth -= 1
        if ch not in " \t":
            prev, prev_i = ch, i
    return line


def _block(lines, i, indent):
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _sequence(lines, i, indent)
    return _mapping(lines, i, indent)


def _sequence(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
        rest = lines[i][1][1:].lstrip(" ")
        if not rest:
            if i + 1 >= len(lines) or lines[i + 1][0] <= indent:
                raise YamlError(f"line {lines[i][2]}: empty list item")
            value, i = _block(lines, i + 1, lines[i + 1][0])
        elif _split_key(rest) is not None:
            lines[i] = [indent + len(lines[i][1]) - len(rest), rest, lines[i][2]]
            value, i = _mapping(lines, i, lines[i][0])
        else:
            value, i = _scalar_or_flow(rest, lines[i][2]), i + 1
        items.append(value)
    return items, i


def _mapping(lines, i, indent):
    result = {}
    while i < len(lines) and lines[i][0] == indent:
        _, text, number = lines[i]
        split = _split_key(text)
        if split is None:
            raise YamlError(f"line {number}: expected `key: value`")
        key, rest = split
        if key in result:
            raise YamlError(f"line {number}: duplicate key {key!r}")
        i += 1
        if rest:
            result[key] = _scalar_or_flow(rest, number)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
            result[key], i = _block(lines, i, lines[i][0])
        else:
            result[key] = None
    return result, i


def _split_key(text):
    """Return (key, rest) when text is `key:` or `key: rest`, else None."""
    if text[0] not in "\"'":
        match = re.match(r"([^\s:\[\]{},#][^:]*?)\s*:(\s|$)", text)
        return (match.group(1), text[match.end():].strip()) if match else None
    parser = _Flow(text, 0)
    key = parser.quoted()
    rest = text[parser.pos:]
    if not rest.startswith(":") or rest[1:2] not in ("", " "):
        return None
    return key, rest[1:].strip()


def _scalar_or_flow(text, number):
    try:
        parser = _Flow(text, number)
        value = parser.value(in_flow=False)
        if parser.pos != len(text):
            raise YamlError(f"line {number}: unexpected text after value")
        return value
    except IndexError:
        raise YamlError(f"line {number}: unterminated value") from None


def _plain(token, number):
    if token[0] in _RESERVED_START:
        raise YamlError(f"line {number}: quote values that start with {token[0]!r}")
    if token in ("true", "false"):
        return token == "true"
    if token in ("null", "~"):
        return None
    if _INT.match(token):
        return int(token)
    if _FLOAT.match(token):
        return float(token)
    return token


class _Flow:
    def __init__(self, text, number):
        self.text, self.pos, self.number = text, 0, number

    def _skip(self):
        while self.pos < len(self.text) and self.text[self.pos] == " ":
            self.pos += 1

    def value(self, in_flow=True):
        self._skip()
        ch = self.text[self.pos]
        if ch == "[":
            return self._seq()
        if ch == "{":
            return self._map()
        if ch in "\"'":
            return self.quoted()
        stop = ",]}" if in_flow else ""
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in stop:
            self.pos += 1
        token = self.text[start:self.pos].strip()
        if not token:
            raise YamlError(f"line {self.number}: missing value")
        return _plain(token, self.number)

    def quoted(self):
        quote = self.text[self.pos]
        end = self.pos + 1
        if quote == '"':
            while self.text[end] != '"':
                end += 2 if self.text[end] == "\\" else 1
            raw = self.text[self.pos:end + 1]
            try:
                value = json.loads(raw)
            except ValueError:
                raise YamlError(f"line {self.number}: bad escape in {raw}") from None
        else:
            while True:
                end = self.text.index("'", end)
                if self.text[end + 1:end + 2] != "'":
                    break
                end += 2
            value = self.text[self.pos + 1:end].replace("''", "'")
        self.pos = end + 1
        self._skip()
        return value

    def _seq(self):
        self.pos += 1
        items = []
        self._skip()
        if self.text[self.pos] == "]":
            self.pos += 1
            return items
        while True:
            items.append(self.value())
            self._skip()
            ch = self.text[self.pos]
            self.pos += 1
            if ch == "]":
                return items
            if ch != ",":
                raise YamlError(f"line {self.number}: expected `,` or `]`")

    def _map(self):
        self.pos += 1
        result = {}
        self._skip()
        if self.text[self.pos] == "}":
            self.pos += 1
            return result
        while True:
            self._skip()
            if self.text[self.pos] in "\"'":
                key = self.quoted()
            else:
                start = self.pos
                while self.text[self.pos] != ":":
                    if self.text[self.pos] in ",[]{}":
                        raise YamlError(f"line {self.number}: expected `key: value` in {{...}}")
                    self.pos += 1
                key = self.text[start:self.pos].strip()
            self._skip()
            if self.text[self.pos] != ":":
                raise YamlError(f"line {self.number}: expected `:` after {key!r}")
            self.pos += 1
            if key in result:
                raise YamlError(f"line {self.number}: duplicate key {key!r}")
            result[key] = self.value()
            self._skip()
            ch = self.text[self.pos]
            self.pos += 1
            if ch == "}":
                return result
            if ch != ",":
                raise YamlError(f"line {self.number}: expected `,` or `}}`")
