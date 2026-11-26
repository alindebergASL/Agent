"""Bind named arithmetic inputs to numbered lines already read in the active turn."""

import csv
import re
from decimal import Decimal


INPUT_PROPERTIES = {key: {"type": "string"} for key in ("name", "value", "source_url", "interpretation")}
CONDITION_PROPERTIES = {key: {"type": "string"} for key in ("applies_to", "source_url", "interpretation")}
NUMBER = re.compile(r"(?<![\w.+-])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+|(?=\.\d))(?:\.\d+)?(?:[eE][+-]?\d{1,3})?(?!\w|\.\d|[-+])")
CURRENCY = r"(?:[$€£¥₹]|USD\b|EUR\b|GBP\b|CAD\b|AUD\b|JPY\b)"


def numeric_values(cell):
    text = cell.replace("−", "-")
    # Preserve the sign in common financial notation before removing currency markers.
    text = re.sub(rf"\(\s*{CURRENCY}\s*(\d[\d,.]*)\s*\)", r" -\1 ", text, flags=re.I)
    text = re.sub(rf"([+-])\s*{CURRENCY}\s*(?=[\d.])", r" \1", text, flags=re.I)
    text = re.sub(r"(?<!\w)([+-])\s+(?=[\d.])", r"\1", text)
    for match in NUMBER.finditer(text):
        # A bare parenthesized amount can be either accounting notation or prose.
        if text[:match.start()].rstrip().endswith("(") and text[match.end():].lstrip().startswith(")"):
            continue
        yield Decimal(match.group().replace(",", ""))


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
        # CSV delimiters can look like thousands separators. Parse columns before matching.
        cells = next(csv.reader([line]), []) if filename.lower().endswith(".csv") else [line]
        if filename.lower().endswith((".xlsx", ".docx")) and "\t" in line:
            fields = line.split("\t", 2)
            if len(fields) == 3:
                cells = [fields[2]] if fields[1] in ("number", "text") else []
        value = Decimal(literal)
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
        governing_passages.append({**condition, "source_text": receipts[condition["source_url"]][1]})
    return {"expression": expression, "result": calculate(expression, values),
            "inputs": bindings, "conditions": governing_passages}
