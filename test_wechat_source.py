from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import wechat_source as source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def database(self, table='kFavEmoticonOrderTable'):
        path = self.root / 'fixture.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA page_size=4096')
            db.execute('CREATE TABLE kNonStoreEmoticonTable (md5 TEXT, cdn_url TEXT, tp_url TEXT, thumb_url TEXT)')
            db.execute(f'CREATE TABLE {table} (md5 TEXT)')
            db.executemany('INSERT INTO kNonStoreEmoticonTable VALUES (?,?,?,?)', [
                ('a', 'https://example.com/a.gif', '', ''),
                ('b', '', '', 'https://example.com/thumb.png'),
                ('uncollected', 'https://example.com/other.gif', '', ''),
            ])
            db.executemany(f'INSERT INTO {table} VALUES (?)', [('b',), ('a',), ('a',)])
        return path

    def test_reads_only_favorites_in_order_and_reports_missing_original(self):
        records = source.read_favorites(self.database().read_bytes())
        self.assertEqual([r['id'] for r in records], ['b', 'a'])
        self.assertEqual(records[0]['urls'], [])
        self.assertEqual(records[1]['urls'], ['https://example.com/a.gif'])

    def test_missing_order_table_fails_instead_of_exporting_everything(self):
        with self.assertRaisesRegex(RuntimeError, '个人收藏顺序表'):
            source.read_favorites(self.database('unrelated').read_bytes())

    def test_account_discovery_requires_actual_db_and_explicit_selection(self):
        for name in ('wxid_a', 'wxid_b'):
            database = self.root / name / source.DB_RELATIVE
            database.parent.mkdir(parents=True)
            database.touch()
        (self.root / 'not-account').mkdir()
        self.assertEqual([a['id'] for a in source.find_accounts(self.root)], ['wxid_a', 'wxid_b'])
        with self.assertRaisesRegex(RuntimeError, '唯一微信账号'):
            source.select_account(self.root)
        self.assertEqual(source.select_account(self.root, 'wxid_b')['id'], 'wxid_b')

    def encrypted_page(self, key, raw=False, number=1):
        salt = bytes(range(16))
        cipher_key = key if raw else hashlib.pbkdf2_hmac('sha512', key, salt, 256000, 32)
        mac_key = hashlib.pbkdf2_hmac('sha512', cipher_key, bytes(v ^ 0x3a for v in salt), 2, 32)
        iv = bytes(range(16, 32))
        start = 16 if number == 1 else 0
        plain = bytes((i % 251 for i in range(4096 - 80 - start)))
        encryptor = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(plain) + encryptor.finalize()
        payload = ciphertext + iv
        mac = hmac.new(mac_key, payload + struct.pack('<I', number), 'sha512').digest()
        page = (salt if number == 1 else b'') + payload + mac
        expected = (source.SQLITE_HEADER if number == 1 else b'') + plain + bytes(80)
        return page, expected, cipher_key, mac_key

    def test_encrypted_key_modes_and_tamper_detection(self):
        key = bytes(range(32))
        for raw in (False, True):
            with self.subTest(raw=raw):
                page, expected, cipher_key, mac_key = self.encrypted_page(key, raw)
                self.assertTrue(source.verify_db_key(page, key))
                self.assertFalse(source.verify_db_key(page, bytes(32)))
                self.assertEqual(source.decrypt_page(page, 1, cipher_key, mac_key), expected)
                bad = page[:100] + bytes([page[100] ^ 1]) + page[101:]
                with self.assertRaises(RuntimeError):
                    source.decrypt_page(bad, 1, cipher_key, mac_key)

    def build_wal(self, frames):
        salt = b'12345678'
        header = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + salt
        checksum = source.wal_checksum(header, '<')
        wal = header + struct.pack('>II', *checksum)
        for number, size, page in frames:
            prefix = struct.pack('>II', number, size)
            checksum = source.wal_checksum(prefix + page, '<', checksum)
            wal += prefix + salt + struct.pack('>II', *checksum) + page
        return wal

    def test_wal_commits_only_and_validates_chain(self):
        page = bytes(4096)
        wal = self.build_wal([(1, 1, page), (1, 0, b'X' * 4096)])
        transactions = source.committed_wal_frames(wal)
        self.assertEqual(transactions, [(1, [(1, page)])])
        corrupt = bytearray(wal)
        corrupt[100] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'WAL 帧校验失败'):
            source.committed_wal_frames(bytes(corrupt))

    def test_wal_latest_committed_database_is_used(self):
        path = self.database()
        before = path.read_bytes()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DELETE FROM kFavEmoticonOrderTable WHERE md5="b"')
        after = path.read_bytes()
        frames = [(i // 4096 + 1, 0, after[i:i+4096]) for i in range(0, len(after), 4096)]
        n, _, page = frames[-1]
        frames[-1] = n, len(after) // 4096, page
        decoded = source.decode_database(before, self.build_wal(frames))
        self.assertEqual([r['id'] for r in source.read_favorites(decoded)], ['a'])

    def test_snapshot_and_source_are_unchanged(self):
        path = self.database()
        before = path.read_bytes()
        database, wal = source.snapshot_database(path)
        self.assertEqual(database, before)
        self.assertEqual(wal, b'')
        self.assertEqual(path.read_bytes(), before)

    def test_independent_export_download_and_group_pipeline(self):
        import contextlib
        import io
        from PIL import Image
        import migrate

        database = self.database()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE kNonStoreEmoticonTable SET cdn_url=? WHERE md5=?',
                       ('https://example.com/b.gif', 'b'))
        target = self.root / 'xwechat_files/wxid_fixture' / source.DB_RELATIVE
        target.parent.mkdir(parents=True)
        target.write_bytes(database.read_bytes())
        output = self.root / 'output'
        def image_bytes(color):
            data = io.BytesIO()
            Image.new('RGB', (2, 2), color).save(data, format='GIF')
            return data.getvalue()
        payloads = {'https://example.com/b.gif': image_bytes('red'),
                    'https://example.com/a.gif': image_bytes('blue')}
        with contextlib.ExitStack() as stack:
            for name, path in {'ROOT': output, 'EXPORT': output / 'exported',
                               'RAW_EXPORT': output / '.wxemoticon-cache',
                               'GROUP_MANIFEST': output / 'group_manifest.json'}.items():
                stack.enter_context(patch.object(migrate, name, path))
            stack.enter_context(patch.object(migrate.sys, 'platform', 'darwin'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            fetch = stack.enter_context(patch('sticker_download._fetch',
                side_effect=lambda opener, url, *args: payloads[url]))
            paths = migrate.run_export(str(self.root / 'xwechat_files'), 'wxid_fixture')
            self.assertEqual([p.read_bytes() for p in paths], list(payloads.values()))
            grouped = migrate.group_exports()
            self.assertEqual(len(grouped), 2)
            manifest = migrate.load_json(output / 'group_manifest.json', {})
            self.assertEqual(list(manifest['assignments']), [p.name for p in paths])
            migrate.run_export(str(self.root / 'xwechat_files'), 'wxid_fixture')
            self.assertEqual(fetch.call_count, 2)

    def test_url_extension_is_before_query_and_ampersand_decoded(self):
        urls = source.url_candidates('http://example.com/stodownload?m=abc&amp;key=1')
        self.assertEqual(urls[0], 'https://example.com/stodownload?m=abc&key=1')
        self.assertIn('https://example.com/stodownload.gif?m=abc&key=1', urls)


if __name__ == '__main__':
    unittest.main()
