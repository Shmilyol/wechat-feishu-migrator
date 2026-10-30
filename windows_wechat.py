"""Read-only discovery and bounded WCDB Config.Cipher scanning for Windows.

This is an independent ctypes implementation, based on format observations in
https://github.com/liusheng22/export-wechat-emoji/blob/main/cli/src/windows.rs
and cli/src/keyscan.rs. No upstream implementation is imported or executed.

The observed x64 WCDB layout is a *candidate profile*, not a promise that all
WeChat 4.x releases use it. Only a caller-supplied database verifier can establish
that a 32-byte candidate is the requested database's key. This module never
prints, saves, or returns unverified candidates. There is no process injection,
process control, privilege adjustment, or write API.
"""
from __future__ import annotations

import ctypes
import math
import ntpath
from pathlib import Path
import re
import struct
import sys
import time
from contextlib import ExitStack, contextmanager
from typing import Callable

# Fixed-width Windows ABI types also make offline structure tests portable.
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE_PROTECTIONS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
AMD64 = 0x8664
MIN_ADDRESS = 0x10000
MAX_ADDRESS = 0x00007FFFFFFFFFFF
WECHAT_NAMES = frozenset({'weixin.exe', 'wechat.exe'})
CONFIG_NAME = b'com.Tencent.WCDB.Config.Cipher'
# Observed repeating XOR mask: format data, not executable code.
CIPHER_MASK = bytes.fromhex(
    'd2c7442458020000004889442450488b450048844c2448488944254048584c24'
)
MAX_BLOB_SIZE = 1024
MAX_QUERIES = 200_000
MAX_MARKERS = 1024
MAX_REFERENCES = 8192
MAX_CANDIDATES = 4096
_KEY_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64}(?:[0-9a-fA-F]{32})?)'")


class WindowsWechatError(RuntimeError):
    """A platform, discovery, or verified-key lookup failure (never includes keys)."""


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', DWORD), ('cntUsage', DWORD), ('th32ProcessID', DWORD),
        ('th32DefaultHeapID', SIZE_T), ('th32ModuleID', DWORD),
        ('cntThreads', DWORD), ('th32ParentProcessID', DWORD),
        ('pcPriClassBase', ctypes.c_int32), ('dwFlags', DWORD),
        ('szExeFile', WORD * 260),
    ]


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', LPVOID), ('AllocationBase', LPVOID),
        ('AllocationProtect', DWORD), ('PartitionId', WORD),
        ('RegionSize', SIZE_T), ('State', DWORD), ('Protect', DWORD),
        ('Type', DWORD),
    ]


class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [(name, DWORD) for name in (
        'dwSignature', 'dwStrucVersion', 'dwFileVersionMS', 'dwFileVersionLS',
        'dwProductVersionMS', 'dwProductVersionLS', 'dwFileFlagsMask',
        'dwFileFlags', 'dwFileOS', 'dwFileType', 'dwFileSubtype',
        'dwFileDateMS', 'dwFileDateLS',
    )]


def _handle_value(handle):
    return handle.value if isinstance(handle, ctypes.c_void_p) else handle


def _valid_handle(handle):
    return _handle_value(handle) not in (None, 0, -1, ctypes.c_void_p(-1).value)


def _wechat_image(exe):
    return ntpath.basename(exe).lower() in WECHAT_NAMES


