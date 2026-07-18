import time
import uuid

import pytest

from ravens_nest import db, events, replay, store
from ravens_nest.sync import SyncManager, _union_jsonl


class TwoClients:
    """Two clones of one bare 'remote', simulating two machines."""

    def __init__(self, remote, a, b, monkeypatch):
        self.remote = remote
        self.a = a
        self.b = b
        self._monkeypatch = monkeypatch

    def activate(self, client):
        self._monkeypatch.setenv("RAVENS_NEST_DATA", str(client / "data"))

    def manager(self, client, **kwargs):
        self.activate(client)
        kwargs.setdefault("debounce_seconds", 0.2)
        return SyncManager(repo_root=client, **kwargs)


@pytest.fixture
def clients(tmp_path, monkeypatch, run_git):
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))

    a = tmp_path / "client_a"
    run_git(tmp_path, "clone", str(remote), str(a))
    run_git(a, "symbolic-ref", "HEAD", "refs/heads/main")
    (a / ".gitignore").write_text("data/cache.db\n.env\n", encoding="utf-8")
    (a / "data" / "events").mkdir(parents=True)
    (a / "data" / "events" / ".gitkeep").write_text("", encoding="utf-8")
    run_git(a, "add", "-A")
    run_git(a, "commit", "-m", "init")
    run_git(a, "push", "-u", "origin", "main")

    b = tmp_path / "client_b"
    run_git(tmp_path, "clone", str(remote), str(b))
    return TwoClients(remote, a, b, monkeypatch)


def _item_names():
    conn = db.connect()
    names = {row[0] for row in conn.execute("SELECT name FROM items")}
    conn.close()
    return names


