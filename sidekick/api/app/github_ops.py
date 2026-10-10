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

    Each edit is {"path", "find", "replace"}; `find` must occur exactly once in the file (after earlier edits to it)."""
    if not edits:
        raise GitHubError("No edits given")
    originals, current = {}, {}
    for i, e in enumerate(edits, 1):
        path, find, repl = (e.get("path") or "").strip("/"), e.get("find"), e.get("replace")
        if not path or not find or repl is None:
            raise GitHubError(f"Edit {i}: needs path, find and replace")
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
            _, blob = read_file(path, ref=branch)
            return _call("PUT", f"/repos/{repo}/contents/{path}", h, json={
                "message": f"{change['title']}\n\nProposed by Sidekick, approved by {change.get('approved_by', 'a human')}.",
                "content": base64.b64encode(text.encode("utf-8")).decode(), "sha": blob, "branch": branch})["commit"]["sha"]
        step("github.commit", path, commit)

    body = (f"{change.get('summary', '')}\n\n**Requested by:** {change.get('requested_by', '?')} · **Approved by:** {change.get('approved_by', '?')}\n\n"
            f"```diff\n{prepared['diff'][:60000]}\n```\n\n_Opened by Sidekick after human approval._")
    pr = step("github.create_pull_request", f"{branch} → {base}",
              lambda: _call("POST", f"/repos/{repo}/pulls", h, json={"title": change["title"], "head": branch, "base": base, "body": body}))
    return {"url": pr["html_url"], "number": pr["number"], "branch": branch, "repo": repo, "base": base}
