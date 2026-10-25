"""Command-line interface: ``flowkeeper <command>``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from flowkeeper import __version__
from flowkeeper.client import N8nClient, N8nError
from flowkeeper.normalize import dumps, normalize_workflow
from flowkeeper.store import load_directory
from flowkeeper.sync import (
    CONFLICT,
    CREATE,
    REMOTE_ONLY,
    UNCHANGED,
    UPDATE,
    ApplyResult,
    Change,
    apply_plan,
    compute_plan,
    export_workflows,
    fetch_remote,
)
from flowkeeper.validate import ERROR, WARNING, validate_all

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES = 2
EXIT_USAGE = 64


class Style:
    def __init__(self, stream=sys.stdout):
        self.enabled = stream.isatty() and not os.environ.get("NO_COLOR")
        if os.environ.get("FORCE_COLOR"):
            self.enabled = True

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


style = Style()


def err(message: str) -> None:
    print(message, file=sys.stderr)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse plumbing
        self.print_usage(sys.stderr)
        err(f"{self.prog}: error: {message}")
        raise SystemExit(EXIT_USAGE)


# -- helpers ------------------------------------------------------------


def make_client(args: argparse.Namespace) -> N8nClient:
    return N8nClient(
        args.url,
        args.api_key,
        timeout=args.timeout,
        verify_tls=not args.insecure,
    )


def print_issues(issues, root: Path) -> None:
    for issue in issues:
        line = issue.format()
        if issue.level == ERROR:
            err(style.red(line))
        else:
            err(style.yellow(line))


def load_and_check(directory: Path, *, scan: bool, strict: bool, quiet_ok: bool = False):
    """Load the directory and run validation. Returns (workflows, ok)."""
    workflows, load_errors = load_directory(directory)
    issues = validate_all(workflows, load_errors, scan=scan)
    print_issues(issues, directory)
    errors = sum(1 for i in issues if i.level == ERROR)
    warnings = sum(1 for i in issues if i.level == WARNING)
    ok = errors == 0 and (warnings == 0 or not strict)
    if not quiet_ok or not ok:
        total = len(workflows) + len(load_errors)
        summary = f"{total} workflow file(s) checked: {errors} error(s), {warnings} warning(s)"
        if strict and warnings:
            summary += " (warnings count as errors with --strict)"
        print(style.green(summary) if ok else style.red(summary))
    return workflows, ok


SYMBOLS = {CREATE: "+", UPDATE: "~", UNCHANGED: "=", REMOTE_ONLY: "?", CONFLICT: "!"}


def print_change(change: Change, *, show_diff: bool) -> None:
    symbol = SYMBOLS.get(change.action, " ")
    label = f"{symbol} {change.name}"
    location = f"  ({change.local.path})" if change.local else ""
    if change.action == CREATE:
        print(style.green(label) + style.dim(location))
    elif change.action == UPDATE:
        print(style.yellow(label) + style.dim(location))
    elif change.action == CONFLICT:
        print(style.red(label) + style.dim(location))
    else:
        print(style.dim(label + location))
    for reason in change.reasons:
        print(f"    {reason}")
    if change.activation == "activate" and change.action == CREATE:
        print("    will be activated")
    if show_diff and change.diff:
        for line in change.diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print("    " + style.green(line))
            elif line.startswith("-") and not line.startswith("---"):
                print("    " + style.red(line))
            else:
                print("    " + style.dim(line))


def summarize(changes: List[Change]) -> str:
    count = lambda action: sum(1 for c in changes if c.action == action)  # noqa: E731
    text = f"{count(CREATE)} to create, {count(UPDATE)} to update, {count(UNCHANGED)} unchanged"
    if count(REMOTE_ONLY):
        text += f", {count(REMOTE_ONLY)} only in n8n"
    if count(CONFLICT):
        text += f", {count(CONFLICT)} in conflict"
    return text


def show_plan(changes: List[Change], args: argparse.Namespace) -> None:
    for change in changes:
        if change.action == UNCHANGED and not args.verbose:
            continue
        if change.action == REMOTE_ONLY and not args.verbose:
            continue
        print_change(change, show_diff=getattr(args, "diff", False))
    remote_only = sum(1 for c in changes if c.action == REMOTE_ONLY)
    if remote_only and not args.verbose:
        print(style.dim(f"({remote_only} workflow(s) exist only in n8n; run with -v to list them)"))


# -- commands -----------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    client = make_client(args)
    remote = fetch_remote(
        client,
        tags=args.tag or None,
        active=True if args.active_only else None,
        include_archived=args.include_archived,
        keep_pin_data=args.keep_pin_data,
    )
    existing, _ = load_directory(args.dir)
    report = export_workflows(
        remote,
        args.dir,
        existing,
        keep_pin_data=args.keep_pin_data,
        keep_static_data=args.keep_static_data,
        prune=args.prune,
    )
    for path in report.written:
        print(style.green(f"  wrote    {path}"))
    for old, new in report.renamed:
        print(style.yellow(f"  renamed  {old} -> {new}"))
    for path in report.removed:
        print(style.red(f"  removed  {path}"))
    if args.verbose:
        for path in report.unchanged:
            print(style.dim(f"  same     {path}"))
    print(
        f"{len(remote)} workflow(s) exported to {args.dir}: "
        f"{len(report.written)} written, {len(report.renamed)} renamed, "
        f"{len(report.unchanged)} unchanged, {len(report.removed)} removed"
    )
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    if not args.dir.is_dir():
        err(f"{args.dir} does not exist")
        return EXIT_ERROR
    _, ok = load_and_check(args.dir, scan=not args.no_secret_scan, strict=args.strict)
    return EXIT_OK if ok else EXIT_ERROR


def cmd_format(args: argparse.Namespace) -> int:
    workflows, load_errors = load_directory(args.dir)
    for error in load_errors:
        err(style.red(f"error: {error.path}: {error.message}"))
    changed = []
    for wf in workflows:
        text = dumps(normalize_workflow(wf.data, keep_pin_data=True, keep_static_data=True))
        if text != wf.text:
            changed.append(wf.path)
            if not args.check:
                wf.path.write_text(text, encoding="utf-8")
    verb = "would reformat" if args.check else "reformatted"
    for path in changed:
        print(f"  {verb} {path}")
    print(f"{len(changed)} file(s) {verb}, {len(workflows) - len(changed)} already canonical")
    if load_errors:
        return EXIT_ERROR
    return EXIT_ERROR if (args.check and changed) else EXIT_OK


def _plan(args: argparse.Namespace):
    workflows, ok = load_and_check(args.dir, scan=not args.no_secret_scan, strict=False, quiet_ok=True)
    if not ok:
        err(style.red("Fix the errors above first."))
        return None, None
    client = make_client(args)
    remote = fetch_remote(client)
    changes = compute_plan(workflows, remote, manage_activation=not args.no_activation)
    return client, changes


def cmd_plan(args: argparse.Namespace) -> int:
    _, changes = _plan(args)
    if changes is None:
        return EXIT_ERROR
    show_plan(changes, args)
    print(style.bold(f"Plan: {summarize(changes)}."))
    if any(c.action == CONFLICT for c in changes):
        return EXIT_ERROR
    pending = any(c.action in (CREATE, UPDATE) for c in changes)
    return EXIT_CHANGES if (pending and args.detailed_exitcode) else EXIT_OK


def cmd_deploy(args: argparse.Namespace) -> int:
    client, changes = _plan(args)
    if changes is None:
        return EXIT_ERROR
    show_plan(changes, args)
    conflicts = [c for c in changes if c.action == CONFLICT]
    if conflicts:
        err(style.red(f"{len(conflicts)} conflict(s): nothing was deployed."))
        return EXIT_ERROR
    pending = [c for c in changes if c.action in (CREATE, UPDATE)]
    if not pending:
        print(style.green("Nothing to deploy: n8n already matches Git."))
        return EXIT_OK
    if args.dry_run:
        print(style.bold(f"Dry run: {summarize(changes)}. Nothing was changed."))
        return EXIT_OK

    print(style.bold(f"Deploying: {summarize(changes)}."))

    def report(result: ApplyResult) -> None:
        line = f"  {result.change.name}: {result.message}"
        print(style.green(line) if result.ok else style.red(line))

    results = apply_plan(
        client,
        changes,
        version_name=args.version_name or None,
        version_description=args.version_description or None,
        on_result=report,
    )
    failed = [r for r in results if not r.ok]
    created = [r for r in results if r.ok and r.new_id]
    if created:
        print(
            style.dim(
                "New workflows got an id from n8n. They are matched by name until you run "
                "`flowkeeper export` and commit the ids."
            )
        )
    if failed:
        err(style.red(f"{len(failed)} of {len(results)} workflow(s) failed."))
        return EXIT_ERROR
    print(style.green(f"Done: {len(results)} workflow(s) deployed."))
    return EXIT_OK


# -- parser -------------------------------------------------------------


def default_version_name() -> str:
    """Label published versions with the commit they come from, when known."""
    explicit = os.environ.get("FLOWKEEPER_VERSION_NAME")
    if explicit:
        return explicit
    sha = os.environ.get("CI_COMMIT_SHORT_SHA") or os.environ.get("GITHUB_SHA", "")[:8]
    return f"git {sha}" if sha else ""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-d",
        "--dir",
        type=Path,
        default=Path(os.environ.get("FLOWKEEPER_DIR", "workflows")),
        help="folder holding the workflow files (env FLOWKEEPER_DIR, default: workflows)",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="also list unchanged items")

    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument(
        "--url",
        default=os.environ.get("N8N_URL", ""),
        help="n8n address, e.g. https://n8n.example.com (env N8N_URL)",
    )
    connection.add_argument(
        "--api-key",
        default=os.environ.get("N8N_API_KEY", ""),
        help="n8n API key (env N8N_API_KEY; prefer the variable, arguments show up in process lists)",
    )
    connection.add_argument("--timeout", type=float, default=30.0, help="seconds per request (default: 30)")
    connection.add_argument(
        "--insecure", action="store_true", help="do not verify the TLS certificate (self-signed HTTPS)"
    )

    scanning = argparse.ArgumentParser(add_help=False)
    scanning.add_argument("--no-secret-scan", action="store_true", help="skip the search for hard-coded secrets")

    planning = argparse.ArgumentParser(add_help=False)
    planning.add_argument("--diff", action="store_true", help="show a JSON diff for each change")
    planning.add_argument(
        "--no-activation",
        action="store_true",
        help='ignore the "active" field: never activate, deactivate or publish',
    )

    parser = Parser(
        prog="flowkeeper",
        description="GitOps for n8n: keep workflows in Git and deploy them from there.",
    )
    parser.add_argument("--version", action="version", version=f"flowkeeper {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=Parser)
    sub.required = True

    p = sub.add_parser(
        "export",
        aliases=["pull"],
        parents=[common, connection],
        help="write the workflows of an n8n instance to files",
        description="Download workflows from n8n and write one normalized JSON file per workflow.",
    )
    p.add_argument("--tag", action="append", help="only workflows with this tag (repeatable)")
    p.add_argument("--active-only", action="store_true", help="only active (published) workflows")
    p.add_argument("--include-archived", action="store_true", help="also export archived workflows")
    p.add_argument("--prune", action="store_true", help="delete files of workflows that are no longer exported")
    p.add_argument("--keep-pin-data", action="store_true", help="keep pinned test data (off by default)")
    p.add_argument("--keep-static-data", action="store_true", help="keep static data (off by default)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "validate",
        parents=[common, scanning],
        help="check workflow files offline",
        description="Check structure, connections, duplicates and hard-coded secrets. Needs no n8n access.",
    )
    p.add_argument("--strict", action="store_true", help="fail on warnings too")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser(
        "format",
        aliases=["fmt"],
        parents=[common],
        help="rewrite files in canonical form",
        description="Rewrite workflow files with sorted keys and stable ordering, for clean diffs.",
    )
    p.add_argument("--check", action="store_true", help="only report files that would change")
    p.set_defaults(func=cmd_format)

    p = sub.add_parser(
        "plan",
        aliases=["diff"],
        parents=[common, connection, scanning, planning],
        help="show what deploy would change",
        description="Compare the files with n8n and list what deploy would create or update.",
    )
    p.add_argument(
        "--detailed-exitcode",
        action="store_true",
        help="exit with 2 when there are changes (useful for drift detection)",
    )
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser(
        "deploy",
        aliases=["push"],
        parents=[common, connection, scanning, planning],
        help="create or update workflows in n8n from the files",
        description="Validate the files, then create, update, tag and (de)activate workflows to match Git.",
    )
    p.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")
    p.add_argument(
        "--version-name",
        default=default_version_name(),
        help="name given to the versions published in n8n (default: the commit in CI, "
        "e.g. 'git a1b2c3d4'; env FLOWKEEPER_VERSION_NAME)",
    )
    p.add_argument(
        "--version-description",
        default=os.environ.get("FLOWKEEPER_VERSION_DESCRIPTION") or os.environ.get("CI_COMMIT_TITLE", ""),
        help="description of the published versions (default: the commit title on GitLab CI)",
    )
    p.set_defaults(func=cmd_deploy)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except N8nError as exc:
        err(style.red(f"error: {exc}"))
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        err("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
