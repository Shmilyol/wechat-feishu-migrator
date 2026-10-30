"""Offline regression tests; never access real WeChat data or control the UI."""
import contextlib
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import migrate


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / '用户 空格'
        self.root = root
        for name, value in {
            'ROOT': root, 'CONFIG': root / 'config.json',
            'STATE': root / 'state.json', 'EXPORT': root / 'exported',
            'RAW_EXPORT': root / '.wxemoticon-cache',
            'GROUP_MANIFEST': root / 'group_manifest.json',
        }.items():
            p = patch.object(migrate, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        p = patch.dict(migrate.os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict('sys.modules', {'psutil': Mock()})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(migrate, 'is_feishu_window', return_value=True)
        self.window_identity = p.start()
        self.addCleanup(p.stop)

    def test_grouping_stays_stable_and_repairs_missing_files(self):
        migrate.EXPORT.mkdir(parents=True)
        for i in range(101):
            (migrate.EXPORT / f'表情 {i:03}.gif').write_bytes(str(i).encode())
        with patch.object(migrate.os, 'link', side_effect=OSError('unsupported')):
            images = migrate.group_exports()
        self.assertEqual([sum(p.parent.name == str(g) for p in images) for g in (1, 2, 3)], [50, 50, 1])
        manifest = migrate.load_json(migrate.GROUP_MANIFEST, None)
        images[0].unlink()
        (migrate.RAW_EXPORT / '新增.png').write_bytes(b'new')
        images = migrate.group_exports()
        self.assertEqual(len(images), 102)
        updated = migrate.load_json(migrate.GROUP_MANIFEST, None)
        for name, info in manifest['assignments'].items():
            self.assertEqual(updated['assignments'][name], info)
        self.assertEqual(updated['assignments']['新增.png']['group'], 3)
        self.assertEqual((migrate.EXPORT / '1' / '表情 000.gif').read_bytes(), b'0')

    def test_export_uses_internal_module_without_external_command(self):
        self.root.mkdir()
        image = self.root / '收藏.gif'
        image.touch()
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch('wechat_source.export_favorites', return_value=[image]) as export, \
                patch.object(migrate.subprocess, 'run') as run:
            migrate.run_export('D:/微信/xwechat_files', 'wxid_a', 'key.txt')
        export.assert_called_once_with(migrate.RAW_EXPORT, self.root / 'download_manifest.json',
                                       data_dir='D:/微信/xwechat_files', wxid='wxid_a', key_file='key.txt')
        run.assert_not_called()
        self.assertEqual(migrate.load_json(self.root / 'source_order.json', []), ['收藏.gif'])

    def test_file_picker_sequences(self):
        cfg = {'add_button': [100, 200], 'picker_delay': 1, 'upload_delay': 1}
        self.root.mkdir()
        image = self.root / '中文 表情.gif'
        image.touch()
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                pg = Mock()
                with patch.object(migrate.sys, 'platform', platform), \
                        patch.object(migrate.time, 'sleep'), \
                        patch.object(migrate, 'require_windows_picker'), \
                        patch.object(migrate, 'set_clipboard') as clipboard:
                    migrate.choose_file(pg, image, cfg)
                if platform == 'win32':
                    self.assertIn(('alt', 'd'), [c.args for c in pg.hotkey.call_args_list])
                    self.assertIn(('alt', 'n'), [c.args for c in pg.hotkey.call_args_list])
                    self.assertEqual(clipboard.call_args_list[-1].args, ('"中文 表情.gif"',))
                else:
                    clipboard.assert_called_once_with(str(image.resolve()))
                    self.assertEqual(pg.press.call_count, 2)
                pg.click.assert_called_once_with(100, 200)

    def test_batch_boundaries_and_partial_confirmation(self):
        for count in (0, 1, 49, 50, 51, 101):
            pending = [(Path('1') / f'{i}.png', str(i)) for i in range(count)]
            batches = migrate.import_batches(pending, 50)
            self.assertEqual(sum(map(len, batches)), count)
            self.assertTrue(all(len(batch) <= 50 for batch in batches))
        pending = [(Path('1/a'), 'a'), (Path('2/b'), 'b')]
        self.assertEqual(list(map(len, migrate.import_batches(pending, 50))), [1, 1])
        self.assertEqual(migrate.confirmed_indices('1,3', 3), {0, 2})
        with self.assertRaises(ValueError):
            migrate.confirmed_indices('4', 3)

    def test_windows_batch_pastes_exact_file_list(self):
        self.root.mkdir()
        images = [self.root / f'表情 {i}.gif' for i in range(50)]
        for path in images:
            path.touch()
        pg = Mock()
        cfg = {'add_button': [1, 2], 'picker_delay': 1, 'upload_delay': 1}
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.object(migrate, 'require_windows_picker') as picker, \
                patch.object(migrate.time, 'sleep'), patch.object(migrate, 'set_clipboard') as clipboard:
            migrate.choose_files(pg, images, cfg)
        self.assertEqual(picker.call_count, 2)
        self.assertEqual(clipboard.call_args_list[-1].args[0], ' '.join('"' + p.name + '"' for p in images))
        with self.assertRaises(RuntimeError):
            migrate.choose_files(pg, images + [images[0]], cfg)

    def test_windows_clipboard_is_unicode(self):
        clipboard = Mock()
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.dict('sys.modules', {'pyperclip': clipboard}), \
                patch.object(migrate.subprocess, 'run') as run:
            migrate.set_clipboard('C:\\用户 空格\\表情.gif')
        clipboard.copy.assert_called_once_with('C:\\用户 空格\\表情.gif')
        run.assert_not_called()

    def test_windows_activation_handles_alias_and_minimized_window(self):
        window = Mock(title='Feishu', isMinimized=True, isActive=True)
        gw = Mock()
        gw.getAllWindows.return_value = [window]
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.dict('sys.modules', {'pygetwindow': gw}), \
                patch.object(migrate.time, 'sleep'):
            migrate.activate_feishu('飞书')
        window.restore.assert_called_once()
        window.activate.assert_called_once()

    def test_windows_activation_refuses_ambiguous_or_inactive_windows(self):
        for windows in ([], [Mock(title='飞书 - A'), Mock(title='飞书 - B')],
                        [Mock(title='飞书', isMinimized=False, isActive=False)]):
            gw = Mock()
            gw.getAllWindows.return_value = windows
            with self.subTest(count=len(windows)), \
                    patch.object(migrate.sys, 'platform', 'win32'), \
                    patch.dict('sys.modules', {'pygetwindow': gw}), \
                    patch.object(migrate.time, 'sleep'), \
                    self.assertRaises(RuntimeError):
                migrate.activate_feishu('飞书')

    def test_windows_activation_rejects_other_processes_with_matching_titles(self):
        self.window_identity.return_value = False
        window = Mock(title='飞书开放平台 - Google Chrome')
        gw = Mock()
        gw.getAllWindows.return_value = [window]
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.dict('sys.modules', {'pygetwindow': gw}), \
                self.assertRaises(RuntimeError):
            migrate.activate_feishu('飞书')
        window.activate.assert_not_called()

    def prepare_import(self, platform):
        folder = migrate.EXPORT / '1'
        folder.mkdir(parents=True)
        for i in range(2):
            (folder / f'{i}.png').write_bytes(bytes([i]))
        migrate.save_json(migrate.CONFIG, {'platform': platform, 'app_name': '飞书'})
        return types.SimpleNamespace(retry_attempted=False, limit=None, yes=True, pause_every=1)

    def test_import_reactivates_after_confirmation_and_records_attempts(self):
        args = self.prepare_import('win32')
        events = []
        answers = iter(['', 'y', 'y'])
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.object(migrate, 'ui_lib'), patch.object(migrate.time, 'sleep'), \
                patch('builtins.input', side_effect=lambda _: next(answers)), \
                patch.object(migrate, 'activate_feishu', side_effect=lambda _: events.append('activate')), \
                patch.object(migrate, 'choose_files', side_effect=lambda *a: events.append('choose')):
            migrate.migrate(args)
            self.assertEqual(events, ['activate', 'choose', 'activate', 'choose'])
            events.clear()
            migrate.migrate(args)
            self.assertEqual(events, [])
        state = migrate.load_json(migrate.STATE, None)
        self.assertEqual(len(state['attempted']), 2)
        self.assertEqual(len(state['verified']), 2)

    def test_partial_import_stops_and_retries_only_unconfirmed(self):
        args = self.prepare_import('win32')
        args.pause_every = 0
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.object(migrate, 'ui_lib'), patch.object(migrate.time, 'sleep'), \
                patch('builtins.input', side_effect=['', '1']), \
                patch.object(migrate, 'activate_feishu'), patch.object(migrate, 'choose_files'):
            migrate.migrate(args)
        state = migrate.load_json(migrate.STATE, None)
        self.assertEqual(len(state['attempted']), 2)
        self.assertEqual(len(state['verified']), 1)
        args.retry_attempted = True
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.object(migrate, 'ui_lib'), patch.object(migrate.time, 'sleep'), \
                patch('builtins.input', side_effect=['', 'y']), \
                patch.object(migrate, 'activate_feishu'), patch.object(migrate, 'choose_files') as choose:
            migrate.migrate(args)
        self.assertEqual(len(choose.call_args.args[1]), 1)
        self.assertEqual(len(migrate.load_json(migrate.STATE, {})['verified']), 2)

    def test_import_rejects_foreign_calibration_before_ui_actions(self):
        args = self.prepare_import('darwin')
        with patch.object(migrate.sys, 'platform', 'win32'), \
                patch.object(migrate, 'ui_lib') as ui, \
                self.assertRaisesRegex(RuntimeError, '其他系统'):
            migrate.migrate(args)
        ui.assert_not_called()

    def test_failed_picker_preserves_uncertainty_for_safe_retry(self):
        args = self.prepare_import('darwin')
        with patch.object(migrate.sys, 'platform', 'darwin'), \
                patch.object(migrate, 'ui_lib'), patch.object(migrate.time, 'sleep'), \
                patch('builtins.input'), patch.object(migrate, 'activate_feishu'), \
                patch.object(migrate, 'choose_files', side_effect=RuntimeError('picker failed')), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            migrate.migrate(args)
        self.assertEqual(len(migrate.load_json(migrate.STATE, {})['attempted']), 1)
        self.assertEqual(migrate.load_json(migrate.STATE, {})['verified'], {})

    def test_unsupported_platform_stops_before_export(self):
        with patch.object(migrate.sys, 'platform', 'linux'), \
                patch.object(migrate.sys, 'argv', ['migrate.py', '--export']), \
                patch.object(migrate, 'move_old_flat_exports') as move, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(migrate.main(), 1)
        move.assert_not_called()


if __name__ == '__main__':
    unittest.main()
