"""
Shared deterministic helpers for the corpus-cleanup skill.
No third-party dependencies, no network, no LLM calls. Python 3.9+ stdlib only.
"""
import re
import json
import xml.etree.ElementTree as ET
from pathlib import Path


# ----------------------------------------------------------------------------
# Path grouping: cluster files into "source groups" by structure, generically.
# A source group = files that share an extension AND a directory-path shape
# (digits collapsed to '#'), e.g. all of house/####_#thQuarter_XML/*.xml.
# ----------------------------------------------------------------------------
_DIGITS = re.compile(r"\d+")


def path_template(path: Path, root: Path) -> str:
    """Relative path with runs of digits collapsed to '#', for structural clustering."""
    rel = path.relative_to(root)
    parts = []
    for p in rel.parts[:-1]:  # directories
        parts.append(_DIGITS.sub("#", p))
    stem_ext = rel.parts[-1]
    stem_ext = _DIGITS.sub("#", stem_ext)
    parts.append(stem_ext)
    return "/".join(parts)


def group_key(path: Path, root: Path) -> str:
    return f"{path.suffix.lower()}::{path_template(path, root)}"


# ----------------------------------------------------------------------------
# Record extraction per container type. Each yields (record_dict, locator_str).
# The locator uniquely points back to the source for provenance.
# ----------------------------------------------------------------------------
def iter_jsonl(path: Path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line), f"line={i}"
            except json.JSONDecodeError:
                continue


def iter_json(path: Path):
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, list):
        for i, rec in enumerate(data):
            yield rec, f"index={i}"
    elif isinstance(data, dict):
        # If dict wraps exactly one big list, treat that list as the records.
        list_vals = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if len(list_vals) == 1 and len(list_vals[0][1]) > 1:
            k, lst = list_vals[0]
            for i, rec in enumerate(lst):
                yield rec, f"{k}[{i}]"
        else:
            yield data, "root"
    else:
        yield {"_value": data}, "root"


def xml_to_dict(elem):
    """Recursive ElementTree -> nested dict/list. Repeated tags become lists.
    Leaf text is stripped; whitespace-only becomes ''. Attributes prefixed '@'."""
    children = list(elem)
    node = {}
    for k, v in elem.attrib.items():
        node[f"@{k}"] = v
    if not children:
        text = (elem.text or "").strip()
        if not node:
            return text
        node["#text"] = text
        return node
    for child in children:
        tag = child.tag
        val = xml_to_dict(child)
        if tag in node:
            if not isinstance(node[tag], list):
                node[tag] = [node[tag]]
            node[tag].append(val)
        else:
            node[tag] = val
    return node


def iter_xml(path: Path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return
    yield {root.tag: xml_to_dict(root), "_root_tag": root.tag}, "file"


def iter_csv(path: Path):
    import csv
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            yield dict(row), f"row={i}"


def record_iterator(path: Path):
    ext = path.suffix.lower()
    if ext == ".jsonl":
        return iter_jsonl(path)
    if ext == ".json":
        return iter_json(path)
    if ext == ".xml":
        return iter_xml(path)
    if ext == ".csv":
        return iter_csv(path)
    return iter([])


# ----------------------------------------------------------------------------
# Flatten a nested record into dotted field paths. Lists collapse to 'path[]'
# and we descend into their element dicts so nested arrays still yield paths.
# ----------------------------------------------------------------------------
def flatten(rec, prefix="", out=None, max_list=5):
    if out is None:
        out = {}
    if isinstance(rec, dict):
        for k, v in rec.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            flatten(v, key, out, max_list)
    elif isinstance(rec, list):
        key = f"{prefix}[]"
        out.setdefault(key, []).append(len(rec))
        for v in rec[:max_list]:
            flatten(v, key, out, max_list)
    else:
        out.setdefault(prefix, []).append(rec)
    return out


# ----------------------------------------------------------------------------
# Normalization used for entity resolution + money comparison.
# ----------------------------------------------------------------------------
_NAME_STRIP = re.compile(r"[^a-z0-9]+")
_LEGAL_SUFFIX = re.compile(
    r"\b(l\.?l\.?c|l\.?l\.?p|inc|incorporated|corp|corporation|co|company|ltd|"
    r"limited|lp|pllc|pc|group|the)\b",
    re.IGNORECASE,
)


def norm_name(s):
    """Aggressive normalization for exact-dedupe of entity names."""
    if s is None:
        return ""
    s = str(s).lower()
    s = _LEGAL_SUFFIX.sub(" ", s)
    s = _NAME_STRIP.sub("", s)
    return s


def norm_name_light(s):
    """Light normalization: case + punctuation only, keeps legal suffix. For display-safe dedupe."""
    if s is None:
        return ""
    return _NAME_STRIP.sub("", str(s).lower())


def norm_money(v):
    """Parse messy money value (string '12750.00', '', float, None, '$1,000') to float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None