def _qty(item_id):
    conn = db.connect()
    qty = conn.execute("SELECT qty_on_hand FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    conn.close()
    return qty

def _event_log_text(client):
    parts = []
    for path in sorted((client / "data" / "events").glob("*.jsonl")):
        parts.append(path.read_text(encoding="utf-8"))
    return "".join(parts)


def _craft(ts, name, item_id):
    return {
        "id": item_id,
        "ts": ts,
        "actor": "test-host",
        "type": "item.created",
        "payload": {"id": item_id, "name": name, "unit_type": "each", "qty_on_hand": "1"},
    }


def _write_and_apply(event):
    events.append_to_log(event)
    conn = db.connect()
    with conn:
        replay.apply_event(conn, event)
    conn.close()


def test_two_clients_converge(clients):
    ma = clients.manager(clients.a)
    conn = db.connect()
    item = store.create_item("M3 screw", "each", qty_on_hand=10, conn=conn)["payload"]["id"]
    conn.close()
    result = ma.sync_now()
    assert result["last_push"]["ok"] is True

    mb = clients.manager(clients.b)
    result = mb.sync_now()
    assert result["last_pull"]["ok"] is True
    assert result["last_apply"] == {"new_events": 1, "full_replay": False}
    assert _item_names() == {"M3 screw"}

    conn = db.connect()
    store.adjust_qty(item, 5, "restock", conn=conn)
    conn.close()
    mb.sync_now()

    clients.activate(clients.a)
    result = ma.sync_now()
    assert result["last_pull"]["ok"] is True
    assert result["last_apply"]["full_replay"] is False
    assert _qty(item) == "15"


def test_offline_divergence_converges_by_union(clients):
    # Both clients write to the same month file with no sync in between,
    # so client B's pull hits a rebase conflict on the .jsonl.
    ma = clients.manager(clients.a)
    conn = db.connect()
    store.create_item("resistor", "each", conn=conn)
    conn.close()

    mb = clients.manager(clients.b)
    conn = db.connect()
    store.create_item("capacitor", "each", conn=conn)
    conn.close()

    clients.activate(clients.a)
    assert ma.sync_now()["last_push"]["ok"] is True

    clients.activate(clients.b)
    result = mb.sync_now()
    assert result["last_pull"]["ok"] is True
    assert "union" in result["last_pull"]["detail"]
    assert result["last_push"]["ok"] is True
    assert _item_names() == {"resistor", "capacitor"}

    clients.activate(clients.a)
    result = ma.sync_now()
    assert result["last_pull"]["ok"] is True
    assert _item_names() == {"resistor", "capacitor"}

    assert _event_log_text(clients.a) == _event_log_text(clients.b)


def test_pull_of_earlier_events_triggers_full_replay(clients):
    late = _craft("2026-07-10T00:00:00+00:00", "capacitor", str(uuid.uuid4()))
    early = _craft("2026-07-01T00:00:00+00:00", "resistor", str(uuid.uuid4()))

    clients.activate(clients.b)
    _write_and_apply(late)

    ma = clients.manager(clients.a)
    _write_and_apply(early)
    assert ma.sync_now()["last_push"]["ok"] is True

    mb = clients.manager(clients.b)
    result = mb.sync_now()
    assert result["last_apply"] == {"new_events": 1, "full_replay": True}
    assert _item_names() == {"resistor", "capacitor"}


def test_no_remote_is_clear_status_not_a_crash(tmp_path, monkeypatch, run_git):
    repo = tmp_path / "solo"
    run_git(tmp_path, "init", "--initial-branch=main", str(repo))
    monkeypatch.setenv("RAVENS_NEST_DATA", str(repo / "data"))
    manager = SyncManager(repo_root=repo, debounce_seconds=0.2)
    conn = db.connect()
    store.create_item("widget", "each", conn=conn)
    conn.close()

    result = manager.sync_now()
    assert result["has_remote"] is False
    assert result["remote_reachable"] is False
    assert result["last_pull"]["ok"] is False
    assert "no remote" in result["last_pull"]["detail"]
    assert result["last_push"]["ok"] is False
    assert result["unpushed_events"] == 1


def test_unreachable_remote_degrades_gracefully(clients, run_git, tmp_path):
    ma = clients.manager(clients.a)
    run_git(clients.a, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    conn = db.connect()
    store.create_item("widget", "each", conn=conn)
    conn.close()

    result = ma.sync_now()
    assert result["has_remote"] is True
    assert result["remote_reachable"] is False
    assert result["last_pull"]["ok"] is False
    assert result["last_push"]["ok"] is False
    # The event is committed locally and still counted as unpushed.
    assert result["unpushed_events"] == 1


def test_dirty_tree_does_not_block_sync(clients, run_git):
    ma = clients.manager(clients.a)
    gitignore = clients.a / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + "# scratch\n", encoding="utf-8")
    (clients.a / "notes.txt").write_text("draft", encoding="utf-8")
    conn = db.connect()
    store.create_item("widget", "each", conn=conn)
    conn.close()

    result = ma.sync_now()
    assert result["last_pull"]["ok"] is True
    assert result["last_push"]["ok"] is True
    # The unrelated dirt is untouched and uncommitted.
    porcelain = run_git(clients.a, "status", "--porcelain")
    assert " M .gitignore" in porcelain
    assert "?? notes.txt" in porcelain


def test_debounced_push_batches_writes_into_one_commit(clients, run_git):
    ma = clients.manager(clients.a, debounce_seconds=0.4)
    store.add_write_listener(ma.on_event_written)
    try:
        before = int(run_git(clients.remote, "rev-list", "--count", "main").strip())
        conn = db.connect()
        e1 = store.create_item("bolt", "each", conn=conn)
        e2 = store.create_item("nut", "each", conn=conn)
        conn.close()

        deadline = time.time() + 10
        after = before
        while time.time() < deadline:
            after = int(run_git(clients.remote, "rev-list", "--count", "main").strip())
            if after > before:
                break
            time.sleep(0.1)
        assert after == before + 1  # both writes in a single commit

        month = e1["ts"][:7]
        remote_file = run_git(clients.remote, "show", f"main:data/events/{month}.jsonl")
        assert e1["id"] in remote_file
        assert e2["id"] in remote_file
    finally:
        store.remove_write_listener(ma.on_event_written)
        ma.stop()


def test_status_reports_unpushed_count(clients):
    ma = clients.manager(clients.a)
    conn = db.connect()
    for i in range(3):
        store.create_item(f"part-{i}", "each", conn=conn)
    conn.close()

    status = ma.status_dict()
    assert status["has_remote"] is True
    assert status["remote_reachable"] is True
    assert status["unpushed_events"] == 3

    ma.sync_now()
    assert ma.status_dict()["unpushed_events"] == 0


def test_union_merge_is_sorted_and_deduplicated():
    line_a = '{"id":"a","ts":"2026-07-02T00:00:00+00:00","type":"x","payload":{}}'
    line_b = '{"id":"b","ts":"2026-07-01T00:00:00+00:00","type":"x","payload":{}}'
    shared = '{"id":"s","ts":"2026-06-01T00:00:00+00:00","type":"x","payload":{}}'
    merged = _union_jsonl(shared + "\n" + line_a + "\n", shared + "\n" + line_b + "\n")
    assert merged.splitlines() == [shared, line_b, line_a]
