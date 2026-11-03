"""Download image bytes with an atomic, resumable local manifest.

Network transport uses urllib; Pillow is imported only when this API is called.
The manifest is version 1, with a ``records`` mapping keyed by the SHA-256 of
JSON [account, id]. Unrelated manifest fields and records survive each update.
Calls sharing a manifest must be serialized by the caller.
"""
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
import hashlib
import http.client
import io
import json
import math
import errno
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings


_EXTENSIONS = {'GIF': '.gif', 'PNG': '.png', 'JPEG': '.jpg',
               'WEBP': '.webp', 'BMP': '.bmp'}


class PartialDownloadError(RuntimeError):
    """Finished a download pass with recoverable failures and usable images."""
    def __init__(self, successful_paths, failures):
        self.successful_paths = list(successful_paths)
        self.failures = list(failures)
        detail = '; '.join(f'{key[:12]}: {reason}' for key, reason in self.failures)
        super().__init__(f'{len(self.failures)} sticker record(s) failed; '
                         f'successful downloads were preserved: {detail}')


class _DownloadError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _check_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (parts.scheme.lower() in ('http', 'https') and parts.hostname
                 and parts.port != 0 and parts.username is None
                 and parts.password is None
                 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url))
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise _DownloadError('invalid HTTP(S) URL')


class _HTTPRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _image_format(data, Image):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format
                if fmt not in _EXTENSIONS:
                    raise _DownloadError('unsupported image format')
                image.verify()
            # verify() alone does not decode GIF/JPEG pixels or later frames.
            with Image.open(io.BytesIO(data)) as image:
                for frame in range(getattr(image, 'n_frames', 1)):
                    image.seek(frame)
                    image.load()
        return fmt
    except _DownloadError:
        raise
    except Exception:
        # Decoder exceptions can contain metadata from an untrusted image.
        raise _DownloadError('invalid image data') from None


def _remaining(deadline, cancel=None):
    if cancel is not None and cancel.is_set():
        raise CancelledError
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DownloadError('item network time budget exceeded')
    return remaining


def _network_error(error, stage):
    # URLError.reason normally holds the original socket/SSL exception.
    # Never stringify it: it may include a signed URL or proxy credentials.
    for _ in range(4):
        if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
            error = error.reason
        else:
            break
    winerror = getattr(error, 'winerror', None)
    code = winerror if isinstance(winerror, int) else getattr(error, 'errno', None)
    suffix = ''
    if isinstance(code, int):
        suffix = f'（{"WinError" if isinstance(winerror, int) else "errno"}={code}）'
    if isinstance(error, ssl.SSLCertVerificationError):
        category, message, retryable = 'tls_certificate', 'TLS 证书校验失败', False
        verify_code = getattr(error, 'verify_code', None)
        if isinstance(verify_code, int):
            suffix += f'（verify_code={verify_code}）'
            if verify_code == 62:
                message = 'TLS 证书与请求域名不匹配'
    elif isinstance(error, ssl.SSLError):
        category, message, retryable = 'tls', 'TLS 协商或加密连接失败', False
    elif isinstance(error, socket.gaierror):
        category, message = 'dns', '域名解析失败（图片服务器或代理地址）'
        retryable = code in (socket.EAI_AGAIN, 11002)
    elif isinstance(error, TimeoutError) or code in (errno.ETIMEDOUT, 10060):
        category, message, retryable = 'timeout', '连接超时' if stage == 'connect' else '接收图片超时', True
    elif isinstance(error, ConnectionRefusedError) or code in (errno.ECONNREFUSED, 10061):
        category, message, retryable = 'connection_refused', '连接被拒绝（检查网络及代理是否可用）', False
    elif isinstance(error, ConnectionResetError) or code in (errno.ECONNRESET, 10054):
        category, message, retryable = 'connection_reset', '连接被重置', True
    elif code in (errno.EACCES, 10013):
        category, message, retryable = 'network_permission', '网络连接被系统拒绝', False
    elif isinstance(error, (EOFError, http.client.HTTPException)):
        category, message, retryable = 'response', '图片响应中断或格式异常', True
    else:
        category, message, retryable = 'network', '网络连接失败，异常类型 ' + type(error).__name__, True
    return _DownloadError(f'[{category}/{stage}] {message}{suffix}', retryable)


