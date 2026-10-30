"""Read personal sticker metadata from a local WeChat SQLCipher database."""
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import re
import sqlite3
import struct
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

PAGE_SIZE = 4096
RESERVE = 80
SQLITE_HEADER = b'SQLite format 3\0'
DB_RELATIVE = Path('db_storage/emoticon/emoticon.db')
MAX_DB_BYTES = 512 * 1024 * 1024


def data_roots(explicit=None):
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise RuntimeError(f'微信数据目录不存在：{root}')
        return [root]
    home = Path.home()
    candidates = [home / 'xwechat_files', home / 'Documents/xwechat_files']
    if sys.platform == 'win32':
        # Documents may be redirected to OneDrive.
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        function = shell.SHGetFolderPathW
        function.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
        function.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(32768)
        if function(None, 5, None, 0, buffer) == 0:
            candidates.append(Path(buffer.value) / 'xwechat_files')
    else:
        candidates.append(home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files')
    return list(dict.fromkeys(p.resolve() for p in candidates if p.is_dir()))


def find_accounts(data_dir=None):
    accounts = []
    for root in data_roots(data_dir):
        for account in sorted(root.iterdir()):
            if account.is_dir() and not account.name.startswith('.'):
                database = account / DB_RELATIVE
                if database.is_file():
                    accounts.append({'id': account.name, 'database': database.resolve()})
    return accounts


def select_account(data_dir=None, wxid=None):
    accounts = find_accounts(data_dir)
    if wxid:
        accounts = [a for a in accounts if a['id'] == wxid]
    if len(accounts) != 1:
        names = ', '.join(a['id'] for a in accounts) or '无'
        raise RuntimeError(f'需要唯一微信账号，当前匹配：{names}。使用 --list-accounts 查看，再指定 --wechat-data-dir 和 --wxid。')
    return accounts[0]


def read_bounded(path):
    with path.open('rb') as stream:
        content = stream.read(MAX_DB_BYTES + 1)
    if len(content) > MAX_DB_BYTES:
        raise RuntimeError(f'数据库或 WAL 超过读取上限：{path.name}')
    return content


def snapshot_database(path):
    """Copy a stable DB/WAL pair in memory without writing to WeChat's files."""
    path = Path(path)
    wal_path = path.with_name(path.name + '-wal')

    def stamp(file):
        try:
            stat = file.stat()
            return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino
        except FileNotFoundError:
            return None

    for _ in range(3):
        before = stamp(path), stamp(wal_path)
        try:
            database = read_bounded(path)
            wal = read_bounded(wal_path) if before[1] else b''
        except FileNotFoundError:
            continue
        if before == (stamp(path), stamp(wal_path)):
            if not database or len(database) % PAGE_SIZE:
                raise RuntimeError('emoticon.db 不是完整的 4096 字节分页数据库。')
            return database, wal
    raise RuntimeError('微信数据库正在变化，未取得一致快照。请暂停收藏操作后重试。')


def derive_keys(key, salt):
    if len(key) != 32:
        raise RuntimeError('数据库密钥必须为 32 字节。')
    encrypted_key = hashlib.pbkdf2_hmac('sha512', key, salt, 256000, 32)
    mac_salt = bytes(v ^ 0x3a for v in salt)
    for cipher_key in (encrypted_key, key):
        mac_key = hashlib.pbkdf2_hmac('sha512', cipher_key, mac_salt, 2, 32)
        yield cipher_key, mac_key


def page_matches(page, number, mac_key):
    start = 16 if number == 1 else 0
    digest = hmac.new(mac_key, page[start:PAGE_SIZE - 64] + struct.pack('<I', number), 'sha512').digest()
    return hmac.compare_digest(digest, page[PAGE_SIZE - 64:])


def matching_keys(first_page, key):
    for cipher_key, mac_key in derive_keys(key, first_page[:16]):
        if page_matches(first_page, 1, mac_key):
            return cipher_key, mac_key
    return None


def verify_db_key(first_page, key):
    return len(first_page) == PAGE_SIZE and matching_keys(first_page, key) is not None


def decrypt_page(page, number, cipher_key, mac_key):
    if len(page) != PAGE_SIZE or not page_matches(page, number, mac_key):
        raise RuntimeError(f'数据库第 {number} 页校验失败，密钥错误或快照不一致。')
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError('缺少 cryptography，请安装 requirements.txt。') from exc
    start = 16 if number == 1 else 0
    end = PAGE_SIZE - RESERVE
    decryptor = Cipher(algorithms.AES(cipher_key), modes.CBC(page[end:end + 16])).decryptor()
    content = decryptor.update(page[start:end]) + decryptor.finalize()
    return (SQLITE_HEADER if number == 1 else b'') + content + bytes(RESERVE)


def wal_checksum(data, endian, initial=(0, 0)):
    if len(data) % 8:
        raise RuntimeError('WAL 校验块长度错误。')
    a, b = initial
    for x, y in struct.iter_unpack(endian + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def committed_wal_frames(wal):
    """Validate SQLite's checksum chain; ignore an unfinished trailing transaction."""
    if not wal:
        return []
    if len(wal) < 32:
        raise RuntimeError('WAL 头不完整，请等待微信完成写入后重试。')
    magic, version, size = struct.unpack('>III', wal[:12])
    if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != PAGE_SIZE:
        raise RuntimeError('不支持的 WAL 格式或页大小。')
    endian = '<' if magic == 0x377f0682 else '>'
    checksum = wal_checksum(wal[:24], endian)
    if checksum != struct.unpack('>II', wal[24:32]):
        raise RuntimeError('WAL 头校验失败。')
    result, pending = [], []
    frame_size = 24 + PAGE_SIZE
    for offset in range(32, len(wal) - frame_size + 1, frame_size):
        frame = wal[offset:offset + frame_size]
        if frame[8:16] != wal[16:24]:
            break  # Old frames after WAL reuse are not part of this generation.
        expected = wal_checksum(frame[:8] + frame[24:], endian, checksum)
        if expected != struct.unpack('>II', frame[16:24]):
            raise RuntimeError('WAL 帧校验失败，请重新取得快照。')
        checksum = expected
        page_number, database_size = struct.unpack('>II', frame[:8])
        if not page_number or page_number * PAGE_SIZE > MAX_DB_BYTES or database_size * PAGE_SIZE > MAX_DB_BYTES:
            raise RuntimeError('WAL 页号或数据库大小无效。')
        pending.append((page_number, frame[24:]))
        if database_size:
            result.append((database_size, pending))
            pending = []
    return result


def decode_database(database, wal, key=None):
    plain = database.startswith(SQLITE_HEADER)
    if plain:
        decode = lambda page, number: page
    else:
        if key is None:
            raise RuntimeError('缺少数据库密钥。')
        keys = matching_keys(database[:PAGE_SIZE], key)
        if keys is None:
            raise RuntimeError('数据库密钥验证失败。')
        decode = lambda page, number: decrypt_page(page, number, *keys)
    pages = {i // PAGE_SIZE + 1: decode(database[i:i + PAGE_SIZE], i // PAGE_SIZE + 1)
             for i in range(0, len(database), PAGE_SIZE)}
    for size, frames in committed_wal_frames(wal):
        transaction = {number: decode(page, number) for number, page in frames}
        pages.update(transaction)
        pages = {number: page for number, page in pages.items() if number <= size}
        if len(pages) != size:
            raise RuntimeError('WAL 提交后数据库存在缺页。')
    # A standalone SQLite snapshot must not request a companion WAL file.
    first = bytearray(pages[1])
    first[18:20] = b'\x01\x01'
    pages[1] = bytes(first)
    return b''.join(pages[number] for number in range(1, len(pages) + 1))


def url_candidates(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if not isinstance(value, str):
        return []
    urls = []
    for raw in re.findall(r'https?://[^\s<>"\x00]+', value):
        url = raw.replace('&amp;', '&')
        parts = urlsplit(url)
        if parts.scheme == 'http':
            parts = parts._replace(scheme='https')
        urls.append(urlunsplit(parts))
        if '/stodownload' in parts.path:
            for extension in ('.gif', '.png', '.jpg', '.webp'):
                urls.append(urlunsplit(parts._replace(path=parts.path + extension)))
    return list(dict.fromkeys(urls))


def read_favorites(database):
    # A private temporary directory prevents decrypted metadata from mixing with source files.
    with tempfile.TemporaryDirectory(prefix='wechat-stickers-') as directory:
        path = Path(directory) / 'snapshot.db'
        path.write_bytes(database)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata = 'kNonStoreEmoticonTable'
            if metadata not in tables:
                raise RuntimeError('未找到收藏表情元数据表，此微信数据库结构暂不支持。')
            fields = {r[1] for r in connection.execute(f'PRAGMA table_info({metadata})')}
            preferred = [field for field in ('cdn_url', 'tp_url', 'extern_url') if field in fields]
            if 'md5' not in fields or not preferred:
                raise RuntimeError('收藏元数据字段不受支持。')
            # Do not substitute thumbnails or encrypted resources for the original.
            for order in ('kFavEmoticonOrderTable', 'kCustomEmoticonOrderTable'):
                if order not in tables:
                    continue
                if 'md5' not in {r[1] for r in connection.execute(f'PRAGMA table_info({order})')}:
                    continue
                columns = ', '.join('n.' + field for field in preferred)
                rows = connection.execute(
                    f'SELECT o.md5, {columns} FROM {order} o LEFT JOIN {metadata} n ON o.md5=n.md5 ORDER BY o.rowid'
                ).fetchall()
                if not rows:
                    continue
                records, seen = [], set()
                for row in rows:
                    identifier = row[0].hex() if isinstance(row[0], bytes) else str(row[0])
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    urls = list(dict.fromkeys(url for field in row[1:] for url in url_candidates(field)))
                    records.append({'id': identifier, 'urls': urls})
                return records
            if any(order in tables for order in ('kFavEmoticonOrderTable', 'kCustomEmoticonOrderTable')):
                return []
            raise RuntimeError('未找到个人收藏顺序表，停止导出，避免混入未收藏的图片。')


def export_favorites(output_dir, manifest_path, data_dir=None, wxid=None, key_file=None):
    account = select_account(data_dir, wxid)
    database, wal = snapshot_database(account['database'])
    key = None
    if not database.startswith(SQLITE_HEADER):
        if key_file:
            try:
                key = bytes.fromhex(Path(key_file).expanduser().read_text(encoding='utf-8-sig').strip())
            except (ValueError, OSError) as exc:
                raise RuntimeError('无法读取密钥文件，需要仅含 64 位十六进制字符的 UTF-8 文本。') from exc
            if len(key) != 32:
                raise RuntimeError('密钥文件必须包含 64 位十六进制字符。')
        elif sys.platform == 'win32':
            from windows_wechat import discover_processes, find_db_key
            for process in discover_processes():
                print(f'微信 PID {process["pid"]}，版本 {process["version"] or "未知"}')
            key = find_db_key(account['database'], lambda candidate: verify_db_key(database[:PAGE_SIZE], candidate))
        else:
            raise RuntimeError('当前独立自动取密钥仅支持 Windows；macOS 可使用 --key-file 读取本机已有密钥。')
    decoded = decode_database(database, wal, key)
    records = read_favorites(decoded)
    print(f'账号 {account["id"]}：读取到 {len(records)} 个收藏记录。')
    from sticker_download import download_stickers
    # Include the source root to distinguish equal account names on separate installations.
    source = str(account['database'].parent.parent.parent.parent)
    return download_stickers(records, output_dir, manifest_path, account=source + ':' + account['id'])
