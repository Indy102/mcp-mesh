"""git descriptive source — Rev 10 milestone.

MEASUREMENT POLICY (recorded in the value definition, because it changes values):

  author_emails        exact-match allowlist; no name matching, no fuzzy matching
  ref_scope            commits reachable from the HEAD OIDs captured at collection time
  merges               excluded (a merge is not authored work by this definition)
  deduplication        by commit object id, across every configured repository, so the
                       same commit in two clones counts once
  timestamp            AUTHOR date (not committer), converted to the analysis timezone
  day                  the civil date of that converted timestamp
  history model        FULL local history only. A shallow clone, a partial (promisor)
                       clone, and a repository with grafts or replace refs do not
                       contain the history the ref scope names, so they are REFUSED.
                       Nothing is fetched from a network to make an adapter pass.
  empty repository     a repository with no commits has complete history containing
                       zero commits; it is read successfully and contributes nothing
  structural zero      emitted ONLY for days inside a window where EVERY configured
                       repository was read successfully AND proved to hold complete
                       history. A shallow clone returns commits, so a successful
                       `git log` is not evidence that a day really had none.
  partial failure      if any configured repository cannot be read, or its history is
                       incomplete, the metric is `failed` for the run; no
                       complete-looking subtotal and no structural zero is emitted

WHAT THIS MEASURES: the commit history of the configured repositories under the above
policy. It does not measure personal productivity, effort, or output quality.
"""
import subprocess, hashlib, json, pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

METRIC = "git.commits_count"
NORMALIZATION_VERSION = 1


class RepoUnreadable(RuntimeError):
    pass


class HistoryIncomplete(RepoUnreadable):
    """The repository does not hold the history the ref scope names.

    A shallow or partial clone answers `git log` successfully with a TRUNCATED history,
    so every day before the truncation point looks like a proven zero. Refusing is the
    only honest option that does not fabricate values and does not reach the network.
    """


def value_definition(*, author_emails, repos, analysis_timezone):
    """The canonical, complete definition. Its digest is the value_definition_id.

    Repository paths are CANONICALISED. `/var/x` and `/private/var/x` are the same
    directory spelled two ways, and a definition that distinguished them would make one
    repository into two different measurements — which then reads as a changed
    measurement configuration when it is nothing of the kind. Unlike an email local
    part, a filesystem path has an unambiguous canonical form, so resolving it is a
    fact rather than a guess.
    """
    return {"metric_id": METRIC, "normalization_version": NORMALIZATION_VERSION,
            "author_emails": sorted(author_emails), "ref_scope": "captured_head_oids",
            "merges": "excluded", "deduplication": "commit_object_id",
            "timestamp": "author_date", "analysis_timezone": analysis_timezone,
            "repos": sorted(str(pathlib.Path(r).resolve()) for r in repos)}


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RepoUnreadable(f"{repo}: git {' '.join(args)} failed: "
                             f"{r.stderr.strip()[:200]}")
    return r.stdout


