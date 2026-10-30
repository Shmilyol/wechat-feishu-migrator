"""Offline synthetic-memory / WinAPI-mock tests. Never inspect a real process."""
import contextlib
import ctypes
import io
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import windows_wechat as ww


class FakeAPI:
    """Remote memory is a bytearray; handles are synthetic integers."""
    def __init__(self, memory=b'', base=ww.MIN_ADDRESS):
        self.memory, self.base = bytearray(memory), base
        self.entries = [(11, 'Weixin.exe')]
        self.paths = {11: r'C:\Program Files\Tencent\Weixin.exe'}
        self.versions = {self.paths[11]: '4.2.0.0'}
        self.denied = set()
        self.arch_x64 = True
        self.opens, self.closed, self.reads = [], [], []
        self.bad_reads = set()
        self.extra_regions = []

    def process_entries(self):
        return iter(self.entries)

    @contextlib.contextmanager
    def process(self, pid, memory=False):
        self.opens.append((pid, memory))
        if pid in self.denied:
            raise OSError('mock denied')
        try:
            yield pid
        finally:
            self.closed.append((pid, memory))

    def image(self, handle):
        return self.paths[handle]

    def file_version(self, exe):
        return self.versions.get(exe)

    def is_x64(self, handle):
        return self.arch_x64

    def query(self, handle, address):
        for region in self.extra_regions:
            if region[0] <= address < region[0] + region[1]:
                return region
        if self.base <= address < self.base + len(self.memory):
            return self.base, len(self.memory), ww.MEM_COMMIT, 0x04
        return None

    def read(self, handle, address, size):
        self.reads.append((handle, address, size))
        if address in self.bad_reads:
            return b'partial'[:max(0, min(size - 1, 7))]
        offset = address - self.base
        if offset < 0 or offset + size > len(self.memory):
            return None
        return bytes(self.memory[offset:offset + size])


