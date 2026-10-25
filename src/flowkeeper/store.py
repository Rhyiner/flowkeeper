"""Reading and writing workflow files in the Git working tree."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flowkeeper.normalize import dumps, slugify


@dataclass
class LocalWorkflow:
    path: Path
    data: Dict[str, Any]
    text: str

    @property
    def id(self) -> Optional[str]:
        value = self.data.get("id")
        return str(value) if value not in (None, "") else None

    @property
    def name(self) -> str:
        return str(self.data.get("name", ""))


@dataclass
class LoadError:
    path: Path
    message: str


def workflow_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*.json") if p.is_file())


def load_directory(directory: Path) -> Tuple[List[LocalWorkflow], List[LoadError]]:
    workflows: List[LocalWorkflow] = []
    errors: List[LoadError] = []
    for path in workflow_files(directory):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(LoadError(path, f"cannot read file: {exc}"))
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(LoadError(path, f"invalid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"))
            continue
        if not isinstance(data, dict):
            errors.append(LoadError(path, "the file must contain a JSON object (one workflow per file)"))
            continue
        workflows.append(LocalWorkflow(path, data, text))
    return workflows, errors


def plan_filenames(workflows: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Map workflow id -> file name, keeping names unique and stable.

    Workflows are named after their slug. When two workflows share a slug,
    both get their id appended so neither name depends on the other.
    """
    workflows = list(workflows)
    by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for wf in workflows:
        by_slug.setdefault(slugify(wf.get("name", "")), []).append(wf)
    names: Dict[str, str] = {}
    for slug, group in by_slug.items():
        for wf in group:
            wf_id = str(wf["id"])
            names[wf_id] = f"{slug}.json" if len(group) == 1 else f"{slug}--{slugify(wf_id)}.json"
    return names


def write_if_changed(path: Path, workflow: Dict[str, Any]) -> bool:
    text = dumps(workflow)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True
