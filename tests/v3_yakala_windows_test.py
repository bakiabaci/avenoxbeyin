"""Console output of the capture tool (yakala) on a legacy Windows code page (#268).

PYTHONIOENCODING imitates the piped Windows console on every OS; the commands run through a
real install and the public entry.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install, snapshot

INBOX = '📥 000-Inbox/Yakala'
WINDOWS = os.name == 'nt'


class ConsoleOutputTest(unittest.TestCase):
    """A piped Windows console is cp1254 or cp1252; neither can encode the inbox emoji."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-out-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'Örnek Beyin', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), APPDATA=str(root / 'appdata'))
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.vault / INBOX).parent.mkdir(exist_ok=True)  # the starter inbox: an emoji in every path printed

    def entry(self, encoding, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                env=dict(self.env, PYTHONIOENCODING=encoding), cwd=self.vault, timeout=120)
        self.assertEqual(result.returncode, 0, (args, result.stderr))
        # ASCII bytes mean the same under every decoder a caller may use for the pipe.
        self.assertTrue(result.stdout.isascii(), (args, result.stdout[:200]))
        decoded = {codec: json.loads(result.stdout.decode(codec)) for codec in ('cp1254', 'cp1252', 'utf-8')}
        self.assertEqual(decoded['cp1254'], decoded['utf-8'])
        self.assertEqual(decoded['cp1252'], decoded['utf-8'])
        return decoded['utf-8']

    def test_json_output_survives_legacy_code_pages(self):
        note = 'Şifre ve Müşteri Arşiv notu, Iğdır'
        for number, encoding in enumerate(('cp1254', 'cp1252'), 1):
            with self.subTest(encoding=encoding):
                installed = self.entry(encoding, 'kur', '--kisayol-yok')
                self.assertEqual(installed['klasor'], INBOX)
                self.assertTrue((self.vault / installed['web_clipper_sablonu']).is_file())
                added = self.entry(encoding, 'ekle', '--metin', note + ' ' + str(number))
                self.assertTrue(added['path'].startswith(INBOX + '/'), added['path'])
                self.assertTrue((self.vault / added['path']).is_file())
                listed = self.entry(encoding, 'liste')
                # A command that failed after writing its card left a second card on the retry.
                self.assertEqual(listed['bekleyen'], number)
                self.assertIn(note + ' ' + str(number), [card['baslik'] for card in listed['kartlar']])
                self.assertEqual(self.entry(encoding, 'durum')['klasor'], INBOX)
                self.assertEqual(self.entry(encoding, 'sablon')['path'], INBOX)

    def test_durum_writes_nothing(self):
        self.entry('utf-8', 'kur', '--kisayol-yok')
        before = (snapshot(self.vault), snapshot(self.state))
        self.entry('cp1254', 'durum')
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)

    @unittest.skipIf(WINDOWS, 'needs a POSIX pseudo terminal')
    def test_human_text_survives_a_legacy_terminal(self):
        """The module run on its own (the listener and Send To do that) prints text for a person."""
        import pty
        import select
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_yakala.py'), '--vault',
                                    str(self.vault), 'ekle', '--metin', 'Müşteri notu'], stdin=subprocess.DEVNULL,
                                   stdout=slave, stderr=subprocess.PIPE, env=dict(self.env, PYTHONIOENCODING='cp1254'),
                                   cwd=self.vault)
        os.close(slave)
        shown = b''
        try:
            while select.select([master], [], [], 60)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:  # Linux reports the closed terminal as EIO
                    break
                if not chunk:
                    break
                shown += chunk
        finally:
            os.close(master)
            error = process.stderr.read()
            process.stderr.close()
            process.wait(60)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn(b'Beyne atildi: ? 000-Inbox/Yakala/', shown)
        self.assertEqual(len(list((self.vault / INBOX).glob('*.md'))), 1)


if __name__ == '__main__':
    unittest.main()
