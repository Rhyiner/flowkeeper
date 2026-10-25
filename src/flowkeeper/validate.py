"""Offline checks on workflow files, meant to run in CI before anything is deployed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from flowkeeper.client import API_SETTINGS
from flowkeeper.normalize import dumps, normalize_workflow
from flowkeeper.store import LoadError, LocalWorkflow

ERROR = "error"
WARNING = "warning"

# Values that are almost certainly real credentials. Found in a workflow
# file, they would be pushed to Git in clear text.
SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("API key (sk-...)", re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_\-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}=*")),
]
SECRET_NAME = re.compile(
    r"(?i)(passw(or)?d|secret|token|api[_-]?key|apikey|authorization|private[_-]?key|access[_-]?key)"
)

TRIGGER_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.cron",
    "n8n-nodes-base.interval",
    "n8n-nodes-base.start",
}


@dataclass
class Issue:
    level: str
    path: Path
    message: str
    node: Optional[str] = None

    def format(self, root: Optional[Path] = None) -> str:
        path = self.path
        if root is not None:
            try:
                path = self.path.relative_to(root)
            except ValueError:
                pass
        where = f"{path}"
        if self.node:
            where += f" [{self.node}]"
        return f"{self.level}: {where}: {self.message}"


def is_expression(value: str) -> bool:
    return value.startswith("=") or "{{" in value


def is_trigger(node_type: str) -> bool:
    lowered = node_type.lower()
    return node_type in TRIGGER_TYPES or lowered.endswith("trigger")


def _walk(value: Any, path: str = "") -> Iterator[Tuple[str, Any, Any]]:
    """Yield (path, key, value) for every leaf, plus the dicts themselves."""
    if isinstance(value, dict):
        yield path, None, value
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    else:
        yield path, path.rsplit(".", 1)[-1], value


def scan_secrets(parameters: Any) -> List[Tuple[str, str, str]]:
    """Return (level, parameter path, description) for suspicious values."""
    findings: List[Tuple[str, str, str]] = []
    for path, key, value in _walk(parameters):
        if isinstance(value, dict):
            # Header/query parameter lists: {"name": "Authorization", "value": "Bearer ..."}
            name, val = value.get("name"), value.get("value")
            if (
                isinstance(name, str)
                and SECRET_NAME.search(name)
                and isinstance(val, str)
                and len(val) >= 8
                and not is_expression(val)
                and not any(p.search(val) for _, p in SECRET_PATTERNS)
            ):
                findings.append((WARNING, f"{path}.value", f'"{name}" looks like a hard-coded secret'))
            continue
        if not isinstance(value, str) or not value or is_expression(value):
            continue
        matched = next((label for label, pattern in SECRET_PATTERNS if pattern.search(value)), None)
        if matched:
            findings.append((ERROR, path, f"contains what looks like a {matched}"))
        elif key and SECRET_NAME.search(str(key)) and len(value) >= 8:
            findings.append((WARNING, path, "looks like a hard-coded secret"))
    return findings


def validate_workflow(wf: LocalWorkflow, *, scan: bool = True) -> List[Issue]:
    issues: List[Issue] = []
    data = wf.data

    def add(level: str, message: str, node: Optional[str] = None) -> None:
        issues.append(Issue(level, wf.path, message, node))

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        add(ERROR, '"name" is missing or empty')
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        add(ERROR, '"nodes" must be a list')
        nodes = []
    connections = data.get("connections")
    if not isinstance(connections, dict):
        add(ERROR, '"connections" must be an object')
        connections = {}
    if "settings" in data and not isinstance(data["settings"], dict):
        add(ERROR, '"settings" must be an object')
    elif isinstance(data.get("settings"), dict):
        unknown = sorted(k for k in data["settings"] if k not in API_SETTINGS)
        if unknown:
            add(WARNING, f"unknown setting(s) {', '.join(unknown)}: n8n may ignore them")
    if "active" in data and not isinstance(data["active"], bool):
        add(ERROR, '"active" must be true or false')
    if "tags" in data and not (
        isinstance(data["tags"], list) and all(isinstance(t, (str, dict)) for t in data["tags"])
    ):
        add(ERROR, '"tags" must be a list of names')

    names = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            add(ERROR, f"nodes[{index}] is not an object")
            continue
        node_name = node.get("name")
        label = node_name if isinstance(node_name, str) else f"nodes[{index}]"
        if not isinstance(node_name, str) or not node_name:
            add(ERROR, '"name" is missing', label)
        elif node_name in names:
            add(ERROR, "two nodes have this name; n8n requires unique node names", label)
        else:
            names.add(node_name)
        if not isinstance(node.get("type"), str) or not node.get("type"):
            add(ERROR, '"type" is missing', label)
        if not isinstance(node.get("typeVersion"), (int, float)) or isinstance(node.get("typeVersion"), bool):
            add(ERROR, '"typeVersion" must be a number', label)
        position = node.get("position")
        if not (
            isinstance(position, list)
            and len(position) == 2
            and all(isinstance(p, (int, float)) and not isinstance(p, bool) for p in position)
        ):
            add(ERROR, '"position" must be [x, y]', label)
        if "parameters" in node and not isinstance(node["parameters"], dict):
            add(ERROR, '"parameters" must be an object', label)
        if scan:
            for level, param_path, message in scan_secrets(node.get("parameters") or {}):
                add(level, f"parameters.{param_path}: {message}", label)

    for source, outputs in connections.items():
        if source not in names:
            add(ERROR, f'connection from unknown node "{source}"')
            continue
        if not isinstance(outputs, dict):
            add(ERROR, f'connections of "{source}" must be an object')
            continue
        for kind, branches in outputs.items():
            for branch in branches if isinstance(branches, list) else []:
                for target in branch if isinstance(branch, list) else []:
                    target_name = target.get("node") if isinstance(target, dict) else None
                    if target_name not in names:
                        add(ERROR, f'"{source}" ({kind}) is connected to unknown node "{target_name}"')

    node_ids = {n.get("id") for n in nodes if isinstance(n, dict) and n.get("id")}
    groups = data.get("nodeGroups")
    if groups is not None and not isinstance(groups, list):
        add(ERROR, '"nodeGroups" must be a list')
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not group.get("name"):
            add(ERROR, "a node group has no name")
            continue
        missing = [i for i in group.get("nodeIds") or [] if i not in node_ids]
        if missing:
            add(WARNING, f'node group "{group["name"]}" refers to {len(missing)} node id(s) that do not exist')

    if data.get("active") and nodes and not any(
        isinstance(n, dict) and is_trigger(str(n.get("type", ""))) and not n.get("disabled") for n in nodes
    ):
        add(WARNING, "the workflow is marked active but has no enabled trigger node; n8n will refuse to activate it")
    if data.get("pinData"):
        add(WARNING, '"pinData" is stored in Git: pinned test data may contain personal or sensitive data')

    if not any(i.level == ERROR for i in issues):
        canonical_text = dumps(normalize_workflow(data, keep_pin_data=True, keep_static_data=True))
        if canonical_text != wf.text:
            add(WARNING, "file is not in canonical form (run `flowkeeper format`)")
    return issues


def validate_all(
    workflows: List[LocalWorkflow],
    load_errors: List[LoadError],
    *,
    scan: bool = True,
) -> List[Issue]:
    issues = [Issue(ERROR, e.path, e.message) for e in load_errors]
    by_id: Dict[str, List[LocalWorkflow]] = {}
    by_name: Dict[str, List[LocalWorkflow]] = {}
    for wf in workflows:
        issues.extend(validate_workflow(wf, scan=scan))
        if wf.id:
            by_id.setdefault(wf.id, []).append(wf)
        if wf.name:
            by_name.setdefault(wf.name, []).append(wf)
    for wf_id, group in by_id.items():
        if len(group) > 1:
            others = ", ".join(str(w.path.name) for w in group)
            for wf in group:
                issues.append(Issue(ERROR, wf.path, f'id "{wf_id}" is used by several files ({others})'))
    for name, group in by_name.items():
        if len(group) > 1:
            others = ", ".join(str(w.path.name) for w in group)
            for wf in group:
                issues.append(
                    Issue(ERROR, wf.path, f'workflow name "{name}" is used by several files ({others}); names must be unique')
                )
    order = {ERROR: 0, WARNING: 1}
    issues.sort(key=lambda i: (str(i.path), order.get(i.level, 2)))
    return issues
