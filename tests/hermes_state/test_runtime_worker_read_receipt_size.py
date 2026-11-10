"""A running worker's READ receipts keep no full copy of the session row.

Every managed turn reads the session once (``_restore_or_build_system_prompt`` ->
``get_session`` -> ``compression.context``) and that read's result is a full session
row: the resolved system prompt plus the tool-name schema. Stored verbatim in
``worker_receipts``, one copy per read, the row was retained for the whole turn:
measured 113 KB per read, 614 reads on one managed turn, 67 MB before the execution
went terminal. The read is a pure projection of durable state, so the receipt keeps
only the fact that the read happened and re-derives the payload on an exact retry.
"""
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution


def _worker_scope(db, session_id, execution_id='worker'):
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = {'epoch': epoch, 'execution_id': execution_id, 'session_id': session_id, 'generation': 0}
    register_worker_execution(db, epoch=epoch, execution_id=execution_id, session_id=session_id,
                              generation=0, kind='compute', adoption_secret='secret')
    return scope


def test_running_worker_keeps_no_full_session_row_in_a_read_receipt(tmp_path):
    """A read receipt stores a marker, not the session row it read."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', system_prompt='P' * 5000)
        scope = _worker_scope(db, 's')

        result = mutate_worker_execution(db, **scope, sequence=1, operation='compression.context',
                                         payload={'target': 's'})
        # The CALLER still gets the real payload.
        assert result['session']['system_prompt'] == 'P' * 5000

        stored = db._read_all('SELECT LENGTH(result_json) FROM worker_receipts WHERE sequence=1')
        assert stored[0][0] < 200, 'a live read receipt kept the full session row'
    finally:
        db.close()


def test_an_exact_retry_of_a_compacted_read_returns_the_real_payload(tmp_path):
    """The retry contract is unchanged: the same (execution, sequence, digest) still
    returns the same payload, re-derived from the live session row."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', system_prompt='P' * 5000)
        scope = _worker_scope(db, 's')

        first = mutate_worker_execution(db, **scope, sequence=1, operation='compression.context',
                                        payload={'target': 's'})
        retry = mutate_worker_execution(db, **scope, sequence=1, operation='compression.context',
                                        payload={'target': 's'})
        assert retry == first
        assert retry['session']['system_prompt'] == 'P' * 5000
    finally:
        db.close()


def test_a_mutation_receipt_keeps_its_acknowledgement(tmp_path):
    """A mutation's result is an acknowledgement, not a projection: nothing can
    re-derive it, so the receipt keeps it verbatim."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli')
        scope = _worker_scope(db, 's')

        mutate_worker_execution(db, **scope, sequence=1, operation='session.activity',
                                payload={'ts': None, 'description': 'working', 'provenance': 'test'})
        stored = db._read_all('SELECT result_json FROM worker_receipts WHERE sequence=1')
        assert 'read' not in stored[0][0], 'a mutation receipt was compacted'
    finally:
        db.close()


def test_repeated_reads_do_not_grow_the_receipt_table(tmp_path):
    """The growth law: one read per turn must not accumulate one session row per turn."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', system_prompt='P' * 5000)
        scope = _worker_scope(db, 's')

        for seq in range(1, 21):
            mutate_worker_execution(db, **scope, sequence=seq, operation='compression.context',
                                    payload={'target': 's'})
        sizes = db._read_all('SELECT LENGTH(result_json) FROM worker_receipts')
        assert len(sizes) == 20
        assert max(row[0] for row in sizes) < 200, 'a read receipt grew with the turn'
    finally:
        db.close()
