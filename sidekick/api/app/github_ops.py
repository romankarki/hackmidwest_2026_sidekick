"""GitHub code operations for the target repo (Integrations → GitHub: token, repo, base branch).

Reading (used by the LLM agent's tools): list files, read a file, search code.
Writing happens only here and only after a human approves: `open_pr()` creates a branch, commits the approved
edits and opens a pull request. The model never writes to GitHub itself; it proposes exact find/replace edits,
`prepare_edits()` checks them against the current base branch and renders the diff the approver sees.
"""
import base64
import difflib
import re
import time

import requests

from . import integrations

API = "https://api.github.com"
TIMEOUT = 15
TEXT_EXT = (".html", ".js", ".css", ".py", ".md", ".json", ".yml", ".yaml", ".sql", ".sh", ".conf", ".txt", ".toml", ".ts", ".tsx", ".jsx")


class GitHubError(RuntimeError):
    pass


def config():
    c = integrations.creds("github")
    if not c.get("token") or not c.get("repo"):
        raise GitHubError("GitHub is not connected. Configure it in Sidekick → Integrations → GitHub (token + repository).")
    return c["repo"], (c.get("base_branch") or "main"), {"Authorization": f"Bearer {c['token']}", "Accept": "application/vnd.github+json",
                                                         "X-GitHub-Api-Version": "2022-11-28"}


