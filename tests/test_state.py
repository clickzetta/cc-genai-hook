from cc_genai_hook.ids import span_id, trace_id
from cc_genai_hook.state import StateStore


def test_segments_messages_and_idempotency(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    try:
        store.ensure_session("s1", "/tmp/t.jsonl", "2.1.311", "/tmp")
        tid = trace_id("s1", "p1")
        seq, root_sid = store.begin_segment("s1", "p1", tid, 1)
        assert seq == 0
        assert root_sid == span_id("invoke_agent", "s1:p1:0")
        seq2, root_sid2 = store.begin_segment("s1", "p1", tid, 2)
        assert seq2 == 1
        assert root_sid2 == span_id("invoke_agent", "s1:p1:1")
        store.mark_messages("s1", [("m1", "p1", span_id("chat", "m1"))])
        store.mark_messages("s1", [("m1", "p1", span_id("chat", "m1"))])
        assert store.emitted_message_ids("s1") == {"m1"}
        assert store.counts() == (1, 2, 1)
    finally:
        store.close()


def test_tool_idempotency(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    try:
        tool_sid = span_id("execute_tool", "t1")
        assert store.tool_emitted("s1", "t1") is False
        store.mark_tool("s1", "t1", "p1", tool_sid)
        store.mark_tool("s1", "t1", "p1", tool_sid)
        assert store.tool_emitted("s1", "t1") is True
        assert store.tool_count() == 1
    finally:
        store.close()


def test_seed_marks_all_messages(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    try:
        store.mark_all_messages("s1", [("m1", "p1"), ("m2", "p1")])
        assert store.emitted_message_ids("s1") == {"m1", "m2"}
    finally:
        store.close()


def _session_with_rows(store, session_id):
    store.ensure_session(session_id, "/tmp/%s.jsonl" % session_id)
    store.begin_segment(session_id, "p1", trace_id(session_id, "p1"), 1)
    store.mark_messages(session_id, [("m1", "p1", span_id("chat", "m1"))])
    store.mark_tool(session_id, "t1", "p1", span_id("execute_tool", "t1"))


def test_prune_expired_removes_idle_sessions_and_rows(tmp_path):
    import datetime

    store = StateStore(str(tmp_path / "state"))
    try:
        _session_with_rows(store, "old")
        _session_with_rows(store, "new")
        old_stamp = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)
        ).isoformat()
        with store._conn:
            store._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE session_id='old'", (old_stamp,)
            )

        assert store.prune_expired(14) == 1
        assert store.counts() == (1, 1, 1)
        assert store.tool_count() == 1
        assert store.emitted_message_ids("old") == set()
        assert store.emitted_message_ids("new") == {"m1"}
        assert store.tool_emitted("new", "t1") is True
    finally:
        store.close()


def test_prune_disabled_with_zero_ttl(tmp_path):
    import datetime

    store = StateStore(str(tmp_path / "state"))
    try:
        _session_with_rows(store, "s1")
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        assert store.prune_expired(0, now=future) == 0
        assert store.maybe_prune(0, now=future) is None
        assert store.counts() == (1, 1, 1)
    finally:
        store.close()


def test_maybe_prune_is_throttled(tmp_path):
    import datetime

    store = StateStore(str(tmp_path / "state"))
    try:
        _session_with_rows(store, "s1")
        now = datetime.datetime.now(datetime.timezone.utc)
        assert store.maybe_prune(14, now=now) == 0
        # Within the interval: skipped even though the session has expired.
        later = now + datetime.timedelta(days=15)
        assert store.maybe_prune(14, interval_s=30 * 86400, now=later) is None
        assert store.counts()[0] == 1
        # Past the interval: runs and removes it.
        assert store.maybe_prune(14, now=later) == 1
        assert store.counts() == (0, 0, 0)
        assert store.tool_count() == 0
    finally:
        store.close()