def _supported_version(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        return False
    parts = tuple(int(p) for p in version.split('.'))
    # Future major releases need a newly confirmed profile.
    return parts[0] == 4 and parts[1] >= 1


class _WindowsAPI:
    """Small read-only WinAPI boundary, replaced by mocks in offline tests."""

    def __init__(self):
        if sys.platform != 'win32':
            raise WindowsWechatError('此功能仅支持 Windows。')
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise WindowsWechatError('请使用 64 位 Python；32 位解释器无法安全扫描 x64 微信。')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        version = ctypes.WinDLL('version', use_last_error=True)

        def bind(dll, name, args, result):
            function = getattr(dll, name)
            function.argtypes = args
            function.restype = result
            return function

        self._snapshot = bind(kernel, 'CreateToolhelp32Snapshot', [DWORD, DWORD], HANDLE)
        self._first = bind(kernel, 'Process32FirstW', [HANDLE, ctypes.POINTER(_PROCESSENTRY32W)], BOOL)
        self._next = bind(kernel, 'Process32NextW', [HANDLE, ctypes.POINTER(_PROCESSENTRY32W)], BOOL)
        self._open = bind(kernel, 'OpenProcess', [DWORD, BOOL, DWORD], HANDLE)
        self._close = bind(kernel, 'CloseHandle', [HANDLE], BOOL)
        self._image = bind(kernel, 'QueryFullProcessImageNameW',
                           [HANDLE, DWORD, ctypes.POINTER(ctypes.c_wchar), ctypes.POINTER(DWORD)], BOOL)
        self._query = bind(kernel, 'VirtualQueryEx',
                           [HANDLE, LPVOID, ctypes.POINTER(_MEMORY_BASIC_INFORMATION), SIZE_T], SIZE_T)
        self._read = bind(kernel, 'ReadProcessMemory',
                          [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)], BOOL)
        try:
            self._machine = bind(kernel, 'IsWow64Process2',
                                 [HANDLE, ctypes.POINTER(WORD), ctypes.POINTER(WORD)], BOOL)
        except AttributeError as exc:
            raise WindowsWechatError('系统缺少 IsWow64Process2，无法可靠确认微信 x64 架构。') from exc
        self._version_size = bind(version, 'GetFileVersionInfoSizeW',
                                  [LPCWSTR, ctypes.POINTER(DWORD)], DWORD)
        self._version_info = bind(version, 'GetFileVersionInfoW',
                                  [LPCWSTR, DWORD, DWORD, LPVOID], BOOL)
        self._version_query = bind(version, 'VerQueryValueW',
                                   [LPVOID, LPCWSTR, ctypes.POINTER(LPVOID), ctypes.POINTER(DWORD)], BOOL)

    @contextmanager
    def process(self, pid, memory=False):
        rights = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ if memory else PROCESS_QUERY_LIMITED_INFORMATION
        handle = self._open(rights, False, pid)
        if not _valid_handle(handle):
            raise OSError('无法以只读权限打开微信进程。')
        try:
            yield handle
        finally:
            self._close(handle)

    def process_entries(self):
        snapshot = self._snapshot(TH32CS_SNAPPROCESS, 0)
        if not _valid_handle(snapshot):
            raise WindowsWechatError('无法枚举 Windows 进程。')
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not self._first(snapshot, ctypes.byref(entry)):
                return
            # Snapshot enumeration is finite; the additional bound rejects broken APIs.
            for _ in range(100_000):
                raw = bytes(entry.szExeFile)
                name = raw.decode('utf-16-le').split('\0', 1)[0]
                yield int(entry.th32ProcessID), name
                if not self._next(snapshot, ctypes.byref(entry)):
                    return
        finally:
            self._close(snapshot)

    def image(self, handle):
        buffer = ctypes.create_unicode_buffer(32768)
        size = DWORD(len(buffer))
        if not self._image(handle, 0, buffer, ctypes.byref(size)):
            raise OSError('无法查询微信可执行文件路径。')
        return buffer.value

    def file_version(self, exe):
        ignored = DWORD()
        size = self._version_size(exe, ctypes.byref(ignored))
        if not size or size > 16 * 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not self._version_info(exe, 0, size, buffer):
            return None
        pointer, length = LPVOID(), DWORD()
        if not self._version_query(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
            return None
        if not pointer.value or length.value < ctypes.sizeof(_VS_FIXEDFILEINFO):
            return None
        info = ctypes.cast(pointer, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            return None
        return '{}.{}.{}.{}'.format(info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xffff,
                                    info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xffff)

    def is_x64(self, handle):
        process_machine, native_machine = WORD(), WORD()
        if not self._machine(handle, ctypes.byref(process_machine), ctypes.byref(native_machine)):
            return False
        return process_machine.value == AMD64 or (process_machine.value == 0 and native_machine.value == AMD64)

    def query(self, handle, address):
        info = _MEMORY_BASIC_INFORMATION()
        result = self._query(handle, address, ctypes.byref(info), ctypes.sizeof(info))
        if result != ctypes.sizeof(info):
            return None
        return (info.BaseAddress or 0, info.RegionSize, info.State, info.Protect)

    def read(self, handle, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = SIZE_T()
        ok = self._read(handle, address, buffer, size, ctypes.byref(count))
        # Failed/partial reads must not be joined into invented contiguous memory.
        return buffer.raw if ok and count.value == size else None


def _discover(api):
    result = []
    seen = set()
    for pid, name in api.process_entries():
        if not _wechat_image(name) or pid in seen or pid <= 0:
            continue
        seen.add(pid)
        try:
            with api.process(pid) as handle:
                exe = api.image(handle)
                if _wechat_image(exe):
                    result.append({'pid': pid, 'exe': exe, 'version': api.file_version(exe)})
        except OSError:
            continue  # Process exited or cannot be inspected with ordinary read rights.
    return sorted(result, key=lambda item: item['pid'])


def discover_processes() -> list[dict]:
    """Return actual image paths and FILEVERSIONs for running Weixin/WeChat.

    Unknown versions are None, never guessed from an installation directory or
    registry. Other executables are filtered before opening a process handle.
    """
    return _discover(_WindowsAPI())


class _LimitReached(Exception):
    pass


def _readable(region):
    return region[2] == MEM_COMMIT and not region[3] & PAGE_GUARD and (region[3] & 0xff) in READABLE_PROTECTIONS


def _pointer(address, size=1):
    return MIN_ADDRESS <= address <= MAX_ADDRESS and 0 < size <= MAX_ADDRESS + 1 - address


class _Reader:
    def __init__(self, api, handle, byte_limit, deadline):
        self.api, self.handle = api, handle
        self.byte_limit, self.deadline = byte_limit, deadline
        self.bytes_requested = 0
        self.queries = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _LimitReached

    def query(self, address):
        self.check()
        if self.queries >= MAX_QUERIES:
            raise _LimitReached
        self.queries += 1
        return self.api.query(self.handle, address)

    def read(self, address, size, *, known_readable=False):
        self.check()
        if not _pointer(address, size):
            return None
        if self.bytes_requested + size > self.byte_limit:
            raise _LimitReached
        if not known_readable:
            cursor, end = address, address + size
            while cursor < end:
                region = self.query(cursor)
                if not region or not _readable(region):
                    return None
                base, region_size = region[:2]
                if not base <= cursor < base + region_size <= MAX_ADDRESS + 1:
                    return None
                cursor = min(end, base + region_size)
        # Count requested bytes, including failures and graph reads, not just successes.
        self.bytes_requested += size
        data = self.api.read(self.handle, address, size)
        return data if data is not None and len(data) == size else None

    def chunks(self, chunk_size, carry, phase_limit):
        address, requested = MIN_ADDRESS, 0
        while address <= MAX_ADDRESS and requested < phase_limit:
            region = self.query(address)
            if region is None:
                return
            base, size = region[:2]
            end = base + size
            if size <= 0 or base > address or end <= address or end > MAX_ADDRESS + 1:
                return
            if _readable(region):
                cursor = max(base, address)
                tail = b''
                while cursor < end and requested < phase_limit:
                    self.check()
                    wanted = min(chunk_size, end - cursor, phase_limit - requested,
                                 self.byte_limit - self.bytes_requested)
                    if wanted <= 0:
                        return
                    data = self.read(cursor, wanted, known_readable=True)
                    requested += wanted
                    if data is None:
                        tail = b''
                    else:
                        combined = tail + data
                        yield cursor - len(tail), combined
                        tail = combined[-carry:] if carry else b''
                    cursor += wanted
            address = end


def _literal_candidates(decoded, salt):
    """Conservatively accept the observed raw-key and raw-key+salt forms."""
    for match in _KEY_LITERAL.finditer(decoded):
        value = bytes.fromhex(match[1].decode('ascii'))
        if len(value) == 48 and value[32:] != salt:
            continue
        yield value[:32]


def _scan(reader, salt, verify_key, chunk_size):
    markers = set()
    seen_references, seen_blobs, seen_candidates = set(), set(), set()
    try:
        # Reserve half the byte budget for the reference pass and graph reads.
        for base, data in reader.chunks(chunk_size, len(CONFIG_NAME) - 1, reader.byte_limit // 2):
            position = data.find(CONFIG_NAME)
            while position >= 0:
                reader.check()
                markers.add(base + position)
                if len(markers) >= MAX_MARKERS:
                    break
                position = data.find(CONFIG_NAME, position + 1)
            if len(markers) >= MAX_MARKERS:
                break
        if not markers:
            return None
        # Search length words in C-implemented bytes.find instead of inspecting
        # every byte in Python. An exact name pointer establishes a reference.
        length_word = struct.pack('<Q', len(CONFIG_NAME))
        for base, data in reader.chunks(chunk_size, 15, reader.byte_limit - reader.bytes_requested):
            offset = data.find(length_word, 8)
            while offset >= 8:
                reader.check()
                name_ptr = struct.unpack_from('<Q', data, offset - 8)[0]
                reference = base + offset - 8
                if name_ptr in markers and reference not in seen_references:
                    if len(seen_references) >= MAX_REFERENCES:
                        raise _LimitReached
                    seen_references.add(reference)
                    node = reader.read(reference - 0x10, 0x50)
                    if node and struct.unpack_from('<QQ', node, 0x10) == (name_ptr, len(CONFIG_NAME)):
                        config = struct.unpack_from('<Q', node, 0x28)[0]
                        obj = reader.read(config + 0x88, 0x28) if _pointer(config, 0xb0) else None
                        if obj:
                            blob_ptr, blob_size = struct.unpack_from('<QQ', obj, 0x08)
                            location = (blob_ptr, blob_size)
                            if 0 < blob_size <= MAX_BLOB_SIZE and location not in seen_blobs:
                                seen_blobs.add(location)
                                blob = reader.read(blob_ptr, blob_size)
                                if blob:
                                    decoded = bytes(value ^ CIPHER_MASK[i % 32] for i, value in enumerate(blob))
                                    for candidate in _literal_candidates(decoded, salt):
                                        reader.check()
                                        if candidate in seen_candidates:
                                            continue
                                        if len(seen_candidates) >= MAX_CANDIDATES:
                                            raise _LimitReached
                                        seen_candidates.add(candidate)
                                        if verify_key(candidate):
                                            return candidate
                offset = data.find(length_word, offset + 1)
    except _LimitReached:
        return None
    return None


def find_db_key(database_path, verify_key: Callable[[bytes], bool], *,
                max_bytes_per_process=512 * 1024 * 1024, timeout=30.0,
                chunk_size=1024 * 1024) -> bytes:
    """Return exactly one verified 32-byte key, or raise WindowsWechatError.

    Read only 16 salt bytes from the supplied database. verify_key must validate
    candidates against that same database (e.g. first-page HMAC). Each process
    shares one cumulative byte budget across both passes and all pointer reads;
    time, query count, reference count and candidate count are also bounded.
    timeout is per process and cooperative: a blocking Windows call or caller
    verifier cannot be forcibly interrupted. Only the first 64/96-hex literal
    forms observed in the WCDB profile are supported; unknown layouts fail.
    """
    if not callable(verify_key):
        raise TypeError('verify_key 必须是验证 32 字节候选的可调用对象。')
    if isinstance(max_bytes_per_process, bool) or not isinstance(max_bytes_per_process, int) or max_bytes_per_process < 1024:
        raise ValueError('max_bytes_per_process 必须是至少 1024 的整数。')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout 必须是正的有限秒数。')
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 32 <= chunk_size <= 16 * 1024 * 1024:
        raise ValueError('chunk_size 必须是 32 到 16777216 之间的整数。')
    api = _WindowsAPI()
    processes = _discover(api)
    if not processes:
        raise WindowsWechatError('未找到可查询的 Weixin.exe / WeChat.exe，请先自行打开并登录微信。')
    supported = [process for process in processes if _supported_version(process['version'])]
    if not supported:
        raise WindowsWechatError('无法确认微信版本为受支持的 4.1+（4.x）；不会猜测版本或扫描未知版本。')
    with Path(database_path).open('rb') as database:
        salt = database.read(16)
    if len(salt) != 16:
        raise WindowsWechatError('数据库不足 16 字节，无法取得 salt。')
    scanned = 0
    for process in supported:
        with ExitStack() as handles:
            try:
                handle = handles.enter_context(api.process(process['pid'], memory=True))
                # Recheck identity after opening: the original process may have
                # exited and its PID may now belong to a different application.
                exe = api.image(handle)
                if not _wechat_image(exe) or ntpath.normcase(exe) != ntpath.normcase(process['exe']):
                    continue
                if not _supported_version(api.file_version(exe)) or not api.is_x64(handle):
                    continue
            except OSError:
                continue
            scanned += 1
            reader = _Reader(api, handle, max_bytes_per_process, time.monotonic() + timeout)
            # Verifier errors, including OSError, belong to the caller and must
            # propagate rather than being mistaken for a process access failure.
            key = _scan(reader, salt, verify_key, chunk_size)
            if key is not None:
                return key
    raise WindowsWechatError(
        f'未在 {scanned} 个已确认的 x64 微信进程的有限扫描中找到通过数据库验证的密钥。'
        '可能未打开目标数据库、读取受限、达到扫描上限，或当前 WCDB 布局暂不支持；不保证所有 4.1+ 版本兼容。'
    )