def _call(method, path, h, **kw):
    r = requests.request(method, f"{API}{path}", headers=h, timeout=TIMEOUT, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text[:200]
        raise GitHubError(f"GitHub {method} {path} → HTTP {r.status_code}: {msg}")
    return r.json() if r.content else {}


# ---------- read ----------
def list_files(prefix=""):
    repo, base, h = config()
    tree = _call("GET", f"/repos/{repo}/git/trees/{base}", h, params={"recursive": "1"})
    prefix = prefix.strip("/")
    return [t["path"] for t in tree.get("tree", []) if t["type"] == "blob" and t["path"].startswith(prefix)]


def read_file(path, ref=None):
    """(text, blob sha) of a file on the base branch (or `ref`)."""
    repo, base, h = config()
    f = _call("GET", f"/repos/{repo}/contents/{path.strip('/')}", h, params={"ref": ref or base})
    if isinstance(f, list) or f.get("type") != "file":
        raise GitHubError(f"{path} is a directory, not a file")
    return base64.b64decode(f["content"]).decode("utf-8"), f["sha"]


def file_exists(path, ref=None):
    repo, base, h = config()
    r = requests.get(f"{API}/repos/{repo}/contents/{path.strip('/')}", headers=h, params={"ref": ref or base}, timeout=TIMEOUT)
    if r.status_code == 404:
        return False
    if r.status_code >= 400:
        raise GitHubError(f"GitHub GET contents/{path} → HTTP {r.status_code}")
    return True


_brief_cache = {"at": 0.0, "key": None, "text": ""}


def repo_brief(max_chars=6000):
    """The target repo's SIDEKICK.md (stack, file map, conventions), cached 5 min. Empty if GitHub isn't connected or no file."""
    try:
        repo, base, _ = config()
    except GitHubError:
        return ""
    if _brief_cache["key"] == (repo, base) and time.time() - _brief_cache["at"] < 300:
        return _brief_cache["text"]
    try:
        text = read_file("SIDEKICK.md")[0][:max_chars] if file_exists("SIDEKICK.md") else ""
    except (GitHubError, requests.RequestException):
        text = _brief_cache["text"] if _brief_cache["key"] == (repo, base) else ""
    _brief_cache.update(at=time.time(), key=(repo, base), text=text)
    return text


def search(query, max_hits=30):
    """Case-insensitive text search over the repo's text files (the repo is small; GitHub code search doesn't index new repos)."""
    q = query.lower()
    hits = []
    for path in list_files():
        if not path.endswith(TEXT_EXT) and "Dockerfile" not in path:
            continue
        text, _ = read_file(path)
        for n, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                hits.append({"file": path, "line": n, "text": line.strip()[:160]})
                if len(hits) >= max_hits:
                    return hits
    return hits


# ---------- propose ----------
def prepare_edits(edits):
    """Validate find/replace edits against the base branch. Returns {"files": {path: new_text}, "diff": str}.

    Each edit is {"path", "find", "replace"}; `find` must occur exactly once in the file (after earlier edits to it).
    An empty `find` creates a NEW file whose content is `replace` (refused if the file already exists)."""
    if not edits:
        raise GitHubError("No edits given")
    originals, current = {}, {}
    for i, e in enumerate(edits, 1):
        path, find, repl = (e.get("path") or "").strip("/"), e.get("find") or "", e.get("replace")
        if not path or repl is None:
            raise GitHubError(f"Edit {i}: needs path, find and replace")
        if not find:  # new file
            if path in current or file_exists(path):
                raise GitHubError(f"Edit {i}: {path} already exists; to change it, use a non-empty `find` copied from the file")
            originals[path], current[path] = "", repl
            continue
        if path not in current:
            originals[path], _ = read_file(path)
            current[path] = originals[path]
        n = current[path].count(find)
        if n != 1:
            raise GitHubError(f"Edit {i}: the `find` text occurs {n} times in {path}; it must match exactly once. "
                              "Copy it exactly from github_read_file (include more surrounding text to make it unique).")
        current[path] = current[path].replace(find, repl, 1)
    diff = "".join("".join(difflib.unified_diff(originals[p].splitlines(True), current[p].splitlines(True), f"a/{p}", f"b/{p}", n=1))
                   for p in current if current[p] != originals[p])
    if not diff:
        raise GitHubError("The edits don't change anything")
    return {"files": {p: t for p, t in current.items() if t != originals[p]}, "diff": diff}


# ---------- write (only after approval) ----------
def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "change"


def open_pr(change, step):
    """Create a branch, commit the approved edits and open a PR. `step(tool, detail, fn)` reports progress.

    Edits are re-validated against the latest base branch, so a file that changed since approval fails safely."""
    repo, base, h = config()
    prepared = step("github.validate_edits", f"{len(change['edits'])} edit(s) against {repo}@{base}", lambda: prepare_edits(change["edits"]))
    branch = f"sidekick/{slug(change['title'])}-{int(time.time()) % 100000}"

    def create_branch():
        sha = _call("GET", f"/repos/{repo}/git/ref/heads/{base}", h)["object"]["sha"]
        _call("POST", f"/repos/{repo}/git/refs", h, json={"ref": f"refs/heads/{branch}", "sha": sha})
        return sha
    step("github.create_branch", f"{branch} from {base}", create_branch)

    for path, text in prepared["files"].items():
        def commit(path=path, text=text):
            body = {"message": f"{change['title']}\n\nProposed by Sidekick, approved by {change.get('approved_by', 'a human')}.",
                    "content": base64.b64encode(text.encode("utf-8")).decode(), "branch": branch}
            if file_exists(path, ref=branch):  # updating needs the current blob sha; new files don't
                body["sha"] = read_file(path, ref=branch)[1]
            return _call("PUT", f"/repos/{repo}/contents/{path}", h, json=body)["commit"]["sha"]
        step("github.commit", path, commit)

    body = (f"{change.get('summary', '')}\n\n**Requested by:** {change.get('requested_by', '?')} · **Approved by:** {change.get('approved_by', '?')}\n\n"
            f"```diff\n{prepared['diff'][:60000]}\n```\n\n_Opened by Sidekick after human approval._")
    pr = step("github.create_pull_request", f"{branch} → {base}",
              lambda: _call("POST", f"/repos/{repo}/pulls", h, json={"title": change["title"], "head": branch, "base": base, "body": body}))
    return {"url": pr["html_url"], "number": pr["number"], "branch": branch, "repo": repo, "base": base, "body": body,
            "diff": prepared["diff"], "files": list(prepared["files"])}


# ---------- hand-off issue: a developer reviews the PR and builds on top of it ----------
LABELS = {"sidekick": ("6f42c1", "Opened by Sidekick"), "needs-review": ("fbca04", "A developer should review and take this further")}


def _ensure_labels(repo, h):
    for name, (color, desc) in LABELS.items():
        r = requests.post(f"{API}/repos/{repo}/labels", headers=h, timeout=TIMEOUT, json={"name": name, "color": color, "description": desc})
        if r.status_code not in (201, 422):  # 422 = already exists
            raise GitHubError(f"GitHub create label {name} → HTTP {r.status_code}")


def open_review_issue(change, pr, preview_url=None):
    """GitHub issue asking a developer to review the PR in its preview and build on top of it. Links PR ↔ issue.
    Assignee: Integrations → GitHub → 'Review issue assignee' (optional GitHub username)."""
    repo, base, h = config()
    assignee = (integrations.creds("github").get("issue_assignee") or "").strip().lstrip("@")
    files = "\n".join(f"- `{f}`" for f in pr["files"])
    assumptions = "\n".join(f"- {a}" for a in change.get("assumptions") or []) or "- None stated"
    follow_ups = "\n".join(f"- [ ] {f}" for f in change.get("follow_ups") or []) or "- [ ] Anything the preview shows is missing"
    body = f"""Sidekick opened **#{pr['number']}** ({change['title']}). It is a first pass: please review it in the live preview and build on top of it.

| | |
|---|---|
| **Pull request** | #{pr['number']} · branch `{pr['branch']}` → `{pr['base']}` |
| **Preview** | {preview_url or "_no preview configured_"} |
| **Requested by** | {change.get('requested_by', '?')} · approved by {change.get('approved_by', '?')} |

### What changed and why
{change.get('summary', '')}

**Files:**
{files}

### Assumptions Sidekick made
{assumptions}

### Review checklist
- [ ] Open the preview{f" ({preview_url})" if preview_url else ""} and try the change end to end
- [ ] Check light and dark mode, and a narrow (mobile) window
- [ ] Read the diff in #{pr['number']}; push fixes to `{pr['branch']}` (the preview rebuilds on every push)
- [ ] Merge #{pr['number']} when it's right; merging deploys to production

### Suggested follow-ups
{follow_ups}

<details><summary>Diff Sidekick proposed</summary>

```diff
{pr['diff'][:30000]}
```
</details>

_Filed by Sidekick so a developer can take this further. Closes when #{pr['number']} merges._"""
    _ensure_labels(repo, h)
    payload = {"title": f"Review & build on: {change['title']} (#{pr['number']})", "body": body, "labels": list(LABELS)}
    if assignee:
        payload["assignees"] = [assignee]
    try:
        issue = _call("POST", f"/repos/{repo}/issues", h, json=payload)
    except GitHubError:
        if not assignee:
            raise
        payload.pop("assignees")  # not a collaborator / typo: still file the issue, unassigned
        payload["body"] += f"\n\n_Could not assign @{assignee} (not a collaborator on this repo?)._"
        issue = _call("POST", f"/repos/{repo}/issues", h, json=payload)
        assignee = ""
    # link back: the PR closes the issue when it merges, and shows where the follow-up lives
    _call("PATCH", f"/repos/{repo}/pulls/{pr['number']}", h,
          json={"body": f"{pr['body']}\n\nCloses #{issue['number']} (review & follow-ups for a developer)."})
    return {"url": issue["html_url"], "number": issue["number"], "assignee": assignee or None}
