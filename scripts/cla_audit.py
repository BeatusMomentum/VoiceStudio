#!/usr/bin/env python3
"""List contributors whose code is in the tree but who have not signed the CLA.

Blames every tracked text file at HEAD, credits each line to its commit's
author and co-authors, maps them to GitHub accounts, and compares the result
with the signature store that .github/scripts/cla_check.py writes to the
`cla-signatures` branch.

    git fetch origin cla-signatures
    GH_TOKEN=... python scripts/cla_audit.py               # Markdown report
    GH_TOKEN=... python scripts/cla_audit.py --json out.json

People are matched by GitHub account ID. An author is mapped by their GitHub
no-reply address, else by the account linked to the commit email. Commits by AI
agents or unlinked emails are credited to the author of the pull request that
merged them, because that person submitted the work. Without a token the GitHub
API rate limit runs out quickly; unresolved authors are then listed by email.
Blame follows moves within a file only, so code copied between files is
credited to whoever moved it, and squash commits that fold in other people's
pull requests are listed separately for manual review.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SLUG = "debpalash/VoiceStudio"

_spec = importlib.util.spec_from_file_location("cla_check", REPO / ".github" / "scripts" / "cla_check.py")
cla = importlib.util.module_from_spec(_spec)
sys.modules["cla_check"] = cla
_spec.loader.exec_module(cla)

SIGNATURES = f"origin/{cla.SIGNATURE_BRANCH}:{cla.SIGNATURE_PATH}"
BINARY = re.compile(r"\.(lock|png|jpe?g|gif|webp|ico|icns|svg|wav|mp3|flac|ogg|woff2?|ttf|pdf|onnx|bin)$", re.I)
# Identities that do not name the person who submitted the work.
AGENT = re.compile(r"^(noreply@anthropic\.com|.*@openai\.com|codex@users\.noreply\.github\.com|test@local|you@example\.com|)$")
FOLDS_IN = re.compile(r"\bsupersed", re.I)
NOTES = {
    "lookup-failed": "GitHub lookup failed; set GH_TOKEN and rerun",
    "name": "matched by display name; confirm",
    "unresolved": "no linked account or pull request; resolve by hand",
    "submitted": "submitted from an unlinked or agent identity; confirm the work is theirs",
}


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def github(path: str) -> object:
    request = urllib.request.Request(f"https://api.github.com/repos/{SLUG}/{path}")
    if token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def blame(path: str) -> collections.Counter:
    lines: collections.Counter = collections.Counter()
    commit = name = email = None
    out = subprocess.run(["git", "-C", str(REPO), "blame", "-w", "-M", "--line-porcelain", "HEAD", "--", path],
                         capture_output=True, text=True, errors="replace").stdout
    for row in out.splitlines():
        if re.match(r"^[0-9a-f]{40} ", row):
            commit = row[:40]
        elif row.startswith("author "):
            name = row[7:]
        elif row.startswith("author-mail "):
            email = row[12:].strip("<>").lower()
        elif row.startswith("\t"):
            lines[(email, name, commit, path)] += 1
    return lines


def commit_messages(commits: set[str]) -> dict[str, str]:
    messages = {}
    for chunk in git("log", "--no-walk=unsorted", "--format=%H%x00%B%x01", *sorted(commits)).split("\x01"):
        if "\x00" in chunk:
            sha, body = chunk.strip().split("\x00", 1)
            messages[sha] = body
    return messages


def account_for(email: str, commit: str) -> tuple[str | None, int | None, str]:
    """GitHub account for an author email: (login, id, noreply|api|unlinked|lookup-failed)."""
    if AGENT.match(email):
        return None, None, "unlinked"
    if (match := cla.NOREPLY.match(email)) and match.group(1):
        return match.group(2), int(match.group(1)), "noreply"
    try:
        author = github(f"commits/{commit}").get("author") or {}
    except (OSError, ValueError, urllib.error.HTTPError):
        return None, None, "lookup-failed"
    return (author["login"], author["id"], "api") if author.get("id") else (None, None, "unlinked")


def pr_author_for(commit: str) -> tuple[str | None, int | None, str]:
    try:
        pulls = github(f"commits/{commit}/pulls")
    except (OSError, ValueError, urllib.error.HTTPError):
        return None, None, "lookup-failed"
    return (pulls[0]["user"]["login"], pulls[0]["user"]["id"], "submitted") if pulls else (None, None, "unresolved")


def names_to_logins() -> dict[str, tuple[str, int]]:
    """Display names seen with a GitHub no-reply address that includes an account ID."""
    names: dict[str, tuple[str, int]] = {}
    log = git("log", "--format=%aN <%aE>%n%(trailers:key=Co-authored-by,valueonly)")
    for match in re.finditer(r"^(.+?) <([^>]+)>$", log, re.M):
        noreply = cla.NOREPLY.match(match.group(2))
        if noreply and noreply.group(1) and not AGENT.match(match.group(2).lower()):
            names.setdefault(match.group(1).strip().lower(), (noreply.group(2), int(noreply.group(1))))
    return names


def signed_ids(allow_missing: bool) -> set[int]:
    result = subprocess.run(["git", "-C", str(REPO), "show", SIGNATURES], capture_output=True, text=True)
    if result.returncode != 0:
        if allow_missing:
            return set()
        raise SystemExit(f"{SIGNATURES} not found. Run `git fetch origin {cla.SIGNATURE_BRANCH}`, "
                         "or pass --no-signatures to report everyone.")
    return {int(s["id"]) for s in json.loads(result.stdout)["signatures"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    parser.add_argument("--no-signatures", action="store_true", help="report everyone as unsigned")
    args = parser.parse_args()
    signed = signed_ids(args.no_signatures)

    files = [f for f in git("ls-files").splitlines() if f and not BINARY.search(f)]
    blamed: collections.Counter = collections.Counter()
    with ThreadPoolExecutor(16) as pool:
        for counts in pool.map(blame, files):
            blamed.update(counts)
    messages = commit_messages({c for _, _, c, _ in blamed})

    # Credit each line to the commit author and to every person co-author.
    credits: collections.Counter = collections.Counter()  # (email, name, commit, path) -> lines
    for (email, name, commit, path), n in blamed.items():
        credits[(email, name, commit, path)] += n
        for co in cla.co_authors(messages.get(commit, "")):
            co_email = co["email"].lower()
            if not cla.TOOL_EMAILS.match(co_email) and co_email != email:
                credits[(co_email, co["name"], commit, path)] += n

    first_commit: dict[str, str] = {}
    for email, _, commit, _ in credits:
        first_commit.setdefault(email, commit)
    with ThreadPoolExecutor(4) as pool:
        accounts = dict(zip(first_commit, pool.map(lambda e: account_for(e, first_commit[e]), first_commit)))
    per_commit = sorted({c for e, _, c, _ in credits if accounts[e][2] == "unlinked"})
    with ThreadPoolExecutor(4) as pool:
        submitters = dict(zip(per_commit, pool.map(pr_author_for, per_commit)))

    names = names_to_logins()
    people: dict[str, dict] = collections.defaultdict(
        lambda: {"lines": 0, "files": collections.Counter(), "emails": set(), "notes": set(), "login": None, "id": None}
    )
    folded: collections.Counter = collections.Counter()
    for (email, name, commit, path), n in credits.items():
        login, account_id, source = accounts[email]
        if source == "unlinked":
            login, account_id, source = submitters[commit]
        if not account_id and (guess := names.get((name or "").strip().lower())):
            (login, account_id), source = guess, "name"
        if account_id in cla.MAINTAINER_IDS and source in ("noreply", "api"):
            if FOLDS_IN.search(messages.get(commit, "")):
                folded[commit] += n
            continue
        if account_id in cla.BOT_IDS:
            continue
        key = str(account_id) if account_id else (email or name or "?")
        person = people[key]
        person["lines"] += n
        person["files"][path] += n
        person["emails"].add(email)
        person["login"] = person["login"] or login
        person["id"] = account_id
        if source in NOTES:
            person["notes"].add(source)

    unsigned = sorted(
        ((key, p) for key, p in people.items() if p["id"] not in signed),
        key=lambda item: -item[1]["lines"],
    )
    print(f"# CLA audit — {len(unsigned)} unsigned of {len(people)} contributors with code at HEAD\n")
    print("| Contributor | Lines at HEAD | Largest files |\n|---|---:|---|")
    for key, p in unsigned:
        label = f"@{p['login']}" if p["login"] else f"`{key}`"
        if p["notes"]:
            label += " (" + "; ".join(NOTES[n] for n in sorted(p["notes"])) + ")"
        top = ", ".join(f"`{f}` ({n})" for f, n in p["files"].most_common(3))
        print(f"| {label} | {p['lines']} | {top} |")
    if folded:
        print("\n## Maintainer commits that fold in other people's work (review by hand)\n")
        for commit, n in folded.most_common():
            subject = messages.get(commit, "").splitlines()[0] if messages.get(commit) else ""
            print(f"- `{commit[:9]}` {subject} — {n} lines at HEAD")

    if args.json:
        args.json.write_text(json.dumps({
            "people": {key: {"login": p["login"], "id": p["id"], "lines": p["lines"], "emails": sorted(p["emails"]),
                             "signed": p["id"] in signed, "notes": sorted(p["notes"]),
                             "files": p["files"].most_common()} for key, p in people.items()},
            "folded_commits": dict(folded),
        }, indent=1))


if __name__ == "__main__":
    main()
