"""Git-based sync for the event log.

The event log lives in a Git repository; each machine commits its
appended events and pulls the others' with `git pull --rebase`. All git
operations shell out to the `git` CLI and every failure mode (no remote,
no network, dirty tree, even a .jsonl merge conflict) is recorded in the
sync status instead of raised.

`python -m ravens_nest.sync` runs one full sync and prints the status.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db, events, replay

log = logging.getLogger(__name__)

# Sync commits are made by the app, not the user, so pin an identity and
# skip signing — a GPG prompt would hang a background flush.
_GIT_FLAGS = [
    "-c",
    "user.name=Raven's Nest Sync",
    "-c",
    "user.email=sync@ravens-nest.local",
    "-c",
    "commit.gpgsign=false",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _union_jsonl(ours: str, theirs: str) -> str:
    """Merge two versions of an event log file: union of the lines, sorted
    by (ts, id) so every machine converges on identical file content."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in ours.splitlines() + theirs.splitlines():
        line = raw.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    def sort_key(line: str):
        try:
            event = json.loads(line)
            return (0, str(event["ts"]), str(event["id"]))
        except Exception:
            return (1, line, "")

    lines.sort(key=sort_key)
    return "\n".join(lines) + "\n" if lines else ""


@dataclass
class OpResult:
    ts: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "ok": self.ok, "detail": self.detail}


