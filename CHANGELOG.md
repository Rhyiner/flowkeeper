# Changelog

## 0.1.0 — 2026-10-25

First release.

- `export`: one normalized JSON file per workflow, stable ordering, renames followed, optional tag filter and prune.
- `validate`: structure, connections, node groups, duplicate names and ids, hard-coded secrets, canonical form.
- `format`: rewrite files in canonical form (`--check` for CI).
- `plan`: create / update / publish / unpublish preview with JSON diffs and a drift-friendly exit code.
- `deploy`: create, update, tag, publish and unpublish workflows; published versions named after the Git commit.
- n8n 2.x drafts and publishing; fallbacks for n8n 1.x.
- Example pipelines for GitLab CI and GitHub Actions; Docker image.