def _fetch(opener, url, timeout, max_bytes, deadline=None, cancel=None):
    _check_url(url)
    if deadline is None:
        deadline = time.monotonic() + timeout
    stage = 'connect'
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'StickerMigrator/1'})
        with opener.open(request, timeout=min(timeout, _remaining(deadline, cancel))) as response:
            stage = 'read'
            _remaining(deadline, cancel)
            _check_url(response.geturl())
            length = response.headers.get('Content-Length')
            try:
                expected = int(length) if length is not None else None
            except (ValueError, TypeError):
                expected = None
            if expected is not None and expected > max_bytes:
                raise _DownloadError('image exceeds size limit')
            chunks = []
            size = 0
            # HTTPResponse.read() may wait to fill the whole block despite a trickle.
            # read1() returns after at most one underlying read, so deadlines are checked.
            read = getattr(response, 'read1', response.read)
            while True:
                _remaining(deadline, cancel)
                chunk = read(min(65536, max_bytes - size + 1))
                _remaining(deadline, cancel)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise _DownloadError('image exceeds size limit')
                chunks.append(chunk)
            if expected is not None and expected >= 0 and expected != size:
                raise _DownloadError('incomplete HTTP response', retryable=True)
            return b''.join(chunks)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise _DownloadError(f'HTTP {code}', code in (408, 429) or 500 <= code < 600) from None
    except (urllib.error.URLError, OSError, EOFError, http.client.HTTPException) as exc:
        raise _network_error(exc, stage) from None
    except (ValueError, UnicodeError):
        raise _DownloadError('invalid HTTP(S) URL') from None


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.sticker-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_manifest(path):
    if not path.exists():
        return {'version': 1, 'records': {}}
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(manifest, dict) or manifest.get('version') != 1
                or not isinstance(manifest.get('records'), dict)
                or not all(isinstance(entry, dict) for entry in manifest['records'].values())):
            raise ValueError
        return manifest
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError('Cannot read download manifest; existing file was preserved') from None


def _save_entry(manifest, key, entry, manifest_path):
    updated = dict(manifest, records=dict(manifest['records'], **{key: entry}))
    try:
        data = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        _atomic_write(manifest_path, data)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise RuntimeError('Cannot save download manifest; previous checkpoint was preserved') from None
    return updated


def _cached_file(entry, output_dir, max_bytes, Image):
    if entry.get('status') != 'success':
        return None
    name = entry.get('path')
    if (not isinstance(name, str) or not name or Path(name).name != name
            or '/' in name or '\\' in name or name in ('.', '..')):
        return None
    try:
        path = output_dir / name
        if not path.resolve().is_relative_to(output_dir):
            return None
        # Bound reads even if the file changed after stat().
        with path.open('rb') as stream:
            data = stream.read(max_bytes + 1)
        if (len(data) > max_bytes or len(data) != entry.get('size')
                or hashlib.sha256(data).hexdigest() != entry.get('sha256')):
            return None
        if _image_format(data, Image) != entry.get('format'):
            return None
        return path
    except (OSError, ValueError, _DownloadError):
        return None


def _safe_origin(url):
    try:
        _check_url(url)
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname.encode('idna').decode('ascii')
        if len(host) > 253:
            return '无效域名'
        if ':' in host:
            host = '[' + host + ']'
        port = f':{parts.port}' if parts.port is not None else ''
        return f'{parts.scheme}://{host}{port}'
    except (ValueError, UnicodeError, _DownloadError):
        return '无效地址'


def _download_one(job, total, timeout, max_bytes, retries, item_timeout, Image, report, cancel):
    index, record, _, _ = job
    label = f'[{index}/{total}]'
    report(f'{label} 开始下载')
    deadline = time.monotonic() + item_timeout
    opener = urllib.request.build_opener(_HTTPRedirects())
    errors = []
    try:
        for candidate, url in enumerate(record['urls']):
            for attempt in range(retries + 1):
                _remaining(deadline, cancel)
                report(f'{label} 请求资源 {candidate + 1}/{len(record["urls"])}，'
                       f'第 {attempt + 1} 次尝试，目标 {_safe_origin(url)}')
                try:
                    data = _fetch(opener, url, timeout, max_bytes, deadline, cancel)
                    fmt = _image_format(data, Image)
                    return (data, fmt, hashlib.sha256(data).hexdigest(), url), errors
                except _DownloadError as exc:
                    errors.append({'candidate': candidate, 'attempt': attempt + 1, 'error': str(exc)})
                    report(f'{label} 本次请求失败：{exc}')
                    _remaining(deadline, cancel)
                    if not exc.retryable or attempt == retries:
                        break
                    time.sleep(min(0.25 * (2 ** attempt), 2, _remaining(deadline, cancel)))
        return None, errors
    except _DownloadError as exc:
        errors.append({'error': str(exc)})
        return None, errors


