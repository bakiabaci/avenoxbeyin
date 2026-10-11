"""Optional capture tool (yakala): exercised through a real install, the installed entry and hook.

Offline: `isle` runs with every helper tool hidden, so nothing reaches the network and the
popup/hotkey paths are never started.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
INBOX = '📥 000-Inbox/Yakala'
sys.path.insert(0, str(SCRIPTS))
import beyin_v3_yakala as yakala  # noqa: E402


class YakalaUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'Örnek Beyin'
        self.vault.mkdir()

    def test_canonical_merges_url_forms(self):
        self.assertEqual(yakala.canonical('https://youtu.be/abc123?t=40'), yakala.canonical('https://www.youtube.com/watch?v=abc123&list=x'))
        self.assertEqual(yakala.canonical('https://twitter.com/a/status/42'), yakala.canonical('https://x.com/b/status/42?s=20'))
        self.assertNotEqual(yakala.canonical('https://a.com/x'), yakala.canonical('https://a.com/y'))

    def test_kind_and_slug(self):
        self.assertEqual(yakala.kind_of('https://m.youtube.com/watch?v=1'), 'youtube')
        self.assertEqual(yakala.kind_of('https://x.com/a/status/1'), 'x')
        self.assertEqual(yakala.kind_of('https://mail.google.com/mail/u/0/#inbox/1'), 'mail')
        self.assertEqual(yakala.kind_of(None, app='Mail'), 'mail')
        self.assertEqual(yakala.kind_of(None), 'metin')
        self.assertEqual(yakala.slug('Kısa tut, İyi düşün: ağ'), 'kisa-tut-iyi-dusun-ag')
        self.assertEqual(yakala.url_slug('https://www.youtube.com/watch?v=AbC'), 'youtube-abc')
        self.assertEqual(yakala.url_slug('https://x.com/karpathy/status/99'), 'x-karpathy-99')

    def test_capture_writes_one_card_and_dedupes(self):
        first = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=abc', why='limit gözlemi')
        again = yakala.capture(self.vault, url='https://youtu.be/abc', why='ikinci bakış')
        self.assertEqual(first['status'], 'yakalandi')
        self.assertEqual(again['status'], 'mevcut_karta_eklendi')
        self.assertEqual(again['id'], first['id'])
        cards = yakala.cards(self.vault)
        self.assertEqual(len(cards), 1)
        meta, body = cards[0]['meta'], cards[0]['body']
        self.assertEqual((meta['durum'], meta['kaynak_turu'], meta['visibility']), ('bekliyor', 'youtube', 'internal'))
        self.assertIn('limit gözlemi', body)
        self.assertIn('ikinci bakış', body)
        self.assertEqual(yakala.pending(self.vault), 1)

    def test_capture_requires_something(self):
        with self.assertRaises(ValueError):
            yakala.capture(self.vault, why='yalniz neden')

    def test_private_kinds_and_files(self):
        source = Path(self.tmp.name) / 'not.txt'
        source.write_text('dosyadaki ders', encoding='utf-8')
        result = yakala.capture(self.vault, files=[source], why='belge')
        card = yakala.find_card(self.vault, result['id'])
        self.assertEqual(card['meta']['visibility'], 'private')
        self.assertTrue((self.vault / INBOX / 'dosyalar/not.txt').is_file())
        text = yakala.capture(self.vault, text='pano metni')
        self.assertEqual(yakala.find_card(self.vault, text['id'])['meta']['visibility'], 'private')

    def test_process_offline_and_finish(self):
        none = {name: None for name in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
        note = yakala.capture(self.vault, text='Bağlam kaybı en pahalı kalem.', why='ders')
        clipped = self.vault / INBOX / 'clip.md'
        clipped.write_text('---\ntur: yakala\ndurum: bekliyor\nkaynak_turu: makale\nurl: "https://ornek.com/yazi"\n---\n\n'
                           '# Yazı\n\n## Neden\n\n\n\n## Icerik\n\n' + ('Uzun makale metni. ' * 30) + '\n', encoding='utf-8')
        video = yakala.capture(self.vault, url='https://www.youtube.com/watch?v=zzz')
        with patch.object(yakala, 'tools', return_value=none):
            result = yakala.process(self.vault)
        by_id = {entry['id']: entry for entry in result['kartlar']}
        self.assertEqual(by_id[note['id']]['yontem'], 'metin')
        self.assertEqual(by_id['clip']['yontem'], 'web-clipper')
        self.assertEqual(by_id[video['id']]['durum'], 'hata')
        raw = (self.vault / by_id['clip']['ham']).read_text(encoding='utf-8')
        self.assertIn('Uzun makale metni.', raw)
        self.assertIn('visibility: private', raw)
        # The clipped page body moves out of the indexed card into the dot folder.
        self.assertNotIn('Uzun makale metni.', (self.vault / INBOX / 'clip.md').read_text(encoding='utf-8'))
        self.assertEqual(yakala.pending(self.vault), 0)
        concept = self.vault / 'knowledge/concepts/baglam.md'
        concept.parent.mkdir(parents=True)
        concept.write_text('# Bağlam\n', encoding='utf-8')
        done = yakala.finish(self.vault, note['id'], ['knowledge/concepts/baglam.md'], 'tek ders')
        self.assertEqual(done['status'], 'islendi')
        card = yakala.find_card(self.vault, note['id'])
        self.assertEqual(card['meta']['durum'], 'islendi')
        self.assertIn('[[knowledge/concepts/baglam]]', card['body'])
        with self.assertRaises(ValueError):
            yakala.finish(self.vault, note['id'], ['knowledge/concepts/yok.md'])

    def test_vtt_text(self):
        raw = ('WEBVTT\nKind: captions\n\n00:00:00.000 --> 00:00:02.000\nmerhaba <c>dunya</c>\n\n'
               '00:00:02.000 --> 00:00:04.000\nmerhaba dunya\nikinci satir\n\n00:00:40.000 --> 00:00:42.000\nyeni paragraf\n'
               '\n01:00:05.000 --> 01:00:06.000\nsaat\n')
        text = yakala.vtt_text(raw)
        self.assertEqual(text.count('merhaba dunya'), 1)
        self.assertIn('**0:00** merhaba dunya ikinci satir', text)
        self.assertIn('**0:40** yeni paragraf', text)
        self.assertIn('**1:00:05** saat', text)

    def test_repair_mojibake(self):
        good = 'Agent loop’ta en pahalı şey bağlam kaybı'
        self.assertEqual(yakala.repair_mojibake(good.encode('utf-8').decode('mac_roman')), good)
        self.assertEqual(yakala.repair_mojibake('pahalÄ± ÅŸey'), 'pahalı şey')  # cp1252 variant
        for text in ('Müşteri Şifre Arşiv', 'café', 'plain'):
            self.assertEqual(yakala.repair_mojibake(text), text)

    def test_clipper_template_is_importable(self):
        template = yakala.clipper_template()
        for field in ('name', 'behavior', 'properties', 'noteContentFormat', 'noteNameFormat', 'path'):
            self.assertIn(field, template)
        self.assertEqual(template['path'], INBOX)
        names = {prop['name']: prop['value'] for prop in template['properties']}
        self.assertEqual((names['tur'], names['durum']), ('yakala', 'bekliyor'))
        self.assertTrue(all(prop['type'] in ('text', 'multitext', 'number', 'checkbox', 'date', 'datetime')
                            for prop in template['properties']))

    def test_hotkey_specs(self):
        self.assertEqual(yakala.parse_hotkey('ctrl+alt+b'), (['ctrl', 'alt'], 'b'))
        self.assertEqual(yakala.parse_hotkey('Option+Command+K'), (['alt', 'cmd'], 'K'))
        self.assertEqual(yakala.parse_hotkey('⌘⇧space'), (['shift', 'cmd'], 'space'))
        self.assertEqual(yakala.parse_hotkey('cmd++'), (['cmd'], '+'))
        self.assertEqual(yakala.parse_hotkey('cmd+"'), (['cmd'], '"'))
        self.assertEqual(yakala.parse_hotkey('f9'), ([], 'f9'))
        for bad in ('', 'b', 'hyper+b', 'cmd+', 'cmd+pageup'):
            with self.assertRaises(ValueError):
                yakala.parse_hotkey(bad)
        self.assertEqual(yakala.windows_hotkey('ctrl+alt+b'), ('CTRL+ALT+B', 'Ctrl+Alt+B'))
        self.assertEqual(yakala.windows_hotkey('alt+shift+f2')[0], 'ALT+SHIFT+F2')
        for bad in ('cmd+b', 'shift+b', 'ctrl+"'):
            with self.assertRaises(ValueError):
                yakala.windows_hotkey(bad)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS key codes')
    def test_mac_hotkey_codes(self):
        self.assertEqual(yakala.mac_hotkey('ctrl+alt+b')[:2], (11, 0x1800))
        self.assertEqual(yakala.mac_hotkey('cmd+shift+space'), (49, 0x300, '⇧⌘Space'))
        self.assertEqual(yakala.mac_hotkey('alt+f5')[:2], (96, 0x800))

    def test_vault_before_or_after_command(self):
        yakala.capture(self.vault, url='https://ornek.com/a')
        for argv in (['--vault', str(self.vault), '--json', 'liste'], ['liste', '--vault', str(self.vault), '--json']):
            with patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
                yakala.main(argv)
            self.assertEqual(json.loads(out.getvalue())['bekleyen'], 1)

    def test_bad_hotkey_writes_nothing(self):
        state = Path(self.tmp.name) / 'state'
        # The spec is parsed before any platform call, so the macOS branch rejects it on every OS.
        with patch.object(yakala.sys, 'platform', 'darwin'):
            with self.assertRaises(ValueError):
                yakala.install(self.vault, state, spec='hyper+b')
        self.assertFalse((self.vault / INBOX).exists())
        self.assertFalse(state.exists())

    def test_uninstall_leaves_another_vaults_listener(self):
        import plistlib
        agent = Path(self.tmp.name) / 'agent.plist'
        agent.write_bytes(plistlib.dumps({'ProgramArguments': ['python3', 'x.py', 'dinle', '--vault', '/baska/vault']}))
        state = Path(self.tmp.name) / 'state'
        with patch.object(yakala, '_launch_agent', return_value=agent), patch.object(yakala.sys, 'platform', 'darwin'), \
                patch.object(yakala.subprocess, 'run') as run:
            result = yakala.uninstall(self.vault, state)
        run.assert_not_called()
        self.assertTrue(agent.exists())
        self.assertNotIn('LaunchAgent', result['kaldirilan'])

    def test_inbox_report_skips_processed_cards(self):
        import beyin_v3_hygiene as hygiene
        waiting = yakala.capture(self.vault, url='https://ornek.com/a')
        done = yakala.capture(self.vault, text='bitti')
        card = yakala.find_card(self.vault, done['id'])
        yakala.write_card(card['path'], dict(card['meta'], durum='islendi'), card['body'])
        rows = hygiene.inbox_report(self.vault)['folders']
        self.assertEqual([row['notes'] for row in rows if row['folder'] == '📥 000-Inbox'], [1])
        self.assertTrue(waiting['id'])

    def test_clipper_card_with_quoted_yaml_is_pending(self):
        folder = self.vault / INBOX
        folder.mkdir(parents=True)
        (folder / 'a.md').write_text('---\ntur: yakala\ndurum: "bekliyor"\nurl: "https://a.com"\n---\n\n# A\n', encoding='utf-8')
        (folder / 'b.md').write_text('---\ntur: not\ndurum: bekliyor\n---\n', encoding='utf-8')
        self.assertEqual(yakala.pending(self.vault), 1)

    def test_emojiless_inbox_detection_and_install(self):
        vault = Path(self.tmp.name) / 'emojiless-vault'
        vault.mkdir(parents=True)
        (vault / '000-Inbox').mkdir()
        state = Path(self.tmp.name) / 'emojiless-state'
        self.assertEqual(yakala.find_inbox(vault), '000-Inbox/Yakala')
        res = yakala.install(vault, state, hotkey=False)
        self.assertEqual(res['klasor'], '000-Inbox/Yakala')
        self.assertTrue((vault / '000-Inbox/Yakala').is_dir())
        self.assertFalse((vault / '📥 000-Inbox').exists())
        st = yakala.status(vault, state)
        self.assertEqual(st['klasor'], '000-Inbox/Yakala')


class YakalaInboxTest(unittest.TestCase):
    """Which folder holds the cards: the saved choice, the starter folder, one inbox by word, else no guess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-inbox-')
        self.addCleanup(self.tmp.cleanup)
        self.vault, self.state = Path(self.tmp.name) / 'Örnek Beyin', Path(self.tmp.name) / 'state'
        self.vault.mkdir()

    def top(self):
        return sorted(path.name for path in self.vault.iterdir() if not path.name.startswith('.'))

    def test_renamed_inbox_is_reused(self):
        (self.vault / '000-Inbox').mkdir()
        yakala.capture(self.vault, url='https://ornek.com/yazi')
        self.assertFalse((self.vault / '📥 000-Inbox').exists())
        self.assertEqual(len(list((self.vault / '000-Inbox/Yakala').glob('*.md'))), 1)
        self.assertEqual(yakala.clipper_template(yakala.find_inbox(self.vault))['path'], '000-Inbox/Yakala')
        self.assertIn('(000-Inbox/Yakala)', yakala.session_notice(self.vault))

    def test_inbox_choice(self):
        import shutil

        def chosen(*folders):
            for name in folders:
                (self.vault / name).mkdir(parents=True)
            try:
                return yakala.find_inbox(self.vault, self.state)
            finally:
                for path in self.vault.iterdir():
                    shutil.rmtree(path)
        self.assertEqual(chosen(), INBOX)
        self.assertEqual(chosen('00_INBOX', 'Notlar'), '00_INBOX/Yakala')
        self.assertEqual(chosen('GELEN KUTUSU'), 'GELEN KUTUSU/Yakala')
        self.assertEqual(chosen('İNBOX'), 'İNBOX/Yakala')  # str.lower() alone turns this İ into two code points
        self.assertEqual(chosen('📥 000-Inbox', '00_INBOX'), INBOX)  # the starter folder wins
        self.assertEqual(chosen('📥 000-Inbox', '00_INBOX/Yakala'), INBOX)  # always, so an existing vault never moves
        self.assertEqual(chosen('00_INBOX', 'Gelen Kutusu'), INBOX)  # two inboxes: no guess
        self.assertEqual(chosen('00_INBOX', 'Gelen Kutusu/Yakala'), 'Gelen Kutusu/Yakala')  # the one already in use
        self.assertEqual(chosen('00_INBOX/Yakala', 'Gelen Kutusu/Yakala'), INBOX)
        self.assertEqual(chosen('Gelen Belgeler', 'Inbox Arşivi', '🔐 Kasa Inbox', '.inbox', 'Inboxing'), INBOX)

    def test_linked_folder_is_never_picked_or_followed_out(self):
        outside = Path(self.tmp.name) / 'Disari'
        outside.mkdir()
        try:
            (self.vault / 'Inbox').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('this account cannot create symlinks')
        self.assertEqual(yakala.find_inbox(self.vault, self.state), INBOX)
        with self.assertRaises(ValueError):
            yakala.install(self.vault, self.state, hotkey=False, folder_spec='Inbox/Yakala')
        self.assertEqual((list(outside.iterdir()), self.top(), self.state.exists()), ([], ['Inbox'], False))

    def test_inbox_words_are_the_doctors(self):
        import unicodedata
        import beyin_v3_hygiene as hygiene
        self.assertEqual(yakala.INBOX_WORDS.pattern, hygiene.INBOX_WORDS.pattern)
        self.assertEqual(yakala.SENSITIVE_WORDS.pattern, hygiene.SENSITIVE_WORDS.pattern)
        for name in ('📥 000-Inbox', '00_INBOX', 'GELEN KUTUSU', 'İNBOX', 'ınbox', 'MÜŞTERİLER',
                     unicodedata.normalize('NFD', 'Inbox Arşivi')):
            self.assertEqual(yakala._name_words(name), hygiene._name_words(name), name)
        for name in ('📥 000-Inbox', '00_INBOX', 'Gelen Kutusu', 'GelenKutum', 'Gelen Belgeler', '🔐 Kasa Inbox', 'Notlar'):
            doctor = hygiene._inbox_folder(name, None) and not hygiene.sensitive_excluded(name)
            self.assertEqual(yakala._inbox_name(name), doctor, name)
        # The doctor reports an archive that carries the word; yakala never writes into one by a guess.
        self.assertTrue(hygiene._inbox_folder('Inbox Arşivi', None))
        self.assertFalse(yakala._inbox_name(unicodedata.normalize('NFD', 'Inbox Arşivi')))

    def test_folder_outside_the_vault_is_refused_before_anything_is_written(self):
        outside = Path(self.tmp.name) / 'Disari'
        for spec in ('../Disari/Yakala', str(outside / 'Yakala'), 'Notlar/../../Disari', '', '.', '.obsidian/Yakala'):
            with self.assertRaises(ValueError, msg=spec):
                yakala.install(self.vault, self.state, hotkey=False, folder_spec=spec)
        with self.assertRaises(ValueError):
            yakala.main(['kur', '--kisayol-yok', '--klasor', '../Disari/Yakala'], vault=self.vault, state=self.state)
        self.assertEqual((outside.exists(), self.state.exists(), list(self.vault.iterdir())), (False, False, []))
        (self.vault / 'not.md').write_text('# not\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            yakala.chosen_inbox(self.vault, 'not.md')
        # Inside the vault every spelling means the same folder.
        for spec in ('Notlar/Yakala', 'Notlar\\Yakala/', 'Notlar/Gecici/../Yakala', str(self.vault / 'Notlar/Yakala')):
            self.assertEqual(yakala.chosen_inbox(self.vault, spec), 'Notlar/Yakala', spec)

    def test_chosen_folder_is_read_from_the_state_it_was_saved_in(self):
        import io
        installed = yakala.install(self.vault, self.state, hotkey=False, folder_spec='Notlar/Yakala')
        self.assertEqual(installed['klasor'], 'Notlar/Yakala')
        self.assertEqual(json.loads((self.state / 'yakala.json').read_text(encoding='utf-8'))['klasor'], 'Notlar/Yakala')
        self.assertEqual(yakala.status(self.vault, self.state)['klasor'], 'Notlar/Yakala')

        def cli(*argv):
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                yakala.main([*argv, '--json'], vault=self.vault, state=self.state)
            return json.loads(out.getvalue())
        self.assertEqual(cli('durum')['klasor'], 'Notlar/Yakala')
        self.assertTrue(cli('ekle', '--metin', 'deneme')['path'].startswith('Notlar/Yakala/'))
        self.assertEqual(cli('liste')['bekleyen'], 1)
        self.assertEqual(cli('sablon')['path'], 'Notlar/Yakala')
        self.assertIn('Yakalanan 1 kaynak bekliyor (Notlar/Yakala)', yakala.session_notice(self.vault, self.state))
        self.assertEqual(self.top(), ['Notlar'])
        # Another state directory knows nothing of that choice; `kur` without --klasor keeps it.
        self.assertEqual(yakala.find_inbox(self.vault, Path(self.tmp.name) / 'other-state'), INBOX)
        again = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual(again['klasor'], 'Notlar/Yakala')
        template = json.loads((self.vault / again['web_clipper_sablonu']).read_text(encoding='utf-8'))
        skill = (self.vault / '.agents/skills/beyin-yakala/SKILL.md').read_text(encoding='utf-8')
        self.assertEqual(template['path'], 'Notlar/Yakala')
        self.assertIn('`Notlar/Yakala/`', skill)
        self.assertNotIn('000-Inbox', skill)

    def test_existing_cards_keep_their_folder_and_are_never_moved(self):
        # A vault as 3.9.0 left it: cards under the starter path, the user's own inbox beside it, no saved folder.
        (self.vault / INBOX).mkdir(parents=True)
        (self.vault / '000-Inbox').mkdir()
        waiting = yakala.capture(self.vault, url='https://ornek.com/eski', state=self.state)
        done = yakala.find_card(self.vault, yakala.capture(self.vault, text='bitti', state=self.state)['id'], self.state)
        yakala.write_card(done['path'], dict(done['meta'], durum='islendi'), done['body'])
        self.state.mkdir()
        (self.state / 'yakala.json').write_text('{"schema": 1, "session_notice": true, "kisayol": null, "tus": "ctrl+alt+b"}\n',
                                                encoding='utf-8')
        self.assertTrue(waiting['path'].startswith(INBOX + '/'))
        self.assertEqual((yakala.find_inbox(self.vault, self.state), yakala.pending(self.vault, self.state)), (INBOX, 1))
        same = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual((same['klasor'], 'onceki_klasor' in same), (INBOX, False))
        # The user picks the other folder: the queue that stays behind is reported, not moved.
        moved = yakala.install(self.vault, self.state, hotkey=False, folder_spec='000-Inbox/Yakala')
        self.assertEqual((moved['klasor'], moved['onceki_klasor'], moved['onceki_klasorde_kalan']), ('000-Inbox/Yakala', INBOX, 1))
        self.assertIn('UYARI: 1 kart eski klasorde kaldi (' + INBOX + ')', yakala.human(moved, 'kur'))
        self.assertTrue((self.vault / waiting['path']).is_file())
        self.assertEqual(yakala.pending(self.vault, self.state), 0)
        self.assertIn('000-Inbox/Yakala/', yakala.capture(self.vault, text='yeni', state=self.state)['path'])

    def test_saved_folder_that_is_gone_or_edited_is_not_trusted(self):
        (self.vault / '000-Inbox').mkdir()
        self.assertEqual(yakala.install(self.vault, self.state, hotkey=False)['klasor'], '000-Inbox/Yakala')
        yakala.capture(self.vault, text='not', state=self.state)
        (self.vault / '000-Inbox').rename(self.vault / '00_INBOX')  # the saved folder no longer exists
        self.assertEqual((yakala.find_inbox(self.vault, self.state), yakala.pending(self.vault, self.state)), ('00_INBOX/Yakala', 1))
        (Path(self.tmp.name) / 'Disari').mkdir()
        for saved in ('"../Disari"', '7', '[]'):
            (self.state / 'yakala.json').write_text('{"schema": 1, "klasor": ' + saved + '}\n', encoding='utf-8')
            self.assertEqual(yakala.find_inbox(self.vault, self.state), '00_INBOX/Yakala', saved)

    def test_nfd_folder_name_round_trips(self):
        import unicodedata
        nfc = 'Günlük Inbox'
        (self.vault / unicodedata.normalize('NFD', nfc)).mkdir()
        on_disk = self.top()[0]  # the file system decides which form it keeps
        installed = yakala.install(self.vault, self.state, hotkey=False)
        self.assertEqual(installed['klasor'], on_disk + '/Yakala')
        self.assertTrue(yakala.capture(self.vault, text='not', state=self.state)['path'].startswith(on_disk + '/Yakala/'))
        template = json.loads((self.vault / installed['web_clipper_sablonu']).read_text(encoding='utf-8'))
        self.assertEqual((template['path'], yakala.status(self.vault, self.state)['klasor']), (on_disk + '/Yakala',) * 2)
        self.assertIn('(' + on_disk + '/Yakala)', yakala.session_notice(self.vault, self.state))
        if (self.vault / nfc).is_dir():  # APFS and HFS+ read both forms as one name; ext4 and NTFS keep them apart
            again = yakala.install(self.vault, self.state, hotkey=False, folder_spec=nfc + '/Yakala')
            self.assertEqual((again['klasor'], 'onceki_klasor' in again), (on_disk + '/Yakala', False))
            self.assertEqual((self.top(), yakala.pending(self.vault, self.state)), ([on_disk], 1))


class YakalaInstalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-yakala-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'Örnek Beyin', root / 'state'
        self.vault.mkdir()
        self.env = inherited_env(BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                                 HOME=str(root / 'home'), APPDATA=str(root / 'appdata'))
        self.env.pop('BEYIN_V3_FILTER_HARNESS_TURNS', None)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state)], capture_output=True, text=True, encoding='utf-8',
                                env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def entry(self, *args):
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', *args], capture_output=True,
                                text=True, encoding='utf-8', env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def session_start(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': 's1', 'event_id': 's1-1', 'cwd': str(self.vault)}
        result = subprocess.run([sys.executable, str(self.vault / '.claude/scripts/beyin_v3_hook.py'), '--vault',
                                 str(self.vault), '--state', str(self.state), '--harness', 'claude'],
                                input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                                env=self.env, cwd=self.vault, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_installed_module_and_entry(self):
        self.assertTrue((self.vault / '.claude/scripts/beyin_v3_yakala.py').is_file())
        added = self.entry('ekle', 'https://x.com/a/status/7', '--neden', 'okunacak')
        self.assertEqual(added['kaynak_turu'], 'x')
        listed = self.entry('liste')
        self.assertEqual(listed['bekleyen'], 1)

    def test_session_notice_is_opt_in(self):
        self.entry('ekle', 'https://ornek.com/yazi')
        self.assertNotIn('Yakalanan', self.session_start())
        installed = self.entry('kur', '--kisayol-yok')
        self.assertIsNone(installed['kisayol'])
        self.assertTrue((self.state / 'yakala.json').is_file())
        skill = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        self.assertIn('python3 beyin.py yakala isle', skill.read_text(encoding='utf-8'))
        self.assertTrue((self.vault / installed['web_clipper_sablonu']).is_file())
        self.assertIn('Yakalanan 1 kaynak bekliyor', self.session_start())
        removed = self.entry('kaldir')
        self.assertTrue(removed['notlar_korundu'])
        self.assertFalse(skill.exists())
        self.assertFalse((self.state / 'yakala.json').exists())
        self.assertEqual(len(list((self.vault / INBOX).glob('*.md'))), 1)
        self.assertNotIn('Yakalanan', self.session_start())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Start menu and Send To shortcuts')
    def test_windows_shortcuts(self):
        appdata = Path(self.env['APPDATA'])
        installed = self.entry('kur')
        self.assertEqual(installed['kisayol'], 'Ctrl+Alt+B')
        start = appdata / 'Microsoft/Windows/Start Menu/Programs/Beyne At.lnk'
        sendto = appdata / 'Microsoft/Windows/SendTo/Beyne At.lnk'
        self.assertTrue(start.is_file() and sendto.is_file())
        self.assertIsInstance(yakala.windows_context(), dict)
        self.entry('kaldir')
        self.assertFalse(start.exists() or sendto.exists())

    def test_user_skill_with_same_name_is_kept(self):
        own = self.vault / '.agents/skills/beyin-yakala/SKILL.md'
        own.parent.mkdir(parents=True)
        own.write_text('kendi skill\'im\n', encoding='utf-8')
        self.entry('kur', '--kisayol-yok')
        self.entry('kaldir')
        self.assertEqual(own.read_text(encoding='utf-8'), 'kendi skill\'im\n')

    def test_renamed_inbox_through_the_installed_entry(self):
        (self.vault / '000-Inbox').mkdir()
        refused = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'yakala', 'kur', '--kisayol-yok', '--klasor',
                                  '../Disari/Yakala'], capture_output=True, text=True, encoding='utf-8', env=self.env,
                                 cwd=self.vault, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('vault\'un icinde olmali', refused.stderr)
        self.assertFalse((Path(self.tmp.name) / 'Disari').exists() or (self.state / 'yakala.json').exists())
        installed = self.entry('kur', '--kisayol-yok')
        self.assertEqual(installed['klasor'], '000-Inbox/Yakala')
        self.assertEqual(installed['web_clipper_sablonu'], '000-Inbox/Yakala/beyne-at-web-clipper.json')
        self.assertTrue(self.entry('ekle', 'https://ornek.com/yazi')['path'].startswith('000-Inbox/Yakala/'))
        self.assertIn('Yakalanan 1 kaynak bekliyor (000-Inbox/Yakala)', self.session_start())
        self.assertEqual((self.entry('durum')['klasor'], self.entry('sablon')['path']), ('000-Inbox/Yakala',) * 2)
        skill = (self.vault / '.agents/skills/beyin-yakala/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('`000-Inbox/Yakala/`', skill)
        self.assertFalse((self.vault / '📥 000-Inbox').exists())


if __name__ == '__main__':
    unittest.main()
