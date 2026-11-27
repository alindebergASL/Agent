"""Bind named arithmetic inputs to numbered lines already read in the active turn."""

import csv
from bisect import bisect_left
import re
from decimal import Decimal


INPUT_PROPERTIES = {key: {"type": "string"} for key in ("name", "value", "source_url", "interpretation")}
CONDITION_PROPERTIES = {key: {"type": "string"} for key in ("applies_to", "source_url", "interpretation")}
NUMBER = re.compile(r"(?<![\w.+-])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+|(?=\.\d))(?:\.\d+)?(?:[eE][+-]?\d{1,3})?(?!\w|\.\d|[-+])")
CURRENCY = r"(?:[$€£¥₹]|USD\b|EUR\b|GBP\b|CAD\b|AUD\b|JPY\b)"


NORMALIZATIONS = (
    (re.compile(rf"\(\s*{CURRENCY}\s*(\d[\d,.]*)\s*\)", re.I), r" -\1 "),
    (re.compile(rf"([+-])\s*{CURRENCY}\s*(?=[\d.])", re.I), r" \1"),
    (re.compile(r"(?<!\w)([+-])\s+(?=[\d.])"), r"\1"),
)


def numeric_matches(cell):
    """Yield (normalized literal, start, end) in the original cell's character coordinates."""
    text = cell.replace("−", "-")
    spans = None
    for pattern, replacement in NORMALIZATIONS:
        pieces, origins = [], []
        cursor = 0
        for match in pattern.finditer(text):
            first, last = match.span()
            literal = match.expand(replacement)
            pieces.extend((text[cursor:first], literal))
            if spans is None:
                origins.extend((i, i + 1) for i in range(cursor, first))
                source_span = (first, last)
            else:
                origins.extend(spans[cursor:first])
                source_span = (spans[first][0], spans[last - 1][1])
            origins.extend([source_span] * len(literal))
            cursor = last
        if pieces:
            text = "".join([*pieces, text[cursor:]])
            origins.extend(spans[cursor:] if spans is not None else
                           ((i, i + 1) for i in range(cursor, len(cell))))
            spans = origins
    for match in NUMBER.finditer(text):
        first, last = match.span()
        left = first - 1
        while left >= 0 and text[left].isspace():
            left -= 1
        if left >= 0 and text[left] == "(":
            right = last
            while right < len(text) and text[right].isspace():
                right += 1
            if right < len(text) and text[right] == ")":
                continue
        if spans is not None:
            first, last = spans[first][0], spans[last - 1][1]
        yield match.group().replace(",", ""), first, last


def numeric_values(cell):
    return (Decimal(literal) for literal, _, _ in numeric_matches(cell))


def range_values(filename, line, start, end):
    """Verify complete literals inside the returned excerpt, using full field context at its edges."""
    if start == end or not any(ch.isdecimal() for ch in line[start:end]):
        return []
    fields = [(line, start, end)]
    if filename.lower().endswith(".csv"):
        # Locate field boundaries with CSV's opening-quote and doubled-quote rules.
        fields = []
        pos = 0
        while pos < len(line) and pos < end:
            field_start = pos
            if line[pos] == '"':
                pos += 1
                while pos < len(line):
                    quote = line.find('"', pos)
                    if quote < 0:
                        pos = len(line)
                        break
                    pos = quote + 1
                    if pos >= len(line) or line[pos] != '"':
                        break
                    pos += 1
            comma = line.find(",", pos)
            field_end = len(line) if comma < 0 else comma
            pos = field_end + 1
            if field_end <= start:
                continue
            cell = line[field_start:field_end]
            lo, hi = max(start - field_start, 0), min(end, field_end) - field_start
            if cell.startswith('"'):
                # On quoted fields, map raw offsets to positions after unescaping.
                offsets = []
                i = 1
                while i < len(cell):
                    if cell[i] == '"':
                        i += 1
                        if i == len(cell) or cell[i] != '"':
                            offsets.extend(range(i, len(cell)))
                            break
                    offsets.append(i)
                    i += 1
                cell = "".join(cell[index] for index in offsets)
                lo, hi = bisect_left(offsets, lo), bisect_left(offsets, hi)
            fields.append((cell, lo, hi))
    elif filename.lower().endswith((".xlsx", ".docx")):
        columns = line.split("\t", 2)
        if len(columns) == 3:
            if columns[1] not in ("number", "text"):
                return []
            offset = len(line) - len(columns[2])
            fields = [(columns[2], max(0, start - offset), end - offset)]
    return list(dict.fromkeys(literal for field, lo, hi in fields
                              for literal, first, last in numeric_matches(field)
                              if first >= lo and last <= hi and len(literal) <= 64))