def _config(repo, key):
    r = subprocess.run(["git", "-C", str(repo), "config", "--get", key],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def check_history_complete(repo):
    """Prove the repository holds full local history, or say why it does not.

    Returns the evidence dict that is persisted with the run. Raises HistoryIncomplete
    when the history is truncated in a way that would turn absent commits into
    measured zeros.
    """
    ev = {"repo": str(repo)}
    ev["shallow"] = _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    ev["promisor"] = bool(_config(repo, "remote.origin.promisor")) or bool(
        _config(repo, "remote.origin.partialclonefilter"))
    git_dir = _git(repo, "rev-parse", "--git-dir").strip()
    root = pathlib.Path(repo) / git_dir if not pathlib.Path(git_dir).is_absolute() \
        else pathlib.Path(git_dir)
    ev["grafts"] = (root / "info" / "grafts").exists()
    ev["shallow_file"] = (root / "shallow").exists()
    replaced = subprocess.run(["git", "-C", str(repo), "replace", "-l"],
                              capture_output=True, text=True, timeout=60)
    ev["replace_refs"] = bool(replaced.returncode == 0 and replaced.stdout.strip())
    bad = [k for k in ("shallow", "promisor", "grafts", "shallow_file", "replace_refs")
           if ev[k]]
    if bad:
        raise HistoryIncomplete(
            f"{repo}: history is not complete ({', '.join(bad)}); this adapter reads "
            f"full local history only and will not fetch to complete it")
    ev["complete"] = True
    return ev


def capture_heads(repos):
    """Ref scope is pinned BEFORE reading, so a concurrent commit cannot half-appear.

    A repository whose HEAD is unborn (no commits yet) is recorded as None: it is
    readable and complete, and it contributes no commits.
    """
    heads = {}
    for repo in repos:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            heads[str(repo)] = r.stdout.strip()
            continue
        # Distinguish "no commits yet" from "not a repository / unreadable".
        probe = subprocess.run(["git", "-C", str(repo), "rev-parse",
                                "--is-inside-work-tree"],
                               capture_output=True, text=True, timeout=60)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            heads[str(repo)] = None
        else:
            raise RepoUnreadable(f"{repo}: git rev-parse HEAD failed: "
                                 f"{r.stderr.strip()[:200]}")
    return heads


def snapshot_id(heads):
    """Names the exact ref scope the values were computed over. Persisted with the run,
    so a later movement of HEAD cannot make a stored value's origin unknowable."""
    return "git:" + hashlib.sha256(
        json.dumps(heads, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def collect(*, repos, author_emails, analysis_timezone, start, end):
    """Returns (per_day_counts, heads, completeness). Raises RepoUnreadable if ANY repo
    fails or holds incomplete history."""
    tz = ZoneInfo(analysis_timezone)
    completeness = [check_history_complete(repo) for repo in repos]
    heads = capture_heads(repos)
    seen, counts = set(), {}
    for repo in repos:
        head = heads[str(repo)]
        if head is None:                    # repository with no commits
            continue
        out = _git(repo, "log", head, "--no-merges", "--pretty=format:%H%x1f%aI%x1f%ae")
        for line in out.splitlines():
            if not line.strip():
                continue
            oid, aiso, email = line.split("\x1f")
            if email not in author_emails:
                continue
            if oid in seen:                     # dedup across clones
                continue
            seen.add(oid)
            day = datetime.fromisoformat(aiso).astimezone(tz).date()
            if start <= day <= end:
                counts[day] = counts.get(day, 0) + 1
    return counts, heads, completeness


def fetch_result(*, repos, author_emails, analysis_timezone, start, end, FetchResult):
    """Adapter → collector boundary. Every day in the window is DETERMINED, because the
    full history was read AND proved complete; days with no qualifying commit are
    structural zeros.

    Returns (FetchResult, provenance). `provenance` is None only when nothing could be
    established. It is the evidence persisted with the run.
    """
    try:
        counts, heads, completeness = collect(
            repos=repos, author_emails=author_emails,
            analysis_timezone=analysis_timezone, start=start, end=end)
    except RepoUnreadable as e:
        return FetchResult(METRIC, "failed", reason=str(e)), None
    observed, d = {}, start
    while d <= end:
        observed[d] = {"value": float(counts.get(d, 0)),
                       "sample_count": counts.get(d, 0), "quality": "measured"}
        d += timedelta(days=1)
    provenance = {"heads": heads, "snapshot_id": snapshot_id(heads),
                  "history_completeness": completeness,
                  "ref_scope": "captured_head_oids",
                  "value_definition": value_definition(
                      author_emails=author_emails, repos=repos,
                      analysis_timezone=analysis_timezone),
                  "window": {"start": start.isoformat(), "end": end.isoformat()}}
    return FetchResult(METRIC, "ok", observed=observed), provenance