def _completed_downloads(jobs, total, workers, timeout, max_bytes, retries,
                         item_timeout, Image, report):
    cancel = threading.Event()
    arguments = (total, timeout, max_bytes, retries, item_timeout, Image, report, cancel)
    if workers == 1:
        for job in jobs:
            yield job, _download_one(job, *arguments)
        return
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='sticker-download')
    pending = {}
    remaining_jobs = iter(jobs)

    def submit_next():
        job = next(remaining_jobs, None)
        if job is not None:
            pending[executor.submit(_download_one, job, *arguments)] = job

    try:
        for _ in range(min(workers, len(jobs))):
            submit_next()
        last_report = time.monotonic()
        while pending:
            done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            if not done and time.monotonic() - last_report >= 5:
                indices = ', '.join(str(job[0]) for job in pending.values())
                report(f'仍在处理第 {indices} 张（等待网络或校验），可按 Ctrl+C 停止')
                last_report = time.monotonic()
            for future in done:
                job = pending.pop(future)
                yield job, future.result()
                submit_next()
    finally:
        cancel.set()
        for future in pending:
            future.cancel()
        # Running urllib operations stop at their socket timeout; no background writes.
        executor.shutdown(wait=True, cancel_futures=True)


def download_stickers(records, output_dir, manifest_path, account='', *,
                      max_bytes=25 * 1024 * 1024, timeout=10, retries=2,
                      workers=4, item_timeout=60, progress=True) -> list[Path]:
    """Return one Path per input record, in order (duplicates share a Path).

    ``records`` is a list of {"id": str, "urls": list[str]}. Candidate URLs are
    tried in order; transient transport failures get ``retries`` extra attempts
    per URL (0..10). Each urllib operation uses ``timeout`` seconds. The default
    per-image limit is 25 MiB, also enforced when Content-Length is absent.
    At most ``workers`` downloads run concurrently (default 4, maximum 8).
    ``item_timeout`` is a shared network budget across a record's candidates and
    retries, checked before/after blocking operations. A blocking socket call can
    exceed the remaining budget by its timeout; DNS, TLS/redirect handling and
    image decoding are not forcibly interrupted. Progress is flushed to stdout.

    Each successful or failed record is checkpointed immediately. Cache hits are
    rehashed and decoded; equal SHA-256 payloads reuse one file across records
    and accounts. Returned files contain the original bytes, including animation.
    The local manifest may contain source URLs; exception text never includes
    URLs or raw network/decoder error messages. A batch with any failed records
    raises PartialDownloadError after processing the rest; its successful_paths
    preserve input order so the caller can group usable images. Checkpoints survive.
    Invalid arguments raise ValueError before any downloads or manifest changes.
    An unreadable manifest is never replaced. Checkpoint-write failures stop the
    batch and remove its newly written, uncheckpointed image.
    """
    if (not isinstance(records, list) or not isinstance(account, str)
            or any(not isinstance(record, dict) or not isinstance(record.get('id'), str)
                   or not isinstance(record.get('urls'), list)
                   or not all(isinstance(url, str) for url in record['urls'])
                   for record in records)):
        raise ValueError('records must contain string ids and URL lists; account must be a string')
    if (type(max_bytes) is not int or max_bytes <= 0
            or type(retries) is not int or not 0 <= retries <= 10
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('max_bytes and timeout must be positive; retries must be an integer from 0 to 10')
    if (type(workers) is not int or not 1 <= workers <= 8
            or isinstance(item_timeout, bool) or not isinstance(item_timeout, (int, float))
            or not math.isfinite(item_timeout) or item_timeout <= 0
            or type(progress) is not bool):
        raise ValueError('workers must be 1..8; item_timeout must be positive; progress must be boolean')
    output_dir = Path(output_dir).resolve()
    manifest_path = Path(manifest_path).resolve()
    manifest = _load_manifest(manifest_path)
    console_lock = threading.Lock()

    def report(message):
        if progress:
            with console_lock:
                print(message, flush=True)

    report(f'共 {len(records)} 个收藏，最多 {workers} 路并发；正在检查已有下载记录……')
    if progress:
        proxies = urllib.request.getproxies()
        proxy_configured = any(proxies.get(scheme) for scheme in ('http', 'https', 'all'))
        report('网络配置：检测到系统或环境代理配置' if proxy_configured else '网络配置：未检测到代理配置')
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError('Pillow is required to validate downloaded stickers') from None

    # Verify each distinct path only once per call; never trust manifest hashes.
    checked = {}
    by_hash = {}
    for cache_index, entry in enumerate(manifest['records'].values(), 1):
        if cache_index % 10 == 0:
            report(f'缓存检查 {cache_index}/{len(manifest["records"])}')
        if entry.get('status') != 'success':
            continue
        signature = (entry.get('path'), entry.get('sha256'), entry.get('size'), entry.get('format'))
        # Malformed metadata is treated as a cache miss, not executable input.
        if not all(isinstance(value, (str, int, type(None))) for value in signature):
            continue
        if signature not in checked:
            checked[signature] = _cached_file(entry, output_dir, max_bytes, Image)
        path = checked[signature] if entry.get('status') == 'success' else None
        if path is not None:
            by_hash[entry['sha256']] = path

    paths_by_key, identities, jobs, scheduled = {}, [], [], set()
    failures = []
    for index, record in enumerate(records, 1):
        sticker_id = record['id']
        identity = json.dumps([account, sticker_id], ensure_ascii=False, separators=(',', ':'))
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        identities.append(key)
        if key in scheduled:
            continue
        scheduled.add(key)
        previous = manifest['records'].get(key, {})
        signature = (previous.get('path'), previous.get('sha256'), previous.get('size'), previous.get('format'))
        try:
            cached = checked.get(signature) if previous.get('status') == 'success' else None
        except TypeError:
            cached = None
        if cached is not None and previous.get('account') == account and previous.get('id') == sticker_id:
            paths_by_key[key] = cached
            report(f'[{index}/{len(records)}] 已缓存，复用本地图片')
        else:
            jobs.append((index, record, key, previous))

    def checkpoint(job, outcome):
        nonlocal manifest
        index, record, key, previous = job
        result, errors = outcome
        sticker_id = record['id']
        entry = dict(previous, account=account, id=sticker_id)
        created = None
        if result is not None:
            data, fmt, digest, url = result
            path = by_hash.get(digest)
            if path is None:
                path = output_dir / f'{key}-{digest}{_EXTENSIONS[fmt]}'
                # Do not overwrite an existing file (including a corrupt cache)
                # before its replacement is safely checkpointed.
                if path.exists() or path.is_symlink() or path == manifest_path:
                    path = output_dir / f'{key}-{digest}-{uuid.uuid4().hex}{_EXTENSIONS[fmt]}'
                try:
                    _atomic_write(path, data)
                    created = path
                except OSError:
                    errors.append({'error': 'cannot write image file'})
                    result = None
            if result is not None:
                entry.update(status='success', path=path.name, sha256=digest,
                             size=len(data), format=fmt, url=url, errors=errors)
        if result is None:
            entry.update(status='failed', errors=errors or [{'error': 'no candidate URLs'}])
            # Do not leave stale success metadata on a failed cache repair.
            for field in ('path', 'sha256', 'size', 'format', 'url'):
                entry.pop(field, None)
        try:
            manifest = _save_entry(manifest, key, entry, manifest_path)
        except RuntimeError:
            if created is not None:
                created.unlink(missing_ok=True)
            raise
        if result is None:
            failures.append((key, entry['errors'][-1]['error']))
            report(f'[{index}/{len(records)}] 失败，记录已保存：{entry["errors"][-1]["error"]}')
        else:
            by_hash[digest] = path
            checked[(path.name, digest, len(data), fmt)] = path
            paths_by_key[key] = path
            report(f'[{index}/{len(records)}] 完成，{len(data) / 1024:.1f} KiB，进度已保存')

    with closing(_completed_downloads(jobs, len(records), workers, timeout, max_bytes,
                                      retries, item_timeout, Image, report)) as completed:
        for job, outcome in completed:
            checkpoint(job, outcome)
    if failures:
        report(f'本轮结束：{len(failures)} 条失败，成功记录已保留，可重新运行重试失败项。')
        successful_paths = [paths_by_key[key] for key in identities if key in paths_by_key]
        raise PartialDownloadError(successful_paths, failures)
    report(f'下载完成：{len(records)} 条记录，{len(set(paths_by_key.values()))} 个图片文件。')
    return [paths_by_key[key] for key in identities]