def calculate_with_evidence(expression, inputs, receipts, calculate, conditions=()):
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 16:
        raise ValueError("Bind 1 to 16 named inputs to numbered source lines already read.")
    values, bindings = {}, []
    for item in inputs:
        if not isinstance(item, dict) or item.keys() != INPUT_PROPERTIES.keys() or not all(
            isinstance(value, str) for value in item.values()
        ):
            raise ValueError("Each input needs a name, numeric value, source_url and interpretation.")
        name, literal, url = item["name"], item["value"], item["source_url"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", name) or name in values:
            raise ValueError("Use distinct variable names starting with a letter.")
        if len(literal) > 64 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d{1,3})?", literal):
            raise ValueError("Provide the numeric source value as a decimal string.")
        if not 1 <= len(item["interpretation"].strip()) <= 500:
            raise ValueError("Explain what this value means, including any operative condition.")
        if url not in receipts:
            raise ValueError("Read the exact source line first, then use its URL with a #L line anchor.")
        filename, line = receipts[url]
        value = Decimal(literal)
        if isinstance(line, list):
            for segment in line:
                if any(value == Decimal(candidate) for candidate in segment["numeric_values"]):
                    bindings.append({**item, "source_text": segment["text"],
                                     "start_char": segment["start_char"], "end_char": segment["end_char"]})
                    values[name] = value
                    break
            else:
                raise ValueError(f"Read a range containing the complete numeric value for {name}.")
            continue
        # CSV delimiters can look like thousands separators. Parse columns before matching.
        cells = next(csv.reader([line]), []) if filename.lower().endswith(".csv") else [line]
        if filename.lower().endswith((".xlsx", ".docx")) and "\t" in line:
            fields = line.split("\t", 2)
            if len(fields) == 3:
                cells = [fields[2]] if fields[1] in ("number", "text") else []
        if not any(value in numeric_values(cell) for cell in cells):
            raise ValueError(f"The cited line does not contain the value given for {name}.")
        values[name] = value
        bindings.append({**item, "source_text": line})
    if not isinstance(conditions, (list, tuple)) or len(conditions) > 16:
        raise ValueError("Supply up to 16 governing passages.")
    governing_passages = []
    for condition in conditions:
        if (not isinstance(condition, dict) or condition.keys() != CONDITION_PROPERTIES.keys()
                or not all(isinstance(value, str) for value in condition.values())
                or condition["applies_to"] not in values or condition["source_url"] not in receipts
                or not 1 <= len(condition["interpretation"].strip()) <= 500):
            raise ValueError("Tie each condition to a bound input and a previously read passage, and explain it.")
        passage = receipts[condition["source_url"]][1]
        if isinstance(passage, list):
            # A condition has no numeric operand to identify its passage. Keep
            # the excerpts and their positions distinct rather than joining gaps.
            governing_passages.append({**condition, "source_ranges": [
                (segment["start_char"], segment["end_char"]) for segment in passage
            ]})
        else:
            governing_passages.append({**condition, "source_text": passage})
    return {"expression": expression, "result": calculate(expression, values),
            "inputs": bindings, "conditions": governing_passages}
