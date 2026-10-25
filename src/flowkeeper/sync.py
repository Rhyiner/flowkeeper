"""Compare Git with an n8n instance and apply the difference."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from flowkeeper.client import API_SETTINGS, N8nClient, N8nError
from flowkeeper.normalize import comparable, normalize_workflow, slugify, tag_names
from flowkeeper.store import LocalWorkflow, plan_filenames, write_if_changed

CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
REMOTE_ONLY = "remote-only"
CONFLICT = "conflict"


@dataclass
class Change:
    action: str
    name: str
    local: Optional[LocalWorkflow] = None
    remote: Optional[Dict[str, Any]] = None  # normalized
    remote_raw: Optional[Dict[str, Any]] = None
    reasons: List[str] = field(default_factory=list)
    content_changed: bool = False
    tags_changed: bool = False
    activation: Optional[str] = None  # "activate" | "deactivate" | "publish" | None
    diff: str = ""

    @property
    def remote_id(self) -> Optional[str]:
        return str(self.remote["id"]) if self.remote and self.remote.get("id") else None


def _has_unpublished_draft(raw: Dict[str, Any]) -> bool:
    """n8n 2.x keeps edits as a draft until they are published."""
    active_version = raw.get("activeVersionId")
    if active_version is None and isinstance(raw.get("activeVersion"), dict):
        active_version = raw["activeVersion"].get("versionId")
    version = raw.get("versionId")
    return bool(active_version and version and active_version != version)


def _content(wf: Dict[str, Any], settings_keys: set, keep_description: bool) -> Dict[str, Any]:
    data = comparable(wf, settings_keys=settings_keys, include_active=False)
    data.pop("tags", None)
    if not keep_description:
        data.pop("description", None)
    return data


def _node_summary(remote_nodes: Any, desired_nodes: Any) -> str:
    def by_name(nodes: Any) -> Dict[str, Any]:
        return {n.get("name"): n for n in nodes or [] if isinstance(n, dict)}

    remote, desired = by_name(remote_nodes), by_name(desired_nodes)
    parts = []
    added = [n for n in desired if n not in remote]
    removed = [n for n in remote if n not in desired]
    modified = [n for n in desired if n in remote and remote[n] != desired[n]]
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if modified:
        parts.append("modified " + ", ".join(modified))
    return ("nodes: " + "; ".join(parts)) if parts else ""


def _diff(remote: Optional[Dict[str, Any]], local: Dict[str, Any], label: str, settings_keys: set) -> str:
    def text(wf: Optional[Dict[str, Any]]) -> List[str]:
        if wf is None:
            return []
        data = comparable(wf, settings_keys=settings_keys)
        if "description" not in local:
            data.pop("description", None)
        return json.dumps(data, indent=2, ensure_ascii=False).splitlines(keepends=True)

    lines = difflib.unified_diff(text(remote), text(local), fromfile=f"n8n: {label}", tofile=f"git: {label}")
    return "".join(lines)


def fetch_remote(
    client: N8nClient,
    *,
    tags: Optional[List[str]] = None,
    active: Optional[bool] = None,
    include_archived: bool = False,
    keep_pin_data: bool = False,
) -> List[Dict[str, Any]]:
    """Return full workflow objects.

    The list endpoint leaves some fields out (``description`` on n8n 2.x,
    for one), so each workflow is then read on its own.
    """
    listed = [
        wf
        for wf in client.iter_workflows(tags=tags, active=active, exclude_pin_data=not keep_pin_data)
        if include_archived or not wf.get("isArchived")
    ]
    full = []
    for wf in listed:
        detail = client.get_workflow(str(wf["id"]))
        if detail is not None:  # deleted in the meantime
            full.append(detail)
    return full


def compute_plan(
    local_workflows: List[LocalWorkflow],
    remote_raw: List[Dict[str, Any]],
    *,
    manage_activation: bool = True,
) -> List[Change]:
    remote_by_id = {str(wf["id"]): wf for wf in remote_raw if wf.get("id") is not None}
    remote_by_name: Dict[str, List[Dict[str, Any]]] = {}
    for wf in remote_raw:
        remote_by_name.setdefault(wf.get("name", ""), []).append(wf)

    changes: List[Change] = []
    matched_ids = set()
    for local in sorted(local_workflows, key=lambda w: w.name.casefold()):
        raw = remote_by_id.get(local.id) if local.id else None
        if raw is None:
            candidates = [w for w in remote_by_name.get(local.name, []) if str(w.get("id")) not in matched_ids]
            if len(candidates) > 1:
                change = Change(CONFLICT, local.name, local)
                change.reasons.append(
                    f'{len(candidates)} workflows are named "{local.name}" in n8n; '
                    'put the right "id" in the file to say which one it is'
                )
                changes.append(change)
                continue
            raw = candidates[0] if candidates else None

        desired = normalize_workflow(local.data, keep_pin_data=True, keep_static_data=True)
        settings_keys = set((desired.get("settings") or {}).keys())
        keep_description = "description" in desired

        if raw is None:
            change = Change(CREATE, local.name, local, content_changed=True)
            change.tags_changed = bool(desired.get("tags"))
            if manage_activation and desired.get("active"):
                change.activation = "activate"
            change.diff = _diff(None, desired, local.name, settings_keys)
            changes.append(change)
            continue

        matched_ids.add(str(raw.get("id")))
        remote = normalize_workflow(raw)
        # A setting that n8n neither knows nor stores would show up as a
        # change forever: only compare settings n8n can actually hold.
        remote_settings = remote.get("settings") or {}
        settings_keys = {k for k in settings_keys if k in API_SETTINGS or k in remote_settings}
        change = Change(UNCHANGED, local.name, local, remote, raw)

        remote_content = _content(remote, settings_keys, keep_description)
        desired_content = _content(desired, settings_keys, keep_description)
        if remote_content != desired_content:
            change.content_changed = True
            for part in ("name", "description", "settings", "connections", "nodeGroups"):
                if remote_content.get(part) != desired_content.get(part):
                    change.reasons.append(f"{part} changed")
            node_summary = _node_summary(remote_content.get("nodes"), desired_content.get("nodes"))
            if node_summary:
                change.reasons.append(node_summary)
        if set(tag_names(remote.get("tags"))) != set(desired.get("tags") or []):
            change.tags_changed = True
            before = ", ".join(remote.get("tags") or []) or "(none)"
            after = ", ".join(desired.get("tags") or []) or "(none)"
            change.reasons.append(f"tags: {before} -> {after}")

        if manage_activation:
            want_active = bool(desired.get("active"))
            is_active = bool(remote.get("active"))
            if want_active and not is_active:
                change.activation = "activate"
            elif not want_active and is_active:
                change.activation = "deactivate"
            elif want_active and (change.content_changed or _has_unpublished_draft(raw)):
                change.activation = "publish"
                if not change.content_changed:
                    change.reasons.append("n8n has unpublished changes; publishing the version from Git")
            if change.activation in ("activate", "deactivate"):
                change.reasons.append(f"active: {str(is_active).lower()} -> {str(want_active).lower()}")

        if change.content_changed or change.tags_changed or change.activation:
            change.action = UPDATE
            change.diff = _diff(remote, desired, local.name, settings_keys)
        changes.append(change)

    for raw in remote_raw:
        if str(raw.get("id")) not in matched_ids:
            changes.append(Change(REMOTE_ONLY, raw.get("name", ""), None, normalize_workflow(raw), raw))
    return changes


class TagCache:
    def __init__(self, client: N8nClient):
        self.client = client
        self._ids: Optional[Dict[str, str]] = None

    def ids_for(self, names: List[str]) -> List[str]:
        if self._ids is None:
            self._ids = {t["name"]: str(t["id"]) for t in self.client.list_tags()}
        result = []
        for name in names:
            if name not in self._ids:
                created = self.client.create_tag(name)
                self._ids[name] = str(created["id"])
            result.append(self._ids[name])
        return result


@dataclass
class ApplyResult:
    change: Change
    ok: bool
    message: str
    new_id: Optional[str] = None


def apply_plan(
    client: N8nClient,
    changes: List[Change],
    *,
    version_name: Optional[str] = None,
    version_description: Optional[str] = None,
    on_result: Optional[Callable[[ApplyResult], None]] = None,
) -> List[ApplyResult]:
    tags = TagCache(client)
    results: List[ApplyResult] = []
    for change in changes:
        if change.action not in (CREATE, UPDATE):
            continue
        assert change.local is not None
        desired = normalize_workflow(change.local.data, keep_pin_data=True, keep_static_data=True)
        done: List[str] = []
        workflow_id = change.remote_id
        try:
            if change.action == CREATE:
                created = client.create_workflow(desired)
                workflow_id = str(created["id"])
                done.append(f"created (id {workflow_id})")
            elif change.content_changed:
                client.update_workflow(workflow_id, desired)
                done.append("updated")
            if change.tags_changed:
                client.set_workflow_tags(workflow_id, tags.ids_for(desired.get("tags") or []))
                done.append("tags set")
            if change.activation in ("activate", "publish"):
                client.publish(workflow_id, name=version_name, description=version_description)
                done.append("published")
            elif change.activation == "deactivate":
                client.unpublish(workflow_id)
                done.append("unpublished")
            result = ApplyResult(change, True, ", ".join(done) or "nothing to do", workflow_id if change.action == CREATE else None)
        except N8nError as exc:
            prefix = ", ".join(done) + "; then " if done else ""
            result = ApplyResult(change, False, f"{prefix}{exc}", workflow_id if change.action == CREATE else None)
        results.append(result)
        if on_result:
            on_result(result)
    return results


@dataclass
class ExportReport:
    written: List[Path] = field(default_factory=list)
    unchanged: List[Path] = field(default_factory=list)
    renamed: List[tuple] = field(default_factory=list)
    removed: List[Path] = field(default_factory=list)


def export_workflows(
    remote_raw: List[Dict[str, Any]],
    directory: Path,
    existing: List[LocalWorkflow],
    *,
    keep_pin_data: bool = False,
    keep_static_data: bool = False,
    prune: bool = False,
) -> ExportReport:
    report = ExportReport()
    directory.mkdir(parents=True, exist_ok=True)
    workflows = [
        normalize_workflow(raw, keep_pin_data=keep_pin_data, keep_static_data=keep_static_data)
        for raw in remote_raw
    ]
    filenames = plan_filenames(workflows)
    existing_by_id: Dict[str, Path] = {}
    owner_of: Dict[Path, str] = {}
    for wf in existing:
        if wf.id and wf.id not in existing_by_id:
            existing_by_id[wf.id] = wf.path
            owner_of[wf.path] = wf.id

    # Files written by hand and deployed before n8n gave them an id are
    # recognised by name, so the export updates them instead of duplicating them.
    without_id: Dict[str, List[Path]] = {}
    for wf in existing:
        if not wf.id and wf.name:
            without_id.setdefault(wf.name, []).append(wf.path)

    for wf in workflows:
        wf_id = str(wf["id"])
        old_path = existing_by_id.get(wf_id)
        if old_path is None and len(without_id.get(wf["name"], [])) == 1:
            old_path = without_id.pop(wf["name"])[0]
            owner_of[old_path] = wf_id
        # Keep files where the user put them (sub-folders); only the name follows the workflow.
        folder = old_path.parent if old_path else directory
        path = folder / filenames[wf_id]
        if owner_of.get(path, wf_id) != wf_id:
            # Another workflow's file already uses this name: never overwrite it.
            path = folder / filenames[wf_id].replace(".json", f"--{slugify(wf_id)}.json")
        changed = write_if_changed(path, wf)
        owner_of[path] = wf_id
        if old_path and old_path != path:
            if old_path.exists():
                old_path.unlink()
            report.renamed.append((old_path, path))
        elif changed:
            report.written.append(path)
        else:
            report.unchanged.append(path)

    if prune:
        exported = {str(wf["id"]) for wf in workflows}
        for wf in existing:
            if wf.id and wf.id not in exported and wf.path.exists():
                wf.path.unlink()
                report.removed.append(wf.path)
    return report
