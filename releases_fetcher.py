"""
releases_fetcher.py - Fetch GitHub Releases for a configurable repo.

Standalone, stdlib-only. Defaults to anthropics/claude-code so out of the box
the dashboard shows news for Claude Code itself; users can point it at any
public repo via the CLAUDE_USAGE_RELEASES_REPO environment variable
(format: "owner/name") or by passing repo= explicitly.

Results are cached in the existing usage.db SQLite database for 6 hours
to avoid hitting GitHub's unauthenticated rate limit.
"""

import json
import os
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DEFAULT_REPO = "anthropics/claude-code"
CACHE_TTL_SECONDS = 6 * 3600
USER_AGENT = "claude-usage-dashboard/1.0"


def configured_repo():
    return os.environ.get("CLAUDE_USAGE_RELEASES_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO


def init_releases_table(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS releases_cache (
            repo        TEXT PRIMARY KEY,
            fetched_at  TEXT,
            payload     TEXT
        );
    """)
    conn.commit()


def _fetch_from_github(repo):
    url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in data:
        out.append({
            "tag":          r.get("tag_name"),
            "name":         r.get("name") or r.get("tag_name"),
            "published_at": r.get("published_at"),
            "html_url":     r.get("html_url"),
            "body":         (r.get("body") or "")[:4000],
            "prerelease":   bool(r.get("prerelease")),
            "author":       (r.get("author") or {}).get("login"),
        })
    return out


def get_releases(conn, repo=None, force=False):
    """Return list of releases for `repo`, using a 6h SQLite cache."""
    repo = repo or configured_repo()
    init_releases_table(conn)

    row = conn.execute(
        "SELECT fetched_at, payload FROM releases_cache WHERE repo = ?",
        (repo,),
    ).fetchone()

    if row and not force:
        try:
            fetched = datetime.fromisoformat(row[0])
            if datetime.now(timezone.utc) - fetched < timedelta(seconds=CACHE_TTL_SECONDS):
                return {"repo": repo, "cached": True, "releases": json.loads(row[1])}
        except Exception:
            pass

    try:
        releases = _fetch_from_github(repo)
    except urllib.error.HTTPError as e:
        # Fall back to stale cache if available
        if row:
            return {"repo": repo, "cached": True, "stale": True,
                    "releases": json.loads(row[1]), "error": f"HTTP {e.code}"}
        return {"repo": repo, "releases": [], "error": f"HTTP {e.code}"}
    except Exception as e:
        if row:
            return {"repo": repo, "cached": True, "stale": True,
                    "releases": json.loads(row[1]), "error": str(e)}
        return {"repo": repo, "releases": [], "error": str(e)}

    conn.execute(
        "INSERT OR REPLACE INTO releases_cache (repo, fetched_at, payload) VALUES (?, ?, ?)",
        (repo, datetime.now(timezone.utc).isoformat(), json.dumps(releases)),
    )
    conn.commit()
    return {"repo": repo, "cached": False, "releases": releases}


if __name__ == "__main__":
    conn = sqlite3.connect(os.path.expanduser("~/.claude/usage.db"))
    out = get_releases(conn, force=True)
    print(f"Repo: {out['repo']}  Cached: {out.get('cached')}  Count: {len(out['releases'])}")
    for r in out["releases"][:5]:
        print(f"  {r['tag']:15} {r['published_at'][:10]}  {r['name']}")
