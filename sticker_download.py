"""Download image bytes with an atomic, resumable local manifest.

Network transport uses urllib; Pillow is imported only when this API is called.
The manifest is version 1, with a ``records`` mapping keyed by the SHA-256 of
JSON [account, id]. Unrelated manifest fields and records survive each update.
Calls sharing a manifest must be serialized by the caller.
"""
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings


_EXTENSIONS = {'GIF': '.gif', 'PNG': '.png', 'JPEG': '.jpg',
               'WEBP': '.webp', 'BMP': '.bmp'}


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


def _fetch(opener, url, timeout, max_bytes):
    _check_url(url)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'StickerMigrator/1'})
        with opener.open(request, timeout=timeout) as response:
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
            while True:
                chunk = response.read(min(65536, max_bytes - size + 1))
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
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise _DownloadError('network error or timeout', retryable=True) from None
    except (OSError, EOFError, http.client.HTTPException):
        raise _DownloadError('response read failed', retryable=True) from None
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


def download_stickers(records, output_dir, manifest_path, account='', *,
                      max_bytes=25 * 1024 * 1024, timeout=20, retries=2) -> list[Path]:
    """Return one Path per input record, in order (duplicates share a Path).

    ``records`` is a list of {"id": str, "urls": list[str]}. Candidate URLs are
    tried in order; transient transport failures get ``retries`` extra attempts
    per URL (0..10). Each urllib operation uses ``timeout`` seconds. The default
    per-image limit is 25 MiB, also enforced when Content-Length is absent.

    Each successful or failed record is checkpointed immediately. Cache hits are
    rehashed and decoded; equal SHA-256 payloads reuse one file across records
    and accounts. Returned files contain the original bytes, including animation.
    The local manifest may contain source URLs; exception text never includes
    URLs or raw network/decoder error messages. A batch with any failed records
    raises RuntimeError after processing the rest. Successful checkpoints survive.
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
    output_dir = Path(output_dir).resolve()
    manifest_path = Path(manifest_path).resolve()
    manifest = _load_manifest(manifest_path)
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError('Pillow is required to validate downloaded stickers') from None

    opener = urllib.request.build_opener(_HTTPRedirects())
    # Verify each distinct path only once per call; never trust manifest hashes.
    checked = {}
    by_hash = {}
    for entry in manifest['records'].values():
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

    paths = []
    failures = []
    for record in records:
        sticker_id = record['id']
        identity = json.dumps([account, sticker_id], ensure_ascii=False, separators=(',', ':'))
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        previous = manifest['records'].get(key, {})
        signature = (previous.get('path'), previous.get('sha256'), previous.get('size'), previous.get('format'))
        try:
            cached = checked.get(signature) if previous.get('status') == 'success' else None
        except TypeError:
            cached = None
        if cached is not None and previous.get('account') == account and previous.get('id') == sticker_id:
            paths.append(cached)
            continue

        errors = []
        result = None
        for candidate, url in enumerate(record['urls']):
            for attempt in range(retries + 1):
                try:
                    data = _fetch(opener, url, timeout, max_bytes)
                    fmt = _image_format(data, Image)
                    digest = hashlib.sha256(data).hexdigest()
                    result = (data, fmt, digest, url)
                    break
                except _DownloadError as exc:
                    errors.append({'candidate': candidate, 'attempt': attempt + 1, 'error': str(exc)})
                    if not exc.retryable or attempt == retries:
                        break
                    time.sleep(min(0.25 * (2 ** attempt), 2))
            if result is not None:
                break

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
        else:
            by_hash[digest] = path
            checked[(path.name, digest, len(data), fmt)] = path
            paths.append(path)
    if failures:
        detail = '; '.join(f'{key[:12]}: {reason}' for key, reason in failures)
        raise RuntimeError(f'{len(failures)} sticker record(s) failed; successful downloads were preserved: {detail}')
    return paths
