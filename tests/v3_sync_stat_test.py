#!/usr/bin/env python3
"""Stat signature cache of warm syncs (#233): no lost edit, every conflict stays visible.

Every case is checked against LegacySyncEngine, the frozen engine that reads every source,
on its own state directory over the same vault. A cache hit must give the same sync report
and the same events, records and ownership rows as reading the bytes, and every indexed
record must carry the hash of the bytes now on disk.

Fixtures are written and read as bytes: text mode would turn '\n' into '\r\n' on Windows,
and the expected texts below are LF.
"""
from contextlib import contextmanager
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env
from v3_sync_test import load_module
from v3_sync_warm_test import LegacySyncEngine

subject = load_module()
SCRIPTS = Path(subject.__file__).resolve().parent
CLI = Path(__file__).resolve().parents[1] / 'scripts/beyin_v3.py'
_real_time_ns = time.time_ns
NO_CHANGE_TIME = 'Windows os.stat has no change time: st_ctime is the creation time'


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))
    return path


def get(path):
    return path.read_bytes().decode('utf-8')


def coarse_signature(st):
    """A filesystem whose stat cannot see an in-place rewrite: 2 s mtime ticks, no ctime or
    file id (FAT; on Windows st_ctime is the creation time)."""
    return (st.st_size, st.st_mtime_ns // 2_000_000_000 * 2_000_000_000, 0, '0')


def weak_signature(st):
    """The signature first proposed for the cache: size and mtime only."""
    return (st.st_size, st.st_mtime_ns, 0, '0')


def tunneled_signature(st):
    """NTFS hands the creation time of a replaced file to the file that takes its name
    (tunneling), so there an atomic save shows only in the file id."""
    return (st.st_size, st.st_mtime_ns, 0, str(st.st_ino))


@contextmanager
def clock(seconds):
    """Run a sync as if it happened `seconds` from now (no real sleeping)."""
    offset = int(seconds * 1_000_000_000)
    with patch.object(subject.time, 'time_ns', side_effect=lambda: _real_time_ns() + offset):
        yield


class StatSignatureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='beyin-sync-stat-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.engine = subject.SyncEngine(self.vault, self.state)
        self.oracle = LegacySyncEngine(self.vault, self.root / 'oracle-state')
        self.minutes = 0
        self.last_reads = []

    def write(self, name='notes/note.md', id='note', text='Alpha calibration.\n', **fields):
        return put(self.vault / name, subject.render(dict(id=id, kind='note', revision=1, **fields), text))

    def snapshot(self, engine):
        with engine.store._connect() as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                    for table in ('events', 'records', 'markdown_sources')}

    def later(self):
        """Each settled sync happens a minute after the previous one, past the racy window."""
        self.minutes += 1
        return clock(60 * self.minutes)

    @contextmanager
    def reads(self):
        calls = []
        original = Path.read_bytes

        def counted(path):
            calls.append(Path(path).name)
            return original(path)

        with patch.object(Path, 'read_bytes', counted):
            yield calls

    def sync_both(self):
        """One sync of each engine over the same vault; reports and tables must be equal.

        self.last_reads names the sources the cached engine read (twice each: scan and validation).
        """
        with self.later():
            with self.reads() as calls:
                result = self.engine.sync()
            expected = self.oracle.sync()
        self.last_reads = calls
        self.assertEqual(result, expected)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.assert_index_is_fresh()
        return result

    def assert_index_is_fresh(self):
        """Retrieval's own gate: a record is used only while its hash is that of its source."""
        with self.engine.store._connect() as db:
            rows = db.execute('SELECT s.source, r.payload FROM markdown_sources s JOIN records r ON r.id=s.id').fetchall()
        for source, payload in rows:
            # open(), not Path.read_bytes: the tests patch and count that one.
            with open(self.vault / source, 'rb') as handle:
                self.assertEqual(json.loads(payload)['source_sha256'], hashlib.sha256(handle.read()).hexdigest(), source)

    def signatures(self):
        """source -> (payload_sha256, seen_ns); an empty hash is a signature seen once, not trusted."""
        with self.engine.store._connect() as db:
            return {row[0]: row[1:] for row in db.execute('SELECT source, payload_sha256, seen_ns FROM source_signatures')}

    def settle(self):
        """Index the vault and make every signature trusted (read after the racy window)."""
        self.sync_both()
        self.sync_both()

    def record(self, id):
        with self.engine.store._connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def rewrite_same_size(self, path, old, new):
        raw = path.read_bytes()
        self.assertEqual(len(old), len(new))
        path.write_bytes(raw.replace(old, new))

    def test_settled_unchanged_sources_are_not_read(self):
        for index in range(5):
            self.write(f'notes/note-{index}.md', id=f'note-{index}')
        self.settle()
        with self.reads() as calls:
            result = self.sync_both()
        self.assertEqual(result['status'], 'succeeded')
        # Only the oracle read the five notes (twice each: scan and validation).
        self.assertEqual(len(calls), 10)
        self.assertEqual(self.last_reads, [])
        with self.reads() as calls, self.later():
            self.assertEqual(self.engine.sync()['indexed'], 5)
        self.assertEqual(calls, [])

    def test_a_signature_is_trusted_from_its_second_read(self):
        # Seen once: remembered, not trusted. Read again two seconds later: trusted.
        self.write()
        self.sync_both()
        (payload, seen), = self.signatures().values()
        self.assertEqual(payload, '')
        self.sync_both()
        self.assertEqual(self.last_reads, ['note.md', 'note.md'])
        (payload, again), = self.signatures().values()
        self.assertEqual((len(payload), again), (64, seen))
        self.sync_both()
        self.assertEqual(self.last_reads, [])

    def test_a_same_size_edit_is_indexed(self):
        path = self.write()
        other = self.write('notes/other.md', id='other')
        self.settle()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        with self.reads() as calls, self.later():
            result = self.engine.sync()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
        self.assertEqual(self.record('note')['revision'], 2)
        self.assertIn('note.md', calls)
        self.assertNotIn(other.name, calls)
        self.assertEqual(result['status'], 'succeeded')
        with self.later():
            self.assertEqual(self.oracle.sync(), result)
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))

    @unittest.skipIf(os.name == 'nt', NO_CHANGE_TIME)
    def test_an_edit_with_its_mtime_put_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_second_write_in_the_same_timestamp_tick_is_indexed(self):
        # Coarse timestamps: both writes carry one mtime. The first was read within the
        # racy window, so its signature is not trusted and the second write is read.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            with clock(0.5):
                self.engine.sync()
                self.oracle.sync()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with clock(1):
                result = self.engine.sync()
                self.assertEqual(result, self.oracle.sync())
            self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            # Read after the window, the signature is trusted from then on.
            with clock(10):
                self.engine.sync()
            with self.reads() as calls, clock(20):
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_a_replaced_file_is_indexed(self):
        # Atomic save: a new file with the same size and mtime takes the name.
        path = self.write()
        self.settle()
        before = path.stat()
        replacement = path.with_name('note.md.tmp')
        replacement.write_bytes(path.read_bytes().replace(b'Alpha', b'Omega'))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertNotEqual(path.stat().st_ino, before.st_ino)
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')

    def test_a_clock_set_back_is_indexed(self):
        path = self.write()
        self.settle()
        before = path.stat()
        put(path, get(path).replace('Alpha', 'Earlier clock'))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns - 3600 * 1_000_000_000))
        with clock(-3600):
            result = self.engine.sync()
            self.assertEqual(result, self.oracle.sync())
        self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
        self.assertEqual(self.record('note')['text'], 'Earlier clock calibration.\n')
        self.sync_both()

    def test_an_unreadable_placeholder_keeps_its_record_without_a_read(self):
        path = self.write()
        self.settle()
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted), self.later():
            result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE event_type='delete'").fetchone()[0], 0)

    def test_a_changed_unreadable_placeholder_matches_a_full_read(self):
        # Same as reading every source: the warning, and what happens to the record.
        path = self.write()
        self.settle()
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        original = Path.read_bytes

        def evicted(file):
            if Path(file).name == path.name:
                raise OSError(errno.EDEADLK, 'Resource deadlock avoided')
            return original(file)

        with patch.object(Path, 'read_bytes', evicted):
            result = self.sync_both()
        self.assertEqual(result['warnings'], [{'source': 'notes/note.md', 'reason': str(OSError(errno.EDEADLK, 'Resource deadlock avoided'))}])
        # Readable again: indexed again, as in a full read.
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_duplicate_ids_stay_quarantined(self):
        self.write()
        self.settle()
        self.write('notes/copy.md')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'note', 'reason': 'duplicate source id; all copies quarantined'}])
            self.assertIsNone(self.record('note'))
        (self.vault / 'notes/copy.md').unlink()
        self.assertEqual(self.sync_both()['status'], 'succeeded')
        self.assertIsNotNone(self.record('note'))

    def test_conflict_markers_stay_visible(self):
        path = self.write()
        self.settle()
        put(path, get(path) + '<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['warnings'], [{'source': 'notes/note.md',
                                                   'reason': 'unresolved git conflict markers; resolve the merge before sync'}])

    def test_id_owned_by_another_writer_stays_a_conflict(self):
        payload = subject._json(dict(id='foreign', revision=7, source='notes/note.md', text='Other writer', kind='note'))
        for engine in (self.engine, self.oracle):
            with engine.store._connect() as db:
                db.execute('INSERT INTO records VALUES (?,?)', ('foreign', payload))
        self.write()
        self.write('notes/collision.md', id='foreign')
        for _ in range(3):
            result = self.sync_both()
            self.assertEqual(result['conflicts'], [{'id': 'foreign', 'reason': 'id already owned by another record'}])

    def test_a_manual_receipt_view_edit_stays_a_conflict(self):
        self.write()
        self.settle()
        self.engine.receipt('event-1', 'Calibrated alpha.', ['notes/note.md'], 'codex')
        views = [path for path in self.vault.rglob('*.md') if 'v3' in path.parts]
        self.assertTrue(views)
        view = views[0]
        put(view, get(view) + '\nEdited by hand.\n')
        relative = view.relative_to(self.vault).as_posix()
        for _ in range(3):
            with self.later():
                result = self.engine.sync()
            self.assertIn({'source': relative, 'reason': 'manual receipt view edit preserved'}, result['conflicts'])
            self.assertTrue(get(view).endswith('Edited by hand.\n'))

    def test_outside_vault_symlink_is_rejected_on_every_sync(self):
        self.write()
        outside = self.root / 'outside'
        outside.mkdir()
        put(outside / 'outside.md', 'Outside source.\n')
        try:
            (self.vault / 'outside-link.md').symlink_to(outside / 'outside.md')
            (self.vault / 'broken-link.md').symlink_to(outside / 'missing.md')
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable on this platform')
        expected = [{'source': 'broken-link.md', 'reason': 'source outside vault'},
                    {'source': 'outside-link.md', 'reason': 'source outside vault'}]
        for _ in range(3):
            self.assertEqual(self.sync_both()['warnings'], expected)

    def test_an_index_written_by_a_version_without_signatures_is_not_trusted(self):
        # Rollback to a version without the table: it re-indexes an edit, the edit is then
        # reverted with its old mtime, and the update comes back. Stat cannot see the revert
        # here; the indexed payload is no longer the one the signature was recorded for.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            original = path.read_bytes()
            before = path.stat()
            rolled_back = LegacySyncEngine(self.vault, self.state)
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.later():
                rolled_back.sync()
            self.assertEqual(self.record('note')['text'], 'Omega calibration.\n')
            path.write_bytes(original)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertIn('note.md', calls)
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')

    def test_a_payload_rewritten_by_another_writer_is_rebuilt(self):
        # The store API can rewrite an indexed row without touching its source; a full read
        # rebuilds the row from the source, so a hit must not keep the rewritten payload.
        self.write()
        self.settle()
        for engine in (self.engine, self.oracle):
            engine.store.update_task('note', 1, {'status': 'rewritten elsewhere'})
        self.sync_both()
        self.assertNotIn('status', self.record('note'))

    def test_rollback_edits_are_indexed_after_update(self):
        path = self.write()
        self.settle()
        rolled_back = LegacySyncEngine(self.vault, self.state)
        put(path, get(path).replace('Alpha', 'Rolled back'))
        with self.later():
            rolled_back.sync()
        self.sync_both()
        self.assertEqual(self.record('note')['text'], 'Rolled back calibration.\n')
        self.sync_both()

    def test_new_code_trusts_no_stored_signature(self):
        self.write()
        self.settle()
        with self.reads() as calls, self.later(), patch.object(subject, '_signature_epoch', return_value='other code'):
            self.engine.sync()
        self.assertIn('note.md', calls)
        with self.engine.store._connect() as db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='source_signature_epoch'").fetchone()[0], 'other code')

    def test_signature_includes_ctime_and_file_id(self):
        st = os.stat(self.write())
        self.assertEqual(subject._stat_signature(st), (st.st_size, st.st_mtime_ns, st.st_ctime_ns, str(st.st_ino)))

    def put_back(self, path, before):
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))

    def test_a_file_server_clock_behind_this_one_does_not_hide_an_edit(self):
        # Timestamps stamped by a clock 10 s behind this machine's, in 2 s ticks: the first write
        # already looks old enough, and the edit lands in its tick. A signature seen once is not
        # trusted; it settles on a read two seconds of this machine's own clock later.
        path = self.write()
        with patch.object(subject, '_stat_signature', coarse_signature):
            for offset, old, new in ((10, None, None), (10.5, b'Alpha', b'Omega'), (11.5, b'Omega', b'Gamma')):
                if old:
                    before = path.stat()
                    self.rewrite_same_size(path, old, new)
                    self.put_back(path, before)
                with clock(offset):
                    result = self.engine.sync()
                    self.assertEqual(result, self.oracle.sync())
                self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
                self.assert_index_is_fresh()
            self.assertEqual(self.record('note')['text'], 'Gamma calibration.\n')
            with clock(20):
                self.engine.sync()
            with self.reads() as calls, clock(30):
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_a_clock_set_back_after_a_sighting_starts_the_wait_over(self):
        # A first sighting dated ahead of the clock cannot prove that two seconds went by.
        self.write()
        with clock(3600):
            self.engine.sync()
        (payload, seen), = self.signatures().values()
        with clock(5):
            self.engine.sync()
        (payload, again), = self.signatures().values()
        self.assertEqual(payload, '')
        self.assertLess(again, seen)
        with clock(10):
            self.engine.sync()
        with self.reads() as calls, clock(20):
            self.engine.sync()
        self.assertEqual(calls, [])

    def test_a_full_sync_reads_what_no_stat_can_show(self):
        # The limit of any stat cache: an in-place edit of the same size with its mtime put back,
        # on a filesystem without a change time (FAT, exFAT; os.stat on Windows). A normal sync
        # cannot see it; retrieval still refuses the record (its hash is not the source's), and
        # `sync --full` reads everything.
        path = self.write()
        self.write('notes/other.md', id='other')
        with patch.object(subject, '_stat_signature', coarse_signature):
            self.settle()
            before = path.stat()
            self.rewrite_same_size(path, b'Alpha', b'Omega')
            self.put_back(path, before)
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertEqual(calls, [])
            self.assertEqual(self.record('note')['text'], 'Alpha calibration.\n')
            self.assertEqual(self.engine.store._eligible()[1], 1)
            with self.later():
                self.oracle.sync()
            with self.reads() as calls, self.later():
                result = self.engine.sync(full=True)
            self.assertEqual(sorted(set(calls)), ['note.md', 'other.md'])
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(self.snapshot(self.engine), self.snapshot(self.oracle))
            self.assert_index_is_fresh()
            self.assertEqual(self.engine.store._eligible()[1], 0)
            # A full sync settles what it read like any other read.
            with self.reads() as calls, self.later():
                self.engine.sync()
            self.assertEqual(calls, [])

    def test_cli_sync_full(self):
        path = self.write()
        cli_state = self.root / 'cli-state'

        def run(*arguments):
            result = subprocess.run([sys.executable, str(CLI), '--vault', str(self.vault), '--state', str(cli_state), 'sync', *arguments],
                                    capture_output=True, text=True, encoding='utf-8', env=inherited_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual(run()['indexed'], 1)
        self.rewrite_same_size(path, b'Alpha', b'Omega')
        self.assertEqual(run('--full')['status'], 'succeeded')
        with subject.SyncEngine(self.vault, cli_state).store._connect() as db:
            payload, = db.execute("SELECT payload FROM records WHERE id='note'").fetchone()
        self.assertEqual(json.loads(payload)['text'], 'Omega calibration.\n')


def _load(name):
    spec = importlib.util.spec_from_file_location('v3_sync_stat_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafeReplaceTest(unittest.TestCase):
    modules = ('beyin_v3_sync', 'beyin_v3_hook', 'beyin_v3_update')

    def test_posix_permission_error_is_raised_at_once(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Permission denied', str(destination))

                with patch.object(module.os, 'name', 'posix'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep') as sleep:
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_windows_sharing_violation_is_retried(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = subject if name == 'beyin_v3_sync' else _load(name)
                calls = []

                def busy(source, destination):
                    calls.append(destination)
                    if len(calls) < 3:
                        error = PermissionError(errno.EACCES, 'The process cannot access the file', str(destination))
                        error.winerror = 32
                        raise error

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', busy), \
                        patch.object(module.time, 'sleep') as sleep:
                    module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleep.call_count, 2)
                # Not a sharing violation (e.g. a read-only target emulated without winerror): no retry.
                calls.clear()

                def refuse(source, destination):
                    calls.append(destination)
                    raise PermissionError(errno.EACCES, 'Access is denied', str(destination))

                with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', refuse), \
                        patch.object(module.time, 'sleep'):
                    with self.assertRaises(PermissionError):
                        module._safe_replace('a', 'b')
                self.assertEqual(len(calls), 1)

    def test_windows_read_only_target_is_raised_at_once(self):
        # #230: Windows refuses to replace a read-only file with the same WinError 5 a scanner
        # causes. No wait lifts that flag, so the caller gets the error without 225 ms of retries.
        with tempfile.TemporaryDirectory(prefix='beyin-safe-replace-') as directory:
            # Plain strings, made before os.name is faked: pathlib refuses a WindowsPath elsewhere.
            target, missing = os.path.join(directory, 'locked.md'), os.path.join(directory, 'new.md')
            Path(target).write_bytes(b'locked')
            for name in self.modules:
                with self.subTest(module=name):
                    module = subject if name == 'beyin_v3_sync' else _load(name)
                    calls = []

                    def denied(source, destination):
                        calls.append(destination)
                        error = PermissionError(errno.EACCES, 'Access is denied', str(destination))
                        error.winerror = 5
                        raise error

                    with patch.object(module.os, 'name', 'nt'), patch.object(module.os, 'replace', denied), \
                            patch.object(module.time, 'sleep') as sleep:
                        with patch.object(module.os, 'access', return_value=False):
                            with self.assertRaises(PermissionError):
                                module._safe_replace('a', target)
                        self.assertEqual((len(calls), sleep.call_count), (1, 0))
                        # Writable and still denied: a scanner or an open handle, worth the retries.
                        calls.clear()
                        with patch.object(module.os, 'access', return_value=True):
                            with self.assertRaises(PermissionError):
                                module._safe_replace('a', target)
                        self.assertEqual((len(calls), sleep.call_count), (5, 4))
                        # A target that does not exist yet cannot be read-only.
                        calls.clear()
                        with patch.object(module.os, 'access', return_value=False):
                            with self.assertRaises(PermissionError):
                                module._safe_replace('a', missing)
                        self.assertEqual(len(calls), 5)


if __name__ == '__main__':
    unittest.main()
