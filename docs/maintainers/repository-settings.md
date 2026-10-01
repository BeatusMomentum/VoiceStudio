# Repository settings

GitHub stores these settings outside the repository, so no file or CI check can
enforce them. Apply them once as a repository admin, then re-check them after
changing workflow or job names. The `gh api` commands below assume an
authenticated `gh` with admin access to `debpalash/VoiceStudio`.

## Ruleset: `main`

Requires the backend/frontend test job and the CLA check, and blocks force-push
and deletion. Required checks also stop direct pushes, so changes land through
pull requests.

The `context` values must match the check names shown on a pull request:

- `Tests (backend + frontend)`: the `name:` of the `test` job in `.github/workflows/ci.yml`.
- `cla`: the job ID in `.github/workflows/cla.yml`. That job has no `name:`, so
  its check is called `cla`. If the job gains `name: CLA`, use `CLA` here.

`integration_id` 15368 is GitHub Actions, so only workflow runs can satisfy
these checks. `strict_required_status_checks_policy` requires branches to be up
to date with `main` before merging.

```bash
gh api --method POST repos/debpalash/VoiceStudio/rulesets --input - <<'JSON'
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "Tests (backend + frontend)", "integration_id": 15368 },
          { "context": "cla", "integration_id": 15368 }
        ]
      }
    }
  ]
}
JSON
```

Leave `bypass_actors` empty unless the owner chooses an emergency bypass. To add
one, use `{ "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request" }`
(repository admins, pull requests only). Do not enable "Require review from
Code Owners" while `@debpalash` is the only code owner, because GitHub does not
let authors approve their own pull requests.

UI: **Settings → Rules → Rulesets → New ruleset → New branch ruleset**. Set the
target to the default branch, enable **Restrict deletions**, **Block force
pushes** and **Require status checks to pass**, then add both checks with source
**GitHub Actions**.

## Ruleset: `cla-signatures`

`.github/workflows/cla.yml` commits signatures to this branch with the
workflow's `GITHUB_TOKEN`. The ruleset blocks deletion and force-push only. It
does not restrict updates or require checks, so the workflow can keep committing.
You can create the ruleset before the branch exists.

```bash
gh api --method POST repos/debpalash/VoiceStudio/rulesets --input - <<'JSON'
{
  "name": "cla-signatures",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/cla-signatures"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ]
}
JSON
```

Check both rulesets with `gh api repos/debpalash/VoiceStudio/rulesets`.

## Secret scanning and push protection

UI: **Settings → Code security → Secret Protection**. Enable **Secret scanning**
and **Push protection**.

```bash
gh api --method PATCH repos/debpalash/VoiceStudio --input - <<'JSON'
{ "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" } } }
JSON
```

The PostHog project token committed in `backend/core/analytics.py` and
`electron/src/shared/utils/analytics.ts` is a publishable write-only token by
design (see `tests/test_no_committed_analytics_token.py`). If an alert flags it,
close the alert as "used in tests" or "false positive". Do not remove the token.

## Private vulnerability reporting

`.github/SECURITY.md` names GitHub Security Advisories as the preferred channel,
and that link only works while this setting is on.

UI: **Settings → Code security → Private vulnerability reporting → Enable**.

```bash
gh api --method PUT repos/debpalash/VoiceStudio/private-vulnerability-reporting
```

## Dependabot alerts

UI: **Settings → Code security → Dependabot alerts → Enable**. This setting
turns on alerts only. The repository has no `.github/dependabot.yml`, so
Dependabot does not open version-update pull requests.

```bash
gh api --method PUT repos/debpalash/VoiceStudio/vulnerability-alerts
```

## Maintainer commit email

Each maintainer should go to **GitHub → Settings → Emails** and enable **Keep my
email addresses private** and **Block command line pushes that expose my
email**. Then set the no-reply address shown on that page as the commit
identity:

```bash
git config --global user.email "ID+USERNAME@users.noreply.github.com"
```

Commits keep their GitHub attribution, and `scripts/cla_audit.py` maps no-reply
addresses to GitHub logins without an API lookup. Commits already pushed
keep their old address.
