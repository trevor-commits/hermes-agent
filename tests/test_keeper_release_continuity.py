from types import SimpleNamespace

from agent.turn_context import CURRENT_TURN_IDENTITY_KEY, export_current_turn_boundary
from hermes_state import SessionDB


def test_export_uses_current_identity_and_never_relabels_history():
    agent = SimpleNamespace(_current_turn_id='new-turn', _persist_user_turn_identity='current')
    history = [{'role': 'user', 'content': 'same ask', CURRENT_TURN_IDENTITY_KEY: 'old'}]
    result = export_current_turn_boundary(agent, {'messages': history}, 'same ask')
    assert 'turn_id' not in result and 'current_turn_user_idx' not in result
    history.append({'role': 'user', 'content': 'same ask', CURRENT_TURN_IDENTITY_KEY: 'current'})
    result = export_current_turn_boundary(agent, {'messages': history}, 'same ask')
    assert result['current_turn_user_idx'] == 1
    assert result['turn_id'] == 'new-turn'


def test_host_reopen_preserves_rollover_boundary(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('rollover', 'cli')
        db.end_session('rollover', 'proactive_rollover')
        assert db.reopen_if_explicitly_closed('rollover', provenance='test') is None
        assert db.get_session('rollover')['end_reason'] == 'proactive_rollover'
        db.create_session('closed', 'cli')
        db.end_session('closed', 'tui_close')
        assert db.reopen_if_explicitly_closed('closed', provenance='test') == 'tui_close'
        assert db.get_session('closed')['ended_at'] is None
    finally:
        db.close()


def test_malformed_continuation_metadata_is_ignored_without_losing_valid_child(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('parent', 'cli')
        db.end_session('parent', 'proactive_rollover')
        db.create_session('bad', 'cli', parent_session_id='parent')
        db._conn.execute("UPDATE sessions SET model_config = '{bad' WHERE id = 'bad'")
        db._conn.commit()
        assert db.find_live_compression_child('parent') is None
        db.create_session('good', 'cli', parent_session_id='parent',
                          model_config={'_proactive_rollover': 1, '_reset_from': 'parent'})
        assert db.find_live_compression_child('parent')['id'] == 'good'
        db.create_session('bad', 'cli', model_config={'model': 'synthetic'})
        assert db.get_session('bad')['id'] == 'bad'
        # The bounded reader must also tolerate the malformed child on a compression edge.
        db._conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 'parent'")
        db._conn.commit()
        assert isinstance(db.list_recent_sessions_bounded(), list)
    finally:
        db.close()


def test_reopen_preserves_legacy_reset_edge_with_malformed_metadata(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('parent', 'cli', session_key='test-key')
        db.end_session('parent', 'session_reset')
        db.create_session('child', 'cli', parent_session_id='parent', session_key='test-key')
        db._conn.execute("UPDATE sessions SET model_config = '{bad' WHERE id = 'child'")
        db._conn.commit()
        db.reopen_session('parent')
        assert db.get_session('parent')['ended_at'] is None
        import json
        assert json.loads(db.get_session('child')['model_config'])['_reset_from'] == 'parent'
    finally:
        db.close()
