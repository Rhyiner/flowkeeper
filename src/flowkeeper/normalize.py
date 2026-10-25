"""Turn raw n8n workflow JSON into a stable, diff-friendly form.

n8n returns a lot of bookkeeping alongside a workflow (timestamps, version
ids, sharing info, execution counters...). None of it describes what the
workflow *does*, and all of it changes constantly, which would make every
export look like a change in Git. This module keeps only the fields that
define a workflow and writes them in a canonical order.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

# Top-level fields written to disk, in this order.
TOP_LEVEL_ORDER = (
    "id",
    "name",
    "description",
    "active",
    "tags",
    "settings",
    "nodes",
    "connections",
    "nodeGroups",
    "pinData",
    "staticData",
)

# Node fields that come first, for readability. Everything else follows
# in alphabetical order.
NODE_KEY_ORDER = (
    "id",
    "name",
    "type",
    "typeVersion",
    "position",
    "disabled",
    "notes",
    "parameters",
    "credentials",
)


def canonical(value: Any) -> Any:
    """Return a copy of ``value`` with every dict key sorted, recursively."""
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def _ordered(data: Dict[str, Any], first: tuple) -> Dict[str, Any]:
    result = {key: data[key] for key in first if key in data}
    for key in sorted(data):
        if key not in result:
            result[key] = data[key]
    return result


def _normalize_node(node: Dict[str, Any]) -> Dict[str, Any]:
    node = canonical(node)
    return _ordered(node, NODE_KEY_ORDER)


def tag_names(tags: Any) -> List[str]:
    """Accept tags as returned by the API (dicts) or as stored on disk (strings)."""
    names = []
    for tag in tags or []:
        if isinstance(tag, dict):
            name = tag.get("name")
        else:
            name = tag
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return sorted(set(names), key=str.casefold)


def _non_empty(value: Any) -> bool:
    return value not in (None, {}, [], "")


def normalize_workflow(
    raw: Dict[str, Any],
    *,
    keep_pin_data: bool = False,
    keep_static_data: bool = False,
) -> Dict[str, Any]:
    """Return the canonical, on-disk representation of a workflow."""
    raw = copy.deepcopy(raw)
    wf: Dict[str, Any] = {}

    if raw.get("id") not in (None, ""):
        wf["id"] = str(raw["id"])
    wf["name"] = raw.get("name", "")
    if isinstance(raw.get("description"), str) and raw["description"].strip():
        wf["description"] = raw["description"]
    # n8n 2.x deprecates "active" in favour of "activeVersionId" (null when unpublished).
    if "activeVersionId" in raw:
        wf["active"] = raw.get("activeVersionId") is not None
    else:
        wf["active"] = bool(raw.get("active", False))
    wf["tags"] = tag_names(raw.get("tags"))
    wf["settings"] = canonical(raw.get("settings") or {})

    nodes = raw.get("nodes") or []
    if isinstance(nodes, list):
        normalized_nodes = [_normalize_node(n) if isinstance(n, dict) else n for n in nodes]
        normalized_nodes.sort(key=lambda n: str(n.get("name", "")) if isinstance(n, dict) else "")
        wf["nodes"] = normalized_nodes
    else:
        wf["nodes"] = nodes
    wf["connections"] = canonical(raw.get("connections") or {})
    if isinstance(raw.get("nodeGroups"), list) and raw["nodeGroups"]:
        # Canvas frames (n8n 2.x). Sorted by name, like nodes.
        groups = [canonical(g) for g in raw["nodeGroups"]]
        groups.sort(key=lambda g: (str(g.get("name", "")), str(g.get("id", ""))) if isinstance(g, dict) else ("", ""))
        wf["nodeGroups"] = groups

    if keep_pin_data and _non_empty(raw.get("pinData")):
        wf["pinData"] = canonical(raw["pinData"])
    if keep_static_data and _non_empty(raw.get("staticData")):
        wf["staticData"] = canonical(raw["staticData"])

    return _ordered(wf, TOP_LEVEL_ORDER)


def dumps(workflow: Dict[str, Any]) -> str:
    """Serialize a normalized workflow exactly as it is stored in Git."""
    ordered = _ordered(workflow, TOP_LEVEL_ORDER)
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


def slugify(name: str, fallback: str = "workflow") -> str:
    """Make a portable file name out of a workflow name."""
    text = unicodedata.normalize("NFKD", name or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80].rstrip("-") or fallback


def comparable(
    workflow: Dict[str, Any],
    *,
    settings_keys: Optional[set] = None,
    include_active: bool = True,
) -> Dict[str, Any]:
    """Strip what must not count as a difference between Git and n8n.

    * ``id`` differs between instances and is used for matching, not content.
    * ``settings`` are compared only on the keys Git defines (``settings_keys``):
      n8n fills in defaults for keys a file does not mention.
    """
    wf = {k: v for k, v in workflow.items() if k not in ("id", "pinData", "staticData")}
    if not include_active:
        wf.pop("active", None)
    if settings_keys is not None:
        settings = wf.get("settings") or {}
        wf["settings"] = {k: settings[k] for k in sorted(settings) if k in settings_keys}
    return wf
