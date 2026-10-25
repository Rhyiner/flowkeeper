# flowkeeper

**GitOps for n8n.** Keep your [n8n](https://n8n.io) workflows in Git, review changes in merge requests, and deploy them from CI.

[![CI](https://github.com/YOUR_GITHUB_USER/flowkeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_GITHUB_USER/flowkeeper/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

n8n keeps workflows in its database. That makes it hard to see what changed, to review a change before it goes live, or to roll back a bad edit. flowkeeper writes each workflow to a clean JSON file you can commit, and deploys those files back to n8n.

```
   n8n editor ──export──►  Git  ──merge request──►  CI: validate, plan  ──merge──►  CI: deploy ──► n8n
```

- **Clean diffs.** Exports drop timestamps, version ids and other bookkeeping, and sort everything, so a diff shows only what really changed.
- **Offline validation.** Broken JSON, duplicate node names, connections to missing nodes and hard-coded secrets (API keys, tokens, private keys) are caught before anything reaches n8n.
- **Plan before deploy.** `flowkeeper plan` lists what a deployment would create, update, publish or unpublish, with an optional JSON diff.
- **Built for n8n 2.x.** Understands drafts and publishing, and names each published version after the Git commit, so n8n's version history points back to Git.
- **No dependencies.** Python standard library only. Runs in any CI image with Python 3.10+.

Tested end to end against **n8n 2.44.6**. n8n 1.x is supported through fallbacks (the `/activate` endpoint and the older settings schema).

## Install

```bash
pip install https://github.com/YOUR_GITHUB_USER/flowkeeper/archive/refs/tags/v0.1.0.tar.gz
flowkeeper --version
```

Or with Docker:

```bash
docker run --rm -v "$PWD:/work" -e N8N_URL -e N8N_API_KEY ghcr.io/YOUR_GITHUB_USER/flowkeeper:0.1 plan
```

## Quick start

1. In n8n, create an API key: **Settings → n8n API → Create an API key**.
2. Tell flowkeeper where n8n is:

   ```bash
   export N8N_URL=https://n8n.example.com
   export N8N_API_KEY=...          # keep it out of your shell history and of Git
   ```

3. Export your workflows and commit them:

   ```bash
   mkdir n8n-workflows && cd n8n-workflows && git init
   flowkeeper export               # writes workflows/*.json
   flowkeeper validate
   git add . && git commit -m "Import n8n workflows"
   ```

4. Change a workflow (in a file, or in the n8n editor followed by `flowkeeper export`), then:

   ```bash
   flowkeeper plan --diff          # what would change in n8n
   flowkeeper deploy               # make n8n match Git
   ```

## Commands

| Command | What it does | Needs n8n |
|---|---|---|
| `flowkeeper export` (`pull`) | Writes every workflow to `workflows/<name>.json` | yes |
| `flowkeeper validate` | Checks the files: structure, connections, duplicates, secrets | no |
| `flowkeeper format` (`fmt`) | Rewrites the files in canonical form | no |
| `flowkeeper plan` (`diff`) | Shows what `deploy` would change | yes |
| `flowkeeper deploy` (`push`) | Creates, updates, tags and publishes workflows to match Git | yes |

Useful options:

- `export --tag gitops` exports only workflows with that tag (repeatable). `--prune` deletes files of workflows that no longer exist. `--keep-pin-data` keeps pinned test data, which is dropped by default.
- `validate --strict` fails on warnings too. `--no-secret-scan` turns the secret search off.
- `plan --detailed-exitcode` exits with `2` when there are changes: handy for a scheduled drift check.
- `deploy --dry-run` only shows the plan. `--no-activation` never publishes or unpublishes anything.
- `-v` also lists unchanged workflows, and workflows that exist only in n8n.

Every command reads `N8N_URL`, `N8N_API_KEY` and `FLOWKEEPER_DIR` (default `workflows`) from the environment. Run `flowkeeper <command> --help` for the full list.

## The files

One workflow per file, named after the workflow:

```json
{
  "id": "aBcD1234eFgH5678",
  "name": "Daily report",
  "active": true,
  "tags": ["reports"],
  "settings": { "executionOrder": "v1" },
  "nodes": [ ... ],
  "connections": { ... }
}
```

- **What is kept:** `id`, `name`, `description`, `active`, `tags` (as names), `settings`, `nodes`, `connections` and `nodeGroups` (canvas frames).
- **What is dropped:** timestamps, version ids, sharing and ownership, execution counters, `meta`, `staticData` and, unless you ask for it, `pinData`.
- **Matching:** a file is matched to an n8n workflow by `id`, then by name. A file without `id` is created on first deploy; the next `export` writes the new id into it.
- **Folders:** you can move files into sub-folders; exports keep them where you put them.
- **Renames:** when a workflow is renamed in n8n, `export` renames its file.

`active: true` means "published". On n8n 2.x, edits are drafts until they are published, so `deploy` publishes every active workflow it updates, and also publishes a workflow whose live version differs from Git (someone edited it in n8n without going through Git). Published versions are named `git <commit>` in CI (from `CI_COMMIT_SHORT_SHA` or `GITHUB_SHA`), or what you pass with `--version-name`.

## CI/CD

Ready-to-copy pipelines:

- GitLab CI: [`examples/gitlab-ci.yml`](examples/gitlab-ci.yml)
- GitHub Actions: [`examples/github-actions.yml`](examples/github-actions.yml)

Both run `validate` on every change, `plan` on merge requests, `deploy` on the default branch, and an optional scheduled drift check.

`validate` runs anywhere. The jobs that talk to n8n must run where n8n is reachable. For an n8n on a private network, that means a self-hosted runner (GitLab Runner or a GitHub Actions runner) on your network. It connects out to GitLab or GitHub, so you do not need to open any port.

Store the API key as a masked, protected CI variable or as a secret, never in the repository.

## What flowkeeper does not do

- **Credentials.** Secrets stay in n8n. Workflows refer to credentials by id and name, and the credentials must exist on the instance you deploy to: n8n refuses to publish a workflow whose required credential is missing.
- **Deleting workflows.** Removing a file never deletes anything in n8n. The workflow shows up as "only in n8n" with `plan -v`; delete or archive it in n8n yourself.
- **Moving between instances.** Credential ids and sub-workflow ids differ from one instance to another and are not rewritten. Deploying to the instance the files were exported from always works.
- **Execution data, variables, data tables, users and projects** are out of scope.

## Security

- The secret scan looks for well-known token formats (GitHub, GitLab, Slack, AWS, Google, `sk-` keys, Telegram bots, private keys, bearer tokens) and warns about values in fields named like `password` or `token`. n8n expressions such as `={{ $env.TOKEN }}` are not flagged. Prefer credentials or environment variables to secrets in node parameters.
- Pinned data is dropped by default because it often holds real payloads: emails, names, tokens.
- If your n8n lets you choose API key scopes, give flowkeeper only what it uses. `export` and `plan` need `workflow:list`, `workflow:read`, `tag:list` and `workflowTags:list`. `deploy` also needs `workflow:create`, `workflow:update`, `workflow:activate`, `workflow:deactivate`, `tag:create` and `workflowTags:update`.
- Pass the key through `N8N_API_KEY` rather than `--api-key`: command-line arguments are visible to other users of the machine.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (for `plan`, also "no changes" unless `--detailed-exitcode`) |
| 1 | Validation errors, API errors or a failed deployment |
| 2 | `plan --detailed-exitcode` found changes |
| 64 | Wrong command-line usage |

## Development

```bash
python -m unittest discover -s tests -t tests       # unit tests, against a fake n8n
```

The end-to-end tests run the whole export → plan → deploy cycle against a real, disposable n8n:

```bash
docker run -d --name n8n-e2e -p 5678:5678 -e N8N_SECURE_COOKIE=false n8nio/n8n:2.44.6
export FLOWKEEPER_E2E_URL=http://127.0.0.1:5678
export FLOWKEEPER_E2E_API_KEY=$(python tests/e2e/bootstrap.py "$FLOWKEEPER_E2E_URL")
python -m unittest discover -s tests/e2e -v
```

`bootstrap.py` creates the owner account and an API key through n8n's internal endpoints. Only use it on a throwaway instance.

## License

[MIT](LICENSE)
