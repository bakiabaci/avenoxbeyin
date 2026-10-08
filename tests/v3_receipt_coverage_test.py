"""Receipt coverage ratio tests (Issue #78)."""
from datetime import datetime, timezone
from unittest.mock import patch
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_entry
import beyin_v3_hook as hook
from beyin_v3_projections import receipt_coverage, _checkpoint_schema
from beyin_v3_sync import SyncEngine


class ReceiptCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.state.mkdir()

    def _enqueue(self, event, session_id, at=None, harness='claude'):
        queue_dir = self.state / 'hook-queue'
        queue_dir.mkdir(parents=True, exist_ok=True)
        t = time.time() if at is None else at
        payload = {
            'harness': harness,
            'event': event,
            'session': session_id,
            'at': t,
        }
        item_path = queue_dir / f"{int(t * 1000)}-{os.urandom(4).hex()}.json"
        item_path.write_text(json.dumps(payload), encoding='utf-8')

    def test_coverage_counts_sessions_with_user_prompt_submit(self):
        """Only sessions with at least one UserPromptSubmit are counted in coverage (Issue #78)."""
        engine = SyncEngine(self.vault, self.state)

        # 1. Interactive Session 1: Has UserPromptSubmit + has a receipt
        now = time.time()
        self._enqueue('SessionStart', 'sess_interactive_1', at=now - 100)
        self._enqueue('UserPromptSubmit', 'sess_interactive_1', at=now - 90)
        self._enqueue('Stop', 'sess_interactive_1', at=now - 80)

        # Drain to create checkpoint
        hook.drain_queue(self.vault, self.state)
        gaps_data = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        sess1_hashed = gaps_data['checkpoints'][0]['session']

        # Add matching receipt for Session 1
        engine.note_create('notes/work.md', 'Work completed.', {'id': 'work-1'})
        engine.receipt('evt_1', 'Completed work', ['notes/work.md'], 'claude', session=sess1_hashed)
        engine.sync()

        # 2. Interactive Session 2: Has UserPromptSubmit but NO receipt
        self._enqueue('SessionStart', 'sess_interactive_2', at=now - 50)
        self._enqueue('UserPromptSubmit', 'sess_interactive_2', at=now - 40)
        self._enqueue('Stop', 'sess_interactive_2', at=now - 30)

        # 3. Non-interactive Session 3: SessionStart + Stop only (NO UserPromptSubmit)
        self._enqueue('SessionStart', 'sess_empty_3', at=now - 20)
        self._enqueue('Stop', 'sess_empty_3', at=now - 10)

        hook.drain_queue(self.vault, self.state)

        # Verify receipt-gaps.json
        gaps_file = self.state / 'receipt-gaps.json'
        self.assertTrue(gaps_file.exists())
        data = json.loads(gaps_file.read_text(encoding='utf-8'))

        coverage = data.get('receipt_coverage')
        self.assertIsNotNone(coverage)
        # Empty session 3 without UserPromptSubmit must NOT be in coverage!
        self.assertEqual(coverage['total'], 2, "Total should count only sessions with UserPromptSubmit")
        self.assertEqual(coverage['covered'], 1, "Only sess_interactive_1 has a receipt")
        self.assertEqual(coverage['missing'], 1, "sess_interactive_2 is missing a receipt")
        self.assertEqual(coverage['ratio'], 0.5)

    def test_coverage_zero_sessions_returns_none_ratio(self):
        """When there are zero sessions with user prompts, ratio is None."""
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=time.time())
        self.assertEqual(cov['total'], 0)
        self.assertEqual(cov['covered'], 0)
        self.assertEqual(cov['missing'], 0)
        self.assertIsNone(cov['ratio'])
        self.assertIsNone(cov['last_7d']['ratio'])
        self.assertIsNone(cov['last_30d']['ratio'])

    def test_coverage_time_windows_measured_from_reference_now(self):
        """7-day and 30-day windows must be measured relative to the reference timestamp."""
        engine = SyncEngine(self.vault, self.state)
        now = 1800000000.0  # Fixed reference point

        # Session A: 3 days ago (within 7d, within 30d, all_time)
        t_a = now - 3 * 86400
        self._enqueue('SessionStart', 'sess_a', at=t_a)
        self._enqueue('UserPromptSubmit', 'sess_a', at=t_a + 1)
        self._enqueue('Stop', 'sess_a', at=t_a + 2)

        # Session B: 15 days ago (outside 7d, within 30d, all_time)
        t_b = now - 15 * 86400
        self._enqueue('SessionStart', 'sess_b', at=t_b)
        self._enqueue('UserPromptSubmit', 'sess_b', at=t_b + 1)
        self._enqueue('Stop', 'sess_b', at=t_b + 2)

        # Session C: 45 days ago (outside 7d, outside 30d, all_time)
        t_c = now - 45 * 86400
        self._enqueue('SessionStart', 'sess_c', at=t_c)
        self._enqueue('UserPromptSubmit', 'sess_c', at=t_c + 1)
        self._enqueue('Stop', 'sess_c', at=t_c + 2)

        hook.drain_queue(self.vault, self.state)

        with engine.store._connect() as db:
            cov = receipt_coverage(db, now=now)

        self.assertEqual(cov['total'], 3)
        self.assertEqual(cov['last_30d']['total'], 2)  # A and B
        self.assertEqual(cov['last_7d']['total'], 1)   # A only

    def test_doctor_reports_none_for_missing_gaps_file(self):
        """Review Point 6: doctor must report potential_missing_receipts=None when receipt-gaps.json does not exist."""
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout)
        self.assertIsNone(doc.get('potential_missing_receipts'))
        self.assertIsNone(doc.get('receipt_coverage'))
        # Doctor reads through its own store; it must not build a SyncEngine and its journal.
        self.assertFalse((self.state / 'markdown-journal').exists())

    def test_doctor_dynamic_coverage_and_human_output(self):
        """Review Point 5 & Human Rendering: doctor computes coverage dynamically at invocation time."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()

        self._enqueue('SessionStart', 'doc_sess_1', at=now - 50)
        self._enqueue('UserPromptSubmit', 'doc_sess_1', at=now - 40)
        self._enqueue('Stop', 'doc_sess_1', at=now - 30)

        hook.drain_queue(self.vault, self.state)
        gaps_data = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        sess1_hashed = gaps_data['checkpoints'][0]['session']

        engine.note_create('notes/doc.md', 'Doctor test note.', {'id': 'doc-1'})
        engine.receipt('evt_doc', 'Doctor receipt', ['notes/doc.md'], 'claude', session=sess1_hashed)
        engine.sync()

        # Add second session without receipt
        self._enqueue('SessionStart', 'doc_sess_2', at=now - 20)
        self._enqueue('UserPromptSubmit', 'doc_sess_2', at=now - 10)
        self._enqueue('Stop', 'doc_sess_2', at=now)
        hook.drain_queue(self.vault, self.state)

        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        doc = json.loads(proc.stdout)

        self.assertEqual(doc.get('potential_missing_receipts'), 1)
        cov = doc.get('receipt_coverage')
        self.assertIsNotNone(cov)
        self.assertEqual(cov['total'], 2)
        self.assertEqual(cov['covered'], 1)
        self.assertEqual(cov['ratio'], 0.5)

        # Human rendering test
        human_text = beyin_entry.human_result(doc, 'doctor')
        self.assertIn('Makbuz kapsami: %50 (1/2 oturum, son 7 gun: %50)', human_text)

    def test_first_prompt_delivered_as_session_start_counts(self):
        """Hermes and OpenCode send the first user prompt as SessionStart; a one-prompt session still counts."""
        flows = {'opencode': ('SessionStart', 'Stop'), 'hermes': ('SessionStart', 'SessionEnd'),
                 'antigravity': ('SessionStart', 'SessionEnd')}
        for harness, (start, end) in flows.items():
            payload = {'hook_event_name': start, 'session_id': 'one-' + harness, 'event_id': harness + '-start'}
            if harness != 'antigravity':
                payload['prompt'] = 'tek istem'
            hook.enqueue_event(self.vault, self.state, payload, harness)
            hook.enqueue_event(self.vault, self.state, {'hook_event_name': end, 'session_id': 'one-' + harness,
                                                        'event_id': harness + '-end'}, harness)
        queued = [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-queue').glob('*.json')]
        self.assertFalse(any('prompt' in item for item in queued))
        hook.drain_queue(self.vault, self.state)
        with SyncEngine(self.vault, self.state).store._connect() as db:
            cov = receipt_coverage(db, now=time.time())
        self.assertEqual(cov['total'], 2)
        self.assertEqual(cov['missing'], 2)

    def test_unreadable_receipt_created_at_is_skipped_not_fatal(self):
        """A receipt row with a missing, null or malformed created_at never covers a session and never breaks sync or doctor."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        for session in ('sess_ok', 'sess_bad'):
            self._enqueue('UserPromptSubmit', session, at=now - 60)
            self._enqueue('Stop', session, at=now - 50)
        hook.drain_queue(self.vault, self.state)
        later = datetime.fromtimestamp(now - 10, timezone.utc).isoformat().replace('+00:00', 'Z')
        rows = {'ok': {'session': 'sess_ok', 'created_at': later},
                'null': {'session': 'sess_bad', 'created_at': None},
                'missing': {'session': 'sess_bad'},
                'malformed': {'session': 'sess_bad', 'created_at': '2026-13-45T00:00:00+00:00'}}
        with engine.store._connect() as db:
            for event_id, fields in rows.items():
                receipt = dict(fields, event_id=event_id, summary='s', refs=['notes/x.md'], harness='claude')
                db.execute('INSERT INTO receipts VALUES (?,?)', (event_id, json.dumps(receipt)))
        self.assertNotEqual(engine.sync()['status'], 'conflict')
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual([item['session'] for item in gaps['checkpoints']], ['sess_bad'])
        self.assertEqual((gaps['receipt_coverage']['covered'], gaps['receipt_coverage']['total']), (1, 2))
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        doc = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(doc['potential_missing_receipts'], 1)
        self.assertEqual((doc['receipt_coverage']['covered'], doc['receipt_coverage']['missing']), (1, 1))

    def test_schema_migration_adds_prompt_at_cleanly(self):
        """Legacy receipt_checkpoints table without prompt_at column is migrated without errors."""
        engine = SyncEngine(self.vault, self.state)
        with engine.store._connect() as db:
            db.execute('DROP TABLE IF EXISTS receipt_checkpoints')
            db.execute('CREATE TABLE receipt_checkpoints(harness TEXT, session TEXT, at REAL, turn_at REAL DEFAULT 0, PRIMARY KEY(harness,session))')
            db.execute("INSERT INTO receipt_checkpoints VALUES ('claude', 'legacy_sess', 100.0, 50.0)")

        # Calling _checkpoint_schema or sync should migrate schema
        with engine.store._connect() as db:
            _checkpoint_schema(db)
            cols = {row[1] for row in db.execute('PRAGMA table_info(receipt_checkpoints)')}
            self.assertIn('prompt_at', cols)

    def test_receipt_after_prompt_at_covers_session_despite_late_conversational_turn(self):
        """Late conversational turns (e.g. 'thanks', 'status') after receipt was written do not un-cover the session (Issue #212)."""
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        sess_id = 'sess_late_turn'

        # 1. User starts prompt at now - 100
        self._enqueue('SessionStart', sess_id, at=now - 100)
        self._enqueue('UserPromptSubmit', sess_id, at=now - 90)

        # 2. Receipt generated at now - 50 (after prompt_at, before late turn)
        engine.note_create('notes/task.md', 'Work completed.', {'id': 'task-1'})
        # Stamp the authoritative Markdown and SQLite together; sync must not restore
        # a newer on-disk timestamp and make the old threshold falsely pass.
        receipt_time = datetime.fromtimestamp(now - 50, timezone.utc)
        with patch('beyin_v3_sync.datetime', wraps=datetime) as clock:
            clock.now.return_value = receipt_time
            engine.receipt('evt_task', 'Completed work', ['notes/task.md'], 'claude', session=sess_id)
        engine.sync()
        with engine.store._connect() as db:
            event = json.loads(db.execute("SELECT payload FROM receipts WHERE id='evt_task'").fetchone()[0])
        # datetime serializes microseconds; time.time() may carry finer precision.
        self.assertAlmostEqual(datetime.fromisoformat(event['created_at']).timestamp(), now - 50, delta=0.000001)

        # 3. User says "thanks!" at now - 20 (turn_at becomes now - 20, later than receipt created_at)
        self._enqueue('UserPromptSubmit', sess_id, at=now - 20)
        self._enqueue('Stop', sess_id, at=now - 10)
        hook.drain_queue(self.vault, self.state)

        # 4. Coverage and gaps must still mark this session as COVERED because receipt >= prompt_at
        gaps_file = self.state / 'receipt-gaps.json'
        data = json.loads(gaps_file.read_text(encoding='utf-8'))
        coverage = data['receipt_coverage']
        self.assertEqual(coverage['total'], 1)
        self.assertEqual(coverage['covered'], 1)
        self.assertEqual(coverage['missing'], 0)
        self.assertEqual(data['potential_missing_receipts'], 0)

    def test_stop_reminder_recognizes_500_knowledge_and_bash_vault_filter(self):
        """🧠 500-Knowledge/ counts as distilled note (Issue #197), and Bash outside vault does not trigger reminder (Issue #212)."""
        # 1. Check _is_distilled_note
        self.assertTrue(hook._is_distilled_note('🧠 500-Knowledge/AI/Agents.md'))
        self.assertTrue(hook._is_distilled_note('knowledge/concepts/agent.md'))
        self.assertFalse(hook._is_distilled_note('🧠 500-Knowledge/v3/notes.md'))
        self.assertFalse(hook._is_distilled_note('tasks/todo.md'))

        # 2. Check receipt_reminder tool filter for Bash outside vault
        sess = 'sess_bash_test'
        # Bash call targeting a file OUTSIDE the vault
        payload_outside = {
            'session_id': sess,
            'toolName': 'Bash',
            'tool_input': {'command': 'cat /tmp/other_file.txt', 'file_path': '/tmp/other_file.txt'},
            'cwd': str(self.tmp.name),
        }
        res = hook.receipt_reminder(payload_outside, self.state, 'claude', 'PostToolUse', vault=self.vault)
        self.assertIsNone(res)
        # Verify no .edited marker created
        folder = self.state / 'receipt-reminders'
        self.assertFalse(any(p.name.endswith('.edited') for p in folder.glob('*')))

    def test_followup_diagnostic_is_separate_from_missing_in_all_windows(self):
        from beyin_v3_projections import record_checkpoints, refresh_gaps
        engine = SyncEngine(self.vault, self.state)
        now = time.time()
        record_checkpoints(engine, [
            {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'followup', 'at': now - 100},
            {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'followup', 'at': now - 20},
            {'event': 'Stop', 'harness': 'claude', 'session': 'followup', 'at': now - 10},
        ])
        with engine.store._connect() as db:
            db.execute('INSERT INTO receipts VALUES (?,?)', ('followup', json.dumps({
                'harness': 'claude', 'session': 'followup',
                'created_at': datetime.fromtimestamp(now - 50, timezone.utc).isoformat()})))
            refresh_gaps(engine, db)
            cov = receipt_coverage(db, now=now)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual(gaps['potential_missing_receipts'], 0)
        for counts in (cov, cov['last_7d'], cov['last_30d'], gaps['receipt_coverage']):
            self.assertEqual((counts['covered'], counts['missing'], counts['receipt_before_last_prompt']), (1, 0, 1))
        cmd = [sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
               '--state', str(self.state), 'doctor']
        doctor = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        self.assertEqual(doctor['receipt_coverage']['receipt_before_last_prompt'], 1)

    def test_out_of_order_prompt_preserves_earliest_boundary(self):
        from beyin_v3_projections import record_checkpoints
        engine = SyncEngine(self.vault, self.state)
        record_checkpoints(engine, [{'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'late', 'at': 200}])
        record_checkpoints(engine, [
            {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'late', 'at': 100},
            {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'late', 'at': 300},
            {'event': 'Stop', 'harness': 'claude', 'session': 'late', 'at': 400},
        ])
        with engine.store._connect() as db:
            row = db.execute('SELECT prompt_at,turn_at FROM receipt_checkpoints').fetchone()
            self.assertEqual(tuple(row), (100, 300))
            db.execute('INSERT INTO receipts VALUES (?,?)', ('late', json.dumps({
                'harness': 'claude', 'session': 'late',
                'created_at': datetime.fromtimestamp(150, timezone.utc).isoformat()})))
            self.assertEqual(receipt_coverage(db, now=500)['covered'], 1)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'Changing the process timezone requires tzset')
    def test_naive_receipt_is_utc_in_every_local_timezone(self):
        from beyin_v3_projections import record_checkpoints
        engine = SyncEngine(self.vault, self.state)
        stamp = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        record_checkpoints(engine, [
            {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': 'naive', 'at': stamp.timestamp() - 10},
            {'event': 'Stop', 'harness': 'claude', 'session': 'naive', 'at': stamp.timestamp() + 10},
        ])
        with engine.store._connect() as db:
            db.execute('INSERT INTO receipts VALUES (?,?)', ('naive', json.dumps({
                'harness': 'claude', 'session': 'naive', 'created_at': stamp.replace(tzinfo=None).isoformat()})))
            try:
                for zone in ('UTC0', 'UTC-3'):
                    with patch.dict(os.environ, {'TZ': zone}):
                        time.tzset()
                        self.assertEqual(receipt_coverage(db, now=stamp.timestamp() + 20)['covered'], 1)
            finally:
                time.tzset()

    def test_later_receipt_clears_diagnostic_and_wrong_harness_stays_missing(self):
        from beyin_v3_projections import record_checkpoints
        engine = SyncEngine(self.vault, self.state)
        for session in ('updated', 'wrong'):
            record_checkpoints(engine, [
                {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': session, 'at': 100},
                {'event': 'UserPromptSubmit', 'harness': 'claude', 'session': session, 'at': 200},
                {'event': 'Stop', 'harness': 'claude', 'session': session, 'at': 300},
            ])
        with engine.store._connect() as db:
            for ident, harness, session, stamp in (
                    ('early', 'claude', 'updated', 150), ('new', 'claude', 'updated', 200),
                    ('other', 'codex', 'wrong', 250)):
                db.execute('INSERT INTO receipts VALUES (?,?)', (ident, json.dumps({
                    'harness': harness, 'session': session,
                    'created_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat()})))
            cov = receipt_coverage(db, now=400)
        self.assertEqual((cov['covered'], cov['missing'], cov['receipt_before_last_prompt']), (1, 1, 0))


if __name__ == '__main__':
    unittest.main()