def synthetic_memory(key=b'K' * 32, salt=b'S' * 16, marker_offset=127, reference_offset=503):
    memory = bytearray(4096)
    base = ww.MIN_ADDRESS
    config, blob = base + 1600, base + 2200
    memory[marker_offset:marker_offset + len(ww.CONFIG_NAME)] = ww.CONFIG_NAME
    node = reference_offset - 0x10
    struct.pack_into('<QQ', memory, reference_offset, base + marker_offset, len(ww.CONFIG_NAME))
    struct.pack_into('<Q', memory, node + 0x28, config)
    literal = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
    encoded = bytes(value ^ ww.CIPHER_MASK[index % 32] for index, value in enumerate(literal))
    struct.pack_into('<QQ', memory, config - base + 0x90, blob, len(encoded))
    memory[blob - base:blob - base + len(encoded)] = encoded
    return memory


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.key, self.salt = b'K' * 32, b'S' * 16
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'synthetic.db'
        self.database.write_bytes(self.salt + b'synthetic encrypted page')
        self.api = FakeAPI(synthetic_memory(self.key, self.salt))
        patcher = patch.object(ww, '_WindowsAPI', return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def find(self, verifier=None, **kwargs):
        return ww.find_db_key(self.database, verifier or (lambda key: key == self.key),
                              max_bytes_per_process=32768, chunk_size=128, **kwargs)

    def test_public_discovery_includes_actual_file_version_and_only_target_names(self):
        self.api.entries += [(12, 'WeChat.exe'), (13, 'WeChatAppEx.exe'), (14, 'other.exe'),
                             (15, 'Weixin.exe.bak'), (11, 'Weixin.exe')]
        self.api.paths[12] = r'D:\微信\WeChat.exe'
        self.api.versions[self.api.paths[12]] = '4.1.15.0'
        processes = ww.discover_processes()
        self.assertEqual(processes, [
            {'pid': 11, 'exe': self.api.paths[11], 'version': '4.2.0.0'},
            {'pid': 12, 'exe': self.api.paths[12], 'version': '4.1.15.0'},
        ])
        self.assertEqual(self.api.opens, [(11, False), (12, False)])
        self.assertEqual(self.api.reads, [])
        self.assertEqual(len(self.api.closed), 2)

    def test_verified_key_with_marker_and_reference_across_chunk_boundaries(self):
        verifier = Mock(side_effect=lambda candidate: candidate == self.key)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.find(verifier), self.key)
        self.assertEqual(output.getvalue(), '')
        verifier.assert_called_once_with(self.key)
        self.assertIn((11, True), self.api.closed)
        self.assertTrue(all(handle == 11 for handle, _, _ in self.api.reads))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.database])

    def test_known_4_1_and_later_minor_versions_use_structure_probe(self):
        for version in ('4.1.15.0', '4.2.0.0', '4.9.123.456'):
            with self.subTest(version=version):
                self.api.versions[self.api.paths[11]] = version
                self.assertEqual(self.find(), self.key)

    def test_never_returns_unverified_key(self):
        verify = Mock(return_value=False)
        with self.assertRaisesRegex(ww.WindowsWechatError, '未.*验证'):
            self.find(verify)
        verify.assert_called_once_with(self.key)
        self.assertIn((11, True), self.api.closed)

    def test_wrong_salt_is_filtered_before_callback(self):
        self.api.memory = synthetic_memory(self.key, b'T' * 16)
        verify = Mock(return_value=True)
        with self.assertRaises(ww.WindowsWechatError):
            self.find(verify)
        verify.assert_not_called()

    def test_invalid_blob_lengths_and_pointers_do_not_reach_verifier(self):
        for pointer, length in ((0, 99), (ww.MAX_ADDRESS, 99),
                                (ww.MIN_ADDRESS + 2200, 0), (ww.MIN_ADDRESS + 2200, 1025)):
            with self.subTest(pointer=pointer, length=length):
                self.api.memory = synthetic_memory()
                struct.pack_into('<QQ', self.api.memory, 1600 + 0x90, pointer, length)
                verify = Mock(return_value=True)
                with self.assertRaises(ww.WindowsWechatError):
                    self.find(verify)
                verify.assert_not_called()

    def test_plaintext_unlinked_literal_is_not_scanned_as_a_key(self):
        self.api.memory = bytearray(b"x'" + self.key.hex().encode() + b"'")
        verify = Mock(return_value=True)
        with self.assertRaises(ww.WindowsWechatError):
            self.find(verify)
        verify.assert_not_called()

    def test_partial_reads_are_rejected_and_not_falsely_joined(self):
        self.api.bad_reads.add(ww.MIN_ADDRESS)
        verify = Mock(return_value=True)
        with self.assertRaises(ww.WindowsWechatError):
            self.find(verify)
        verify.assert_not_called()

    def test_guarded_memory_is_not_read(self):
        self.api.extra_regions = [(ww.MIN_ADDRESS, 4096, ww.MEM_COMMIT, 0x104)]
        with self.assertRaises(ww.WindowsWechatError):
            self.find()
        self.assertEqual(self.api.reads, [])

    def test_pointer_graph_in_guarded_region_is_not_read(self):
        # The ordinary scan cannot see this marker because the region is guarded.
        reader = ww._Reader(self.api, 11, 4096, float('inf'))
        self.api.extra_regions = [(ww.MIN_ADDRESS, 4096, ww.MEM_COMMIT, 0x104)]
        self.assertIsNone(reader.read(ww.MIN_ADDRESS + 128, 64))
        self.assertEqual(self.api.reads, [])

    def test_pid_reuse_to_another_application_is_rejected_before_memory_read(self):
        original_image = self.api.image
        calls = 0

        def changed_image(handle):
            nonlocal calls
            calls += 1
            return original_image(handle) if calls == 1 else r'C:\Other.exe'

        self.api.image = changed_image
        with self.assertRaises(ww.WindowsWechatError):
            self.find()
        self.assertEqual(self.api.reads, [])
        self.assertIn((11, True), self.api.closed)

    def test_unknown_old_future_major_and_non_x64_processes_are_not_read(self):
        for version in (None, 'unknown', '4.0.9.0', '3.9.1.0', '5.0.0.0'):
            with self.subTest(version=version):
                self.api.versions[self.api.paths[11]] = version
                with self.assertRaisesRegex(ww.WindowsWechatError, '版本'):
                    self.find()
                self.assertEqual(self.api.reads, [])
        self.api.versions[self.api.paths[11]] = '4.2.0.0'
        self.api.arch_x64 = False
        with self.assertRaises(ww.WindowsWechatError):
            self.find()
        self.assertEqual(self.api.reads, [])

    def test_duplicate_nodes_verify_each_candidate_only_once(self):
        # Both references resolve to the same WCDB object/blob.
        struct.pack_into('<QQ', self.api.memory, 900, ww.MIN_ADDRESS + 127, len(ww.CONFIG_NAME))
        struct.pack_into('<Q', self.api.memory, 900 - 0x10 + 0x28, ww.MIN_ADDRESS + 1600)
        verify = Mock(return_value=False)
        with self.assertRaises(ww.WindowsWechatError):
            self.find(verify)
        verify.assert_called_once_with(self.key)

    def test_partial_graph_reads_are_rejected(self):
        for address in (ww.MIN_ADDRESS + 503 - 0x10,
                        ww.MIN_ADDRESS + 1600 + 0x88, ww.MIN_ADDRESS + 2200):
            with self.subTest(address=address):
                self.api.bad_reads = {address}
                verify = Mock(return_value=True)
                with self.assertRaises(ww.WindowsWechatError):
                    self.find(verify)
                verify.assert_not_called()

    def test_reader_rejects_invalid_address_ranges(self):
        reader = ww._Reader(self.api, 11, 8192, float('inf'))
        for address, size in ((0, 64), (ww.MIN_ADDRESS, 0),
                              (ww.MAX_ADDRESS, 2), (ww.MAX_ADDRESS + 1, 1)):
            self.assertIsNone(reader.read(address, size))
        self.assertEqual(self.api.reads, [])

    def test_unknown_version_is_reported_as_none_in_discovery(self):
        self.api.versions.clear()
        self.assertIsNone(ww.discover_processes()[0]['version'])

    def test_permission_failure_is_skipped_and_next_process_is_usable(self):
        self.api.entries.insert(0, (10, 'WeChat.exe'))
        self.api.denied.add(10)
        self.assertEqual(self.find(), self.key)
        self.assertNotIn((10, True), self.api.opens)

    def test_global_process_byte_budget_includes_graph_and_failed_reads(self):
        reader = ww._Reader(self.api, 11, 8192, float('inf'))
        self.assertIsNone(ww._scan(reader, self.salt, lambda key: False, 128))
        self.assertLessEqual(reader.bytes_requested, 8192)
        self.assertEqual(reader.bytes_requested, sum(size for _, _, size in self.api.reads))
        self.api.reads.clear()
        self.api.bad_reads.add(ww.MIN_ADDRESS)
        reader = ww._Reader(self.api, 11, 128, float('inf'))
        self.assertIsNone(reader.read(ww.MIN_ADDRESS, 128))
        self.assertEqual(reader.bytes_requested, 128)
        with self.assertRaises(ww._LimitReached):
            reader.read(ww.MIN_ADDRESS, 1)

    def test_deadline_stops_before_any_memory_api(self):
        reader = ww._Reader(self.api, 11, 8192, 5)
        with patch.object(ww.time, 'monotonic', return_value=5):
            self.assertIsNone(ww._scan(reader, self.salt, lambda key: True, 128))
        self.assertEqual(self.api.reads, [])
        self.assertEqual(reader.queries, 0)

    def test_query_bound_and_invalid_region_terminate(self):
        reader = ww._Reader(self.api, 11, 8192, float('inf'))
        reader.queries = ww.MAX_QUERIES
        with self.assertRaises(ww._LimitReached):
            reader.query(ww.MIN_ADDRESS)
        self.api.query = lambda *_: (ww.MIN_ADDRESS, 0, ww.MEM_COMMIT, 4)
        reader = ww._Reader(self.api, 11, 8192, float('inf'))
        self.assertEqual(list(reader.chunks(128, 15, 8192)), [])

    def test_verifier_errors_propagate_and_handle_is_closed(self):
        for error in (ValueError('mock validation failure'), OSError('mock verifier IO failure')):
            verify = Mock(side_effect=error)
            with self.assertRaises(type(error)) as caught:
                self.find(verify)
            self.assertIs(caught.exception, error)
            self.assertIn((11, True), self.api.closed)

    def test_invalid_optional_arguments_and_short_database(self):
        for kwargs in ({'timeout': 0}, {'timeout': float('nan')}, {'timeout': True},
                       {'max_bytes_per_process': 10}, {'max_bytes_per_process': 1.2},
                       {'chunk_size': 1}, {'chunk_size': 32.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ww.find_db_key(self.database, lambda _: True, **kwargs)
        with self.assertRaises(TypeError):
            ww.find_db_key(self.database, None)
        self.database.write_bytes(b'short')
        with self.assertRaisesRegex(ww.WindowsWechatError, '16'):
            self.find()
        self.assertEqual(self.api.reads, [])

    def test_key_literal_forms_strict_lengths_and_case(self):
        good = self.key.hex().encode()
        salt = self.salt.hex().encode()
        data = (b"X'" + good + b"' x'" + good + salt + b"' "
                b"x'" + good + b"00' x'" + b'z' * 64 + b"' "
                b"x'" + good + salt + b"00'")
        self.assertEqual(list(ww._literal_candidates(data, self.salt)), [self.key, self.key])


class WinAPITests(unittest.TestCase):
    def make_api(self):
        kernel, version = types.SimpleNamespace(), types.SimpleNamespace()
        kernel_functions = ['CreateToolhelp32Snapshot', 'Process32FirstW', 'Process32NextW',
                            'OpenProcess', 'CloseHandle', 'QueryFullProcessImageNameW',
                            'VirtualQueryEx', 'ReadProcessMemory', 'IsWow64Process2']
        version_functions = ['GetFileVersionInfoSizeW', 'GetFileVersionInfoW', 'VerQueryValueW']
        for name in kernel_functions:
            setattr(kernel, name, Mock(return_value=0))
        for name in version_functions:
            setattr(version, name, Mock(return_value=0))
        with patch.object(ww.sys, 'platform', 'win32'), \
                patch.object(ctypes, 'WinDLL', create=True,
                             side_effect=lambda name, **_: kernel if name == 'kernel32' else version):
            api = ww._WindowsAPI()
        return api, kernel, version

    def test_non_windows_and_32_bit_fail_before_loading_libraries(self):
        with patch.object(ww.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ww.WindowsWechatError, 'Windows'):
                ww._WindowsAPI()
        with patch.object(ww.sys, 'platform', 'win32'), patch.object(ctypes, 'sizeof', return_value=4):
            with self.assertRaisesRegex(ww.WindowsWechatError, '64.*32'):
                ww._WindowsAPI()

    def test_abi_sizes_offsets_and_all_function_signatures(self):
        api, kernel, version = self.make_api()
        self.assertEqual(ctypes.sizeof(ww._PROCESSENTRY32W), 568)
        self.assertEqual(ww._PROCESSENTRY32W.szExeFile.offset, 44)
        self.assertEqual(ctypes.sizeof(ww._MEMORY_BASIC_INFORMATION), 48)
        self.assertEqual(ww._MEMORY_BASIC_INFORMATION.RegionSize.offset, 24)
        for dll in (kernel, version):
            for name, function in vars(dll).items():
                with self.subTest(function=name):
                    self.assertTrue(function.argtypes)
                    self.assertIsNotNone(function.restype)
        self.assertIs(kernel.OpenProcess.restype, ctypes.c_void_p)
        self.assertIs(kernel.CreateToolhelp32Snapshot.restype, ctypes.c_void_p)
        self.assertIs(kernel.VirtualQueryEx.restype, ctypes.c_size_t)
        self.assertIs(kernel.ReadProcessMemory.argtypes[-1]._type_, ctypes.c_size_t)

    def test_snapshot_close_even_when_iterator_is_closed_early(self):
        api, kernel, _ = self.make_api()
        kernel.CreateToolhelp32Snapshot.return_value = 0x100000001

        def first(_, entry_pointer):
            entry = entry_pointer._obj
            entry.th32ProcessID = 11
            encoded = 'Weixin.exe'.encode('utf-16-le')
            ctypes.memmove(ctypes.addressof(entry) + ww._PROCESSENTRY32W.szExeFile.offset,
                           encoded, len(encoded))
            return 1

        kernel.Process32FirstW.side_effect = first
        entries = api.process_entries()
        self.assertEqual(next(entries), (11, 'Weixin.exe'))
        entries.close()
        kernel.CloseHandle.assert_called_once_with(0x100000001)

    def test_process_handle_is_pointer_width_and_closed_on_exception(self):
        api, kernel, _ = self.make_api()
        kernel.OpenProcess.return_value = 0x100000001
        with self.assertRaises(ValueError):
            with api.process(11, memory=True) as handle:
                self.assertEqual(handle, 0x100000001)
                raise ValueError('mock')
        kernel.OpenProcess.assert_called_once_with(ww.PROCESS_QUERY_INFORMATION | ww.PROCESS_VM_READ, False, 11)
        kernel.CloseHandle.assert_called_once_with(0x100000001)

    def test_invalid_handles_not_closed(self):
        api, kernel, _ = self.make_api()
        for handle in (None, 0, -1, ctypes.c_void_p(-1).value):
            kernel.OpenProcess.return_value = handle
            with self.assertRaises(OSError):
                with api.process(11):
                    self.fail('invalid handle yielded')
        kernel.CloseHandle.assert_not_called()

    def test_file_version_uses_real_version_resource_fields(self):
        api, _, version = self.make_api()
        info = ww._VS_FIXEDFILEINFO()
        info.dwSignature = 0xFEEF04BD
        info.dwFileVersionMS = (4 << 16) | 2
        info.dwFileVersionLS = 0
        version.GetFileVersionInfoSizeW.return_value = 200
        version.GetFileVersionInfoW.return_value = 1

        def query(_, root, pointer, length):
            self.assertEqual(root, '\\')
            pointer._obj.value = ctypes.addressof(info)
            length._obj.value = ctypes.sizeof(info)
            return 1

        version.VerQueryValueW.side_effect = query
        self.assertEqual(api.file_version(r'C:\Weixin.exe'), '4.2.0.0')
        info.dwSignature = 0
        self.assertIsNone(api.file_version(r'C:\Weixin.exe'))

    def test_read_process_memory_partial_and_failed_calls_are_rejected(self):
        api, kernel, _ = self.make_api()

        def partial(handle, address, buffer, size, count):
            count._obj.value = size - 1
            return 1

        kernel.ReadProcessMemory.side_effect = partial
        self.assertIsNone(api.read(11, ww.MIN_ADDRESS, 20))
        kernel.ReadProcessMemory.side_effect = None
        kernel.ReadProcessMemory.return_value = 0
        self.assertIsNone(api.read(11, ww.MIN_ADDRESS, 20))

    def test_architecture_detection_accepts_amd64_and_rejects_x86_arm64(self):
        api, kernel, _ = self.make_api()

        def machine(process_type, native_type):
            def populate(_, process, native):
                process._obj.value, native._obj.value = process_type, native_type
                return 1
            return populate

        for process_type, native_type, expected in (
                (0, ww.AMD64, True), (ww.AMD64, 0xAA64, True),
                (0x014C, ww.AMD64, False), (0, 0xAA64, False)):
            with self.subTest(process=process_type, native=native_type):
                kernel.IsWow64Process2.side_effect = machine(process_type, native_type)
                self.assertEqual(api.is_x64(11), expected)
        kernel.IsWow64Process2.side_effect = None
        kernel.IsWow64Process2.return_value = 0
        self.assertFalse(api.is_x64(11))


if __name__ == '__main__':
    unittest.main()