class SyncManager:
    """Owns all git operations for one repository checkout.

    Public methods never raise — failures land in the status dict."""

    def __init__(
        self,
        repo_root: Path | str | None = None,
        remote: str = "origin",
        debounce_seconds: float | None = None,
    ):
        self.repo_root = Path(repo_root) if repo_root is not None else config.repo_root()
        self.remote = remote
        if debounce_seconds is None:
            debounce_seconds = float(os.environ.get("RAVENS_NEST_DEBOUNCE", "10"))
        self.debounce_seconds = debounce_seconds
        self.last_pull: OpResult | None = None
        self.last_push: OpResult | None = None
        self.last_apply: dict[str, Any] | None = None
        self.last_error: str | None = None
        # The shared event-file lock (audit C1): every sync operation that
        # can rewrite an event file must exclude append_to_log, so this IS
        # events._write_lock rather than a private lock.
        self._lock = events._write_lock
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    # ------------------------------------------------------------- plumbing

    def _git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true")
        return subprocess.run(
            ["git", *_GIT_FLAGS, *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )

    def _data_specs(self) -> list[str]:
        """Git pathspecs for the data directories this app owns."""
        specs = []
        for p in (config.events_dir(), config.assets_dir()):
            if p.exists():
                rel = os.path.relpath(p, self.repo_root).replace("\\", "/")
                if not rel.startswith(".."):
                    specs.append(rel)
        return specs

    def _events_spec(self) -> str | None:
        rel = os.path.relpath(config.events_dir(), self.repo_root).replace("\\", "/")
        return None if rel.startswith("..") else rel

    def has_remote(self) -> bool:
        try:
            return self._git("remote", "get-url", self.remote).returncode == 0
        except Exception:
            return False

    def remote_reachable(self) -> bool:
        if not self.has_remote():
            return False
        try:
            return self._git("ls-remote", self.remote, "HEAD", timeout=10).returncode == 0
        except Exception:
            return False

    def _current_branch(self) -> str | None:
        proc = self._git("symbolic-ref", "--short", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else None

    def _has_commits(self) -> bool:
        return self._git("rev-parse", "--verify", "-q", "HEAD").returncode == 0

    def _rebase_in_progress(self) -> bool:
        for name in ("rebase-merge", "rebase-apply"):
            proc = self._git("rev-parse", "--git-path", name)
            if proc.returncode == 0 and (self.repo_root / proc.stdout.strip()).exists():
                return True
        return False

    # ------------------------------------------------------------ operations

    def commit_pending(self) -> bool:
        """Stage and commit local event/asset changes. True if a commit was made.

        Run before every pull: it is also the dirty-tree handling, since the
        rebase then starts from a checkout that is clean for the data paths."""
        with self._lock:
            specs = self._data_specs()
            if not specs:
                return False
            self._git("add", "-A", "--", *specs)
            if self._git("diff", "--cached", "--quiet").returncode == 0:
                return False
            count = self._staged_event_count()
            what = f"{count} event(s)" if count else "data"
            proc = self._git("commit", "-m", f"sync: {what} from {socket.gethostname()}")
            if proc.returncode != 0:
                self.last_error = _short(proc.stderr or proc.stdout)
                return False
            return True

    def _staged_event_count(self) -> int:
        spec = self._events_spec()
        if spec is None:
            return 0
        proc = self._git("diff", "--cached", "--numstat", "--", spec)
        total = 0
        for line in proc.stdout.splitlines():
            added = line.split("\t")[0]
            if added.isdigit():
                total += int(added)
        return total

    def push(self) -> OpResult:
        with self._lock:
            if not self.has_remote():
                res = OpResult(_now(), False, "no remote configured")
            elif not self._has_commits():
                res = OpResult(_now(), True, "nothing to push (no commits yet)")
            elif (branch := self._current_branch()) is None:
                res = OpResult(_now(), False, "detached HEAD; cannot push")
            else:
                try:
                    proc = self._git("push", "-u", self.remote, branch, timeout=120)
                    ok = proc.returncode == 0
                    res = OpResult(_now(), ok, "pushed" if ok else _short(proc.stderr or proc.stdout))
                except subprocess.TimeoutExpired:
                    res = OpResult(_now(), False, "push timed out")
            self.last_push = res
            return res

    def pull(self) -> OpResult:
        with self._lock:
            res = self._pull_inner()
            self.last_pull = res
            return res

    def _pull_inner(self) -> OpResult:
        if not self.has_remote():
            return OpResult(_now(), False, "no remote configured")
        branch = self._current_branch()
        if branch is None:
            return OpResult(_now(), False, "detached HEAD; cannot pull")
        try:
            proc = self._git("pull", "--rebase", "--autostash", self.remote, branch, timeout=120)
        except subprocess.TimeoutExpired:
            return OpResult(_now(), False, "pull timed out")
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            detail = "already up to date" if "up to date" in out.lower() else "pulled"
            if "Applying autostash resulted in conflicts" in out:
                detail += "; non-event local changes left in git stash"
            return OpResult(_now(), True, detail)
        if "couldn't find remote ref" in out:
            return OpResult(_now(), True, "remote has no branch yet")
        if self._rebase_in_progress():
            if self._resolve_event_log_conflicts():
                return OpResult(_now(), True, "pulled; merged event logs by union")
            self._git("rebase", "--abort")
            return OpResult(_now(), False, "merge conflict not auto-resolvable: " + _short(out))
        return OpResult(_now(), False, _short(out))

    def _resolve_event_log_conflicts(self) -> bool:
        """Resolve rebase conflicts on .jsonl event logs by taking the union
        of lines. Any conflict on another file aborts (returns False)."""
        for _ in range(100):  # one iteration per conflicted commit
            if not self._rebase_in_progress():
                return True
            proc = self._git("diff", "--name-only", "--diff-filter=U")
            conflicted = [f.strip() for f in proc.stdout.splitlines() if f.strip()]
            for path in conflicted:
                if not path.endswith(".jsonl"):
                    return False
                ours = self._git("show", f":2:{path}")
                theirs = self._git("show", f":3:{path}")
                merged = _union_jsonl(
                    ours.stdout if ours.returncode == 0 else "",
                    theirs.stdout if theirs.returncode == 0 else "",
                )
                target = self.repo_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8", newline="\n") as f:
                    f.write(merged)
                self._git("add", "--", path)
            cont = self._git("rebase", "--continue")
            if cont.returncode != 0 and self._rebase_in_progress():
                # The union can equal the upstream version, leaving an empty
                # commit that --continue refuses; skip it.
                still = self._git("diff", "--name-only", "--diff-filter=U").stdout.strip()
                if not still:
                    self._git("rebase", "--skip")
        return not self._rebase_in_progress()

    def _apply_new_events(self) -> dict[str, Any]:
        """Bring cache.db up to date with the log. Incremental when every new
        event sorts after everything already applied; otherwise a pulled event
        lands in the middle of history, so rebuild from scratch."""
        all_events = events.read_all_events()
        conn = db.connect()
        try:
            applied_ids = {row[0] for row in conn.execute("SELECT event_id FROM events_applied")}
            new = [e for e in all_events if e["id"] not in applied_ids]
            if not new:
                info = {"new_events": 0, "full_replay": False}
            else:
                applied = [e for e in all_events if e["id"] in applied_ids]
                needs_full = bool(applied) and min(
                    (e["ts"], e["id"]) for e in new
                ) < max((e["ts"], e["id"]) for e in applied)
                if needs_full:
                    conn.close()
                    conn = None
                    replay.rebuild()
                else:
                    with conn:
                        for event in new:
                            replay.apply_event(conn, event)
                info = {"new_events": len(new), "full_replay": needs_full}
        finally:
            if conn is not None:
                conn.close()
        self.last_apply = info
        return info

    def startup(self) -> dict[str, Any]:
        """Startup sync: pull --rebase, then replay new events into the cache."""
        with self._lock:
            try:
                self.commit_pending()
                self.pull()
                self._apply_new_events()
            except Exception as exc:
                log.exception("sync startup failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
            return self.status_dict(check_remote=False)

    def sync_now(self) -> dict[str, Any]:
        """Full manual sync: commit, pull+apply, push. Never raises."""
        with self._lock:
            self._cancel_timer()
            try:
                self.commit_pending()
                self.pull()
                self._apply_new_events()
                self.push()
            except Exception as exc:
                log.exception("sync failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
            return self.status_dict()

    # -------------------------------------------------------- debounced push

    def on_event_written(self, _event: dict[str, Any] | None = None) -> None:
        """Store write listener. The first write starts the debounce window;
        everything written before it fires lands in one commit."""
        with self._timer_lock:
            if self._timer is not None and self._timer.is_alive():
                return
            timer = threading.Timer(self.debounce_seconds, self._flush_from_timer)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _flush_from_timer(self) -> None:
        try:
            self.flush()
        except Exception:
            log.exception("debounced sync flush failed")

    def flush(self) -> None:
        """Commit and push pending events immediately."""
        with self._lock:
            try:
                committed = self.commit_pending()
                if committed or (self.unpushed_event_count() or 0) > 0:
                    self.push()
            except Exception as exc:
                log.exception("sync flush failed")
                self.last_error = f"{type(exc).__name__}: {exc}"

    def _cancel_timer(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def stop(self) -> None:
        """Cancel the debounce timer and flush anything still pending."""
        self._cancel_timer()
        self.flush()

    # ---------------------------------------------------------------- status

    def unpushed_event_count(self) -> int | None:
        """Events written locally that the remote does not have yet."""
        try:
            spec = self._events_spec()
            if spec is None:
                return None
            total = 0
            if self._git("rev-parse", "--abbrev-ref", "@{u}").returncode == 0:
                # Upstream vs worktree covers committed-but-unpushed and
                # uncommitted changes in one diff; untracked files are extra.
                proc = self._git("diff", "--numstat", "@{u}", "--", spec)
                for line in proc.stdout.splitlines():
                    added = line.split("\t")[0]
                    if added.isdigit():
                        total += int(added)
                proc = self._git("ls-files", "--others", "--exclude-standard", "--", spec)
                for rel in proc.stdout.splitlines():
                    if rel.strip():
                        total += _count_lines(self.repo_root / rel.strip())
            else:
                # No upstream: every event is unpushed.
                directory = config.events_dir()
                if directory.is_dir():
                    for path in sorted(directory.glob("*.jsonl")):
                        total += _count_lines(path)
            return total
        except Exception:
            log.exception("unpushed event count failed")
            return None

    def status_dict(self, check_remote: bool = True) -> dict[str, Any]:
        has_remote = self.has_remote()
        reachable = None
        if check_remote:
            reachable = self.remote_reachable() if has_remote else False
        with self._timer_lock:
            flush_pending = self._timer is not None and self._timer.is_alive()
        return {
            "has_remote": has_remote,
            "remote_reachable": reachable,
            "last_pull": self.last_pull.as_dict() if self.last_pull else None,
            "last_push": self.last_push.as_dict() if self.last_push else None,
            "last_apply": self.last_apply,
            "unpushed_events": self.unpushed_event_count(),
            "flush_pending": flush_pending,
            "last_error": self.last_error,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manager = SyncManager()
    print(json.dumps(manager.sync_now(), indent=2))


if __name__ == "__main__":
    main()
