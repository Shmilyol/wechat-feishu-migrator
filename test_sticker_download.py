"""Offline tests: synthetic Pillow images and mocked urllib HTTP responses only."""
import contextlib
import hashlib
import http.client
import io
import json
import socket
import ssl
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from PIL import Image

import sticker_download as downloader


class Response(io.BytesIO):
    def __init__(self, data, url='https://cdn.example/sticker', headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def read1(self, size=-1):
        return self.read(size)


def image_bytes(fmt='PNG', animated=False, color='red'):
    output = io.BytesIO()
    image = Image.new('RGB', (8, 8), color)
    options = {}
    if animated:
        options = {'save_all': True, 'append_images': [Image.new('RGB', (8, 8), 'blue')],
                   'duration': 100, 'loop': 0}
    image.save(output, format=fmt, **options)
    return output.getvalue()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / '图片 缓存'
        self.manifest = self.root / '清单.json'
        opener_patch = patch.object(downloader.urllib.request, 'build_opener')
        self.opener = opener_patch.start().return_value
        self.addCleanup(opener_patch.stop)
        sleep_patch = patch.object(downloader.time, 'sleep')
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        # Catch an accidental regression to an actual HTTP connection.
        network_patch = patch('socket.socket.connect', side_effect=AssertionError('real network forbidden'))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def download(self, records, **kwargs):
        options = {'workers': 1, 'progress': False, **kwargs}
        return downloader.download_stickers(records, self.output, self.manifest, **options)

    def entries(self):
        return list(json.loads(self.manifest.read_text(encoding='utf-8'))['records'].values())

    def record(self, identity='one', urls=None):
        return {'id': identity, 'urls': ['https://cdn.example/image'] if urls is None else urls}

    def test_progress_is_visible_before_waiting_for_network(self):
        output = io.StringIO()
        data = image_bytes()

        def open_response(*args, **kwargs):
            self.assertIn('[1/1]', output.getvalue())
            return Response(data)

        self.opener.open.side_effect = open_response
        with contextlib.redirect_stdout(output):
            self.download([self.record()], progress=True)
        self.assertIn('完成', output.getvalue())

    def test_slow_continuous_response_cannot_exceed_item_budget(self):
        clock = [0.0]
        data = image_bytes()

        class SlowResponse(Response):
            def read(self, size=-1):
                # Bytes keep arriving before each socket timeout, for >60 s total.
                clock[0] += 2
                return super().read(min(size, 1))

        self.opener.open.return_value = SlowResponse(data)
        with patch.object(downloader.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, 'time budget'):
                self.download([self.record()], timeout=3, retries=0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.entries()[0]['status'], 'failed')
        self.assertLessEqual(clock[0], 65)

    def test_parallel_downloads_are_bounded_ordered_and_checkpointed_on_main_thread(self):
        first_started, second_started = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, maximum = [0], [0]
        main_thread = threading.get_ident()
        payloads = {f'https://cdn.example/{i}': image_bytes(color=color)
                    for i, color in enumerate(('red', 'green', 'blue'))}

        class TrackedResponse(Response):
            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    with lock:
                        active[0] -= 1

        def open_response(request, **kwargs):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            url = request.full_url
            if url.endswith('/0'):
                first_started.set()
                self.assertTrue(second_started.wait(2), 'second download never ran concurrently')
            elif url.endswith('/1'):
                self.assertTrue(first_started.wait(2))
                second_started.set()
            return TrackedResponse(payloads[url])

        write = downloader._atomic_write
        def checkpoint(path, data):
            self.assertEqual(threading.get_ident(), main_thread)
            return write(path, data)

        records = [self.record(str(i), [url]) for i, url in enumerate(payloads)]
        self.opener.open.side_effect = open_response
        with patch.object(downloader, '_atomic_write', side_effect=checkpoint):
            paths = self.download(records, workers=2)
        self.assertEqual(maximum[0], 2)
        self.assertEqual(active[0], 0)
        self.assertEqual([p.read_bytes() for p in paths], list(payloads.values()))
        self.assertEqual(len(self.entries()), 3)
        self.assertTrue(all(e['status'] == 'success' for e in self.entries()))

    def test_parallel_duplicate_ids_share_one_network_job(self):
        self.opener.open.return_value = Response(image_bytes())
        paths = self.download([self.record(), self.record()], workers=4)
        self.assertEqual(paths[0], paths[1])
        self.opener.open.assert_called_once()

    def test_budget_is_shared_across_candidate_urls(self):
        clock = [0.0]
        def unavailable(*args, **kwargs):
            clock[0] += 10
            raise TimeoutError('private URL must not appear')
        self.opener.open.side_effect = unavailable
        urls = [f'https://cdn.example/{i}' for i in range(10)]
        with patch.object(downloader.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, 'time budget'):
                self.download([self.record(urls=urls)], item_timeout=20, retries=0)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_interruption_keeps_completed_checkpoints_for_resume(self):
        self.opener.open.side_effect = [Response(image_bytes()), KeyboardInterrupt()]
        records = [self.record('done'), self.record('interrupted')]
        with self.assertRaises(KeyboardInterrupt):
            self.download(records)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.entries()[0]['status'], 'success')
        self.opener.open.reset_mock(side_effect=True)
        self.opener.open.return_value = Response(image_bytes(color='green'))
        paths = self.download(records)
        self.opener.open.assert_called_once()
        self.assertEqual(len(paths), 2)

    def test_network_error_categories_preserve_codes_but_hide_sensitive_text(self):
        secret = 'https://private.example/image?token=do-not-print'
        refused = ConnectionRefusedError(10061, secret)
        refused.winerror = 10061
        cases = [
            (urllib.error.URLError(socket.gaierror(11001, secret)), '[dns/connect]', '11001'),
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, secret)), '[tls_certificate/connect]', None),
            (urllib.error.URLError(ssl.SSLError(1, secret)), '[tls/connect]', None),
            (urllib.error.URLError(refused), '[connection_refused/connect]', 'WinError=10061'),
            (TimeoutError(secret), '[timeout/connect]', None),
            (ConnectionResetError(10054, secret), '[connection_reset/connect]', '10054'),
        ]
        for error, category, code in cases:
            with self.subTest(category=category):
                self.opener.open.side_effect = error
                with self.assertRaises(downloader._DownloadError) as caught:
                    downloader._fetch(self.opener, 'https://cdn.example/image', 3, 1024)
                message = str(caught.exception)
                self.assertIn(category, message)
                if code:
                    self.assertIn(code, message)
                self.assertNotIn('do-not-print', message)
                self.assertNotIn('private.example', message)

    def test_proxy_progress_never_exposes_proxy_address_or_credentials(self):
        output = io.StringIO()
        self.opener.open.return_value = Response(image_bytes())
        with patch.object(downloader.urllib.request, 'getproxies',
                          return_value={'https': 'http://user:private-password@proxy.private:8080'}), \
                contextlib.redirect_stdout(output):
            self.download([self.record()], progress=True)
        self.assertIn('检测到系统或环境代理配置', output.getvalue())
        self.assertNotIn('private-password', output.getvalue())
        self.assertNotIn('proxy.private', output.getvalue())

    def test_certificate_code_62_is_reported_as_hostname_mismatch(self):
        error = ssl.SSLCertVerificationError(1, 'signed-url?token=private')
        error.verify_code = 62
        self.opener.open.side_effect = urllib.error.URLError(error)
        with self.assertRaises(downloader._DownloadError) as caught:
            downloader._fetch(self.opener, 'https://cdn.example/image', 3, 1024)
        self.assertIn('域名不匹配', str(caught.exception))
        self.assertIn('verify_code=62', str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn('private', str(caught.exception))

    def test_cdn_alias_recovers_hostname_mismatch_without_disabling_tls(self):
        from wechat_source import url_candidates
        original = 'https://vweixinf.tc.qq.com/stodownload?m=abc&filekey=keep%2Fsignature'
        alias = 'https://wxapp.tc.qq.com/stodownload?m=abc&filekey=keep%2Fsignature'
        error = ssl.SSLCertVerificationError(1, 'hostname mismatch')
        error.verify_code = 62
        self.opener.open.side_effect = [urllib.error.URLError(error), Response(image_bytes(), url=alias)]
        paths = self.download([self.record(urls=url_candidates(original))])
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list],
                         [original, alias])
        self.assertTrue(paths[0].exists())
        self.sleep.assert_not_called()

    def test_progress_shows_origin_without_path_or_query_tokens(self):
        output = io.StringIO()
        self.opener.open.return_value = Response(image_bytes())
        url = 'https://cdn.example/private-path?filekey=private-token'
        with contextlib.redirect_stdout(output):
            self.download([self.record(urls=[url])], progress=True)
        self.assertIn('目标 https://cdn.example', output.getvalue())
        self.assertNotIn('private-path', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())
        self.assertEqual(downloader._safe_origin('https://user:secret@host.example/image'), '无效地址')

    def test_openssl_rejects_hostname_mismatch_with_verification_enabled(self):
        # A real in-memory TLS handshake: no sockets and no disabled verification.
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'valid.example.test')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('valid.example.test')]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
        pem = certificate.public_bytes(serialization.Encoding.PEM)
        certificate_path, key_path = self.root / 'tls.pem', self.root / 'tls.key'
        certificate_path.write_bytes(pem)
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        client_context = ssl.create_default_context(cadata=pem.decode('ascii'))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certificate_path, key_path)
        client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
        client = client_context.wrap_bio(client_in, client_out, server_hostname='wrong.example.test')
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        caught = None
        for _ in range(10):
            try:
                client.do_handshake()
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLCertVerificationError as error:
                caught = error
                break
            data = client_out.read()
            if data:
                server_in.write(data)
            try:
                server.do_handshake()
            except ssl.SSLWantReadError:
                pass
            data = server_out.read()
            if data:
                client_in.write(data)
        self.assertIsNotNone(caught, 'TLS accepted a certificate for a different hostname')
        self.assertEqual(caught.verify_code, 62)
        self.assertTrue(client_context.check_hostname)
        self.assertEqual(client_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn('域名不匹配', str(downloader._network_error(caught, 'connect')))

    def test_response_timeout_is_distinguished_from_connection_timeout(self):
        response = Response(image_bytes())
        response.read = lambda size: (_ for _ in ()).throw(TimeoutError('private-token'))
        self.opener.open.return_value = response
        with self.assertRaises(downloader._DownloadError) as caught:
            downloader._fetch(self.opener, 'https://cdn.example/image', 3, 1024)
        self.assertIn('[timeout/read]', str(caught.exception))
        self.assertNotIn('private-token', str(caught.exception))
        self.assertTrue(response.closed)

    def test_real_formats_animations_and_misleading_content_type(self):
        for fmt, animated, suffix in [('PNG', False, '.png'), ('GIF', True, '.gif'),
                                      ('PNG', True, '.png'), ('JPEG', False, '.jpg'),
                                      ('WEBP', True, '.webp'), ('BMP', False, '.bmp')]:
            with self.subTest(fmt=fmt, animated=animated):
                data = image_bytes(fmt, animated)
                self.opener.open.return_value = Response(data, headers={'Content-Type': 'text/html'})
                paths = self.download([self.record(f'{fmt}-{animated}')])
                self.assertEqual(paths[0].suffix, suffix)
                self.assertEqual(paths[0].read_bytes(), data)
                with Image.open(paths[0]) as image:
                    self.assertEqual(image.format, fmt)
                    if animated:
                        self.assertEqual(image.n_frames, 2)
        self.assertEqual(len(self.entries()), 6)

    def test_order_hash_dedup_resume_and_account_isolation(self):
        red, green = image_bytes(), image_bytes(color='green')
        self.opener.open.side_effect = [Response(red), Response(green), Response(red)]
        records = [self.record('first'), self.record('second'), self.record('third')]
        paths = self.download(records, account='wxid_A')
        self.assertEqual([p.read_bytes() for p in paths], [red, green, red])
        self.assertEqual(paths[0], paths[2])
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.opener.open.reset_mock(side_effect=True)
        self.opener.open.side_effect = AssertionError('cache should avoid network')
        self.assertEqual(self.download(records[::-1], account='wxid_A'), paths[::-1])
        self.opener.open.assert_not_called()
        self.opener.open.side_effect = [Response(red)]
        self.assertEqual(self.download([records[0]], account='wxid_B'), [paths[0]])
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(len(self.entries()), 4)
        self.assertEqual({e['account'] for e in self.entries()}, {'wxid_A', 'wxid_B'})

    def test_duplicate_record_returns_one_path_per_input_without_redownload(self):
        self.opener.open.side_effect = [Response(image_bytes())]
        records = [self.record(), self.record()]
        paths = self.download(records)
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(len(self.entries()), 1)

    def test_corrupt_missing_and_semantically_invalid_cache_is_refetched(self):
        data = image_bytes()
        self.opener.open.side_effect = [Response(data) for _ in range(4)]
        original = self.download([self.record()])[0]
        original.write_bytes(b'bad cache')
        repaired = self.download([self.record()])[0]
        self.assertNotEqual(repaired, original)
        self.assertEqual(repaired.read_bytes(), data)
        repaired.unlink()
        recovered = self.download([self.record()])[0]
        manifest = json.loads(self.manifest.read_text(encoding='utf-8'))
        entry = next(iter(manifest['records'].values()))
        invalid = b'not an image'
        recovered.write_bytes(invalid)
        entry.update(sha256=hashlib.sha256(invalid).hexdigest(), size=len(invalid))
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(self.download([self.record()])[0].read_bytes(), data)
        self.assertEqual(self.opener.open.call_count, 4)

    def test_candidates_and_transient_retries_are_bounded_and_ordered(self):
        first, second = 'https://cdn.example/first', 'https://cdn.example/second'
        self.opener.open.side_effect = [urllib.error.URLError('secret=hidden'),
                                       TimeoutError('secret=hidden'), Response(b'<html>error</html>'),
                                       Response(image_bytes())]
        paths = self.download([self.record(urls=[first, second])], retries=2, timeout=7)
        calls = self.opener.open.call_args_list
        self.assertEqual([call.args[0].full_url for call in calls], [first, first, first, second])
        self.assertTrue(all(call.kwargs['timeout'] == 7 for call in calls))
        self.assertEqual(self.sleep.call_count, 2)
        self.assertTrue(paths[0].exists())
        self.assertNotIn('hidden', self.manifest.read_text(encoding='utf-8'))

    def test_http_404_skips_retry_but_503_retries(self):
        url = 'https://cdn.example/image'
        missing = urllib.error.HTTPError(url, 404, 'secret', {}, io.BytesIO())
        busy = urllib.error.HTTPError(url, 503, 'secret', {}, io.BytesIO())
        self.opener.open.side_effect = [missing, busy, Response(image_bytes())]
        self.download([self.record(urls=[url, url + '2'])])
        self.assertEqual(self.opener.open.call_count, 3)
        self.sleep.assert_called_once()
        self.assertTrue(missing.fp.closed)
        self.assertTrue(busy.fp.closed)

    def test_failure_checkpoint_and_summary_preserve_success_and_retry_next_run(self):
        secret = 'https://cdn.example/x?token=private-secret'
        self.opener.open.side_effect = [Response(image_bytes()),
                                       urllib.error.URLError(secret),
                                       Response(image_bytes(color='green'))]
        records = [self.record('good'), self.record('bad', [secret]), self.record('later')]
        with self.assertRaisesRegex(RuntimeError, '1 sticker record') as caught:
            self.download(records, retries=0)
        self.assertNotIn('private-secret', str(caught.exception))
        self.assertEqual([e['status'] for e in self.entries()], ['success', 'failed', 'success'])
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.opener.open.reset_mock(side_effect=True)
        self.opener.open.side_effect = [Response(image_bytes('GIF'))]
        paths = self.download(records)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual([p.suffix for p in paths], ['.png', '.gif', '.png'])
        self.assertTrue(all(e['status'] == 'success' for e in self.entries()))

    def test_many_failures_are_all_recorded_without_partial_files(self):
        self.opener.open.side_effect = [TimeoutError('private')] * 6
        with self.assertRaisesRegex(RuntimeError, '2 sticker record'):
            self.download([self.record('a'), self.record('b')], retries=2)
        self.assertEqual(self.opener.open.call_count, 6)
        self.assertEqual(len(self.entries()), 2)
        self.assertTrue(all(e['status'] == 'failed' for e in self.entries()))
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_limit_enforced_with_lying_and_missing_content_length(self):
        for headers in ({'Content-Length': '1000'}, {'Content-Length': '1'}, {}):
            with self.subTest(headers=headers):
                response = Response(b'x' * 1000, headers=headers)
                self.opener.open.return_value = response
                with self.assertRaisesRegex(RuntimeError, 'size limit'):
                    self.download([self.record(str(headers))], max_bytes=16)
                self.assertTrue(all(0 < size <= 17 for size in response.read_sizes))
                self.assertTrue(response.closed)
        self.assertFalse(self.output.exists())

    def test_exact_limit_succeeds_and_cache_obeys_lower_limit(self):
        data = image_bytes()
        self.opener.open.side_effect = [Response(data), Response(data)]
        self.download([self.record()], max_bytes=len(data))
        with self.assertRaisesRegex(RuntimeError, 'size limit'):
            self.download([self.record()], max_bytes=len(data) - 1)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_partial_response_and_read_failure_retry_without_contaminating_success(self):
        data = image_bytes()
        broken = Response(data)
        broken.read = lambda size: (_ for _ in ()).throw(http.client.IncompleteRead(b'partial', 20))
        self.opener.open.side_effect = [broken, Response(data, headers={'Content-Length': str(len(data) + 1)}),
                                       Response(data)]
        path = self.download([self.record()])[0]
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_invalid_truncated_and_unsupported_images_rejected(self):
        samples = [b'<html>oops</html>', b'GIF89a', image_bytes('PNG')[:45],
                   image_bytes('JPEG')[:100], image_bytes('TIFF')]
        self.opener.open.side_effect = [Response(data, headers={'Content-Type': 'image/gif'}) for data in samples]
        with self.assertRaisesRegex(RuntimeError, '5 sticker record'):
            self.download([self.record(str(i)) for i in range(len(samples))])
        self.assertEqual(self.opener.open.call_count, len(samples))
        self.assertFalse(self.output.exists())

    def test_rejects_damaged_later_animation_frames(self):
        data = image_bytes('GIF', animated=True)
        # Keep the first frame readable; truncate the second image's pixel data.
        frame_start = data.rfind(b'\x2c')
        truncated = data[:frame_start + 12]
        with Image.open(io.BytesIO(truncated)) as image:
            image.load()
        self.opener.open.return_value = Response(truncated)
        with self.assertRaisesRegex(RuntimeError, 'invalid image'):
            self.download([self.record()])

    def test_id_path_traversal_is_hashed_and_signed_url_allowed(self):
        identity = '../../escape/秘密'
        url = 'https://cdn.example/x?token=local-only&key=local-only'
        data = image_bytes()
        self.opener.open.return_value = Response(data)
        path = self.download([self.record(identity, [url])], account='../账号')[0]
        self.assertEqual(path.parent, self.output.resolve())
        self.assertNotIn('escape', path.name)
        self.assertNotIn('秘密', path.name)
        self.assertIn('local-only', self.manifest.read_text(encoding='utf-8'))

    def test_invalid_urls_are_never_opened_and_safe_candidate_can_follow(self):
        invalid = ['file:///tmp/secret', 'ftp://cdn.example/a', 'data:image/png,a',
                   'https:///missing-host', 'https://user:secret@cdn.example/a',
                   'https://cdn.example:bad/a', 'https://cdn.example/a\r\nX-Key: secret']
        self.opener.open.side_effect = [Response(image_bytes())]
        self.download([self.record(urls=invalid + ['https://cdn.example/good'])])
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertNotIn('secret', self.manifest.read_text(encoding='utf-8'))

    def test_redirect_handler_checks_each_hop_and_allows_relative_http_redirects(self):
        handler = downloader._HTTPRedirects()
        request = urllib.request.Request('https://cdn.example/old')
        for url in ['file:///tmp/secret', 'ftp://example.com/a', 'https://u:secret@example.com/a']:
            with self.subTest(url=url), self.assertRaises(downloader._DownloadError):
                handler.redirect_request(request, None, 302, 'Moved', {}, url)
        redirected = handler.redirect_request(request, None, 302, 'Moved', {}, 'https://cdn.example/new')
        self.assertEqual(redirected.full_url, 'https://cdn.example/new')
        # urllib resolves Location against the original URL before redirect_request.
        relative = urllib.parse.urljoin(request.full_url, '/new')
        self.assertEqual(handler.redirect_request(request, None, 302, 'Moved', {}, relative).full_url,
                         'https://cdn.example/new')
        self.opener.open.return_value = Response(image_bytes(), url='file:///tmp/secret')
        with self.assertRaisesRegex(RuntimeError, 'invalid HTTP'):
            self.download([self.record()])

    def test_manifest_extensions_and_unrequested_records_survive_updates(self):
        self.opener.open.side_effect = [Response(image_bytes()), Response(image_bytes(color='green'))]
        self.download([self.record('old')])
        manifest = json.loads(self.manifest.read_text(encoding='utf-8'))
        manifest['custom'] = {'keep': True}
        next(iter(manifest['records'].values()))['custom'] = 'preserved'
        old = dict(manifest['records'])
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.download([self.record('new')])
        updated = json.loads(self.manifest.read_text(encoding='utf-8'))
        self.assertEqual(updated['custom'], {'keep': True})
        for key, entry in old.items():
            self.assertEqual(updated['records'][key], entry)

    def test_invalid_existing_manifest_is_not_overwritten(self):
        for content in ['{broken json', '{"version": 99, "records": {}}',
                        '{"version": 1, "records": []}', '[]']:
            with self.subTest(content=content):
                self.manifest.write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'existing file was preserved'):
                    self.download([self.record()])
                self.assertEqual(self.manifest.read_text(encoding='utf-8'), content)
        self.opener.open.assert_not_called()

    def test_atomic_manifest_failure_preserves_checkpoint_and_removes_new_image(self):
        self.opener.open.side_effect = [Response(image_bytes()), Response(image_bytes(color='green'))]
        old_path = self.download([self.record('old')])[0]
        before = self.manifest.read_bytes()
        real_replace = downloader.os.replace

        def fail_manifest(source, destination):
            if Path(destination) == self.manifest:
                raise OSError('disk full, private path')
            real_replace(source, destination)

        with patch.object(downloader.os, 'replace', side_effect=fail_manifest):
            with self.assertRaisesRegex(RuntimeError, 'previous checkpoint was preserved'):
                self.download([self.record('new')])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(list(self.output.iterdir()), [old_path])
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_atomic_image_failure_does_not_leave_partial_files(self):
        self.opener.open.return_value = Response(image_bytes())
        real_replace = downloader.os.replace

        def fail_image(source, destination):
            if Path(destination).parent == self.output:
                raise OSError('cannot rename')
            real_replace(source, destination)

        with patch.object(downloader.os, 'replace', side_effect=fail_image):
            with self.assertRaisesRegex(RuntimeError, 'cannot write image'):
                self.download([self.record()])
        self.assertEqual(self.entries()[0]['status'], 'failed')
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(list(self.root.rglob('*.tmp')))

    def test_checkpoint_failure_during_cache_repair_keeps_original_file(self):
        self.opener.open.side_effect = [Response(image_bytes()), Response(image_bytes())]
        path = self.download([self.record()])[0]
        path.write_bytes(b'old corrupt file')
        before = self.manifest.read_bytes()
        real_replace = downloader.os.replace

        def fail_manifest(source, destination):
            if Path(destination) == self.manifest:
                raise OSError('disk full')
            real_replace(source, destination)

        with patch.object(downloader.os, 'replace', side_effect=fail_manifest):
            with self.assertRaises(RuntimeError):
                self.download([self.record()])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(path.read_bytes(), b'old corrupt file')
        self.assertEqual(list(self.output.iterdir()), [path])

    def test_manifest_cache_path_and_symlinks_cannot_escape_output_directory(self):
        data = image_bytes()
        self.opener.open.side_effect = [Response(data), Response(data), Response(data)]
        self.download([self.record()])
        external = self.root / 'outside.png'
        external.write_bytes(data)
        manifest = json.loads(self.manifest.read_text(encoding='utf-8'))
        entry = next(iter(manifest['records'].values()))
        entry['path'] = '../outside.png'
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertNotEqual(self.download([self.record()])[0], external)
        manifest = json.loads(self.manifest.read_text(encoding='utf-8'))
        entry = next(iter(manifest['records'].values()))
        link = self.output / 'escape.png'
        link.symlink_to(external)
        entry['path'] = link.name
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        path = self.download([self.record()])[0]
        self.assertEqual(path.parent, self.output)
        self.assertFalse(path.is_symlink())
        self.assertEqual(external.read_bytes(), data)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_argument_validation_precedes_any_write_or_network(self):
        invalid = [{'records': 'wrong'}, {'records': [{'id': 4, 'urls': []}]},
                   {'records': [{'id': 'x', 'urls': [None]}]}, {'account': None},
                   {'max_bytes': 0}, {'max_bytes': True}, {'timeout': 0},
                   {'timeout': float('inf')}, {'timeout': float('nan')}, {'timeout': True},
                   {'retries': -1}, {'retries': 11}, {'retries': True},
                   {'workers': 0}, {'workers': 9}, {'workers': True},
                   {'item_timeout': 0}, {'item_timeout': float('inf')},
                   {'item_timeout': True}, {'progress': 'yes'}]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.download(**dict({'records': [self.record()]}, **kwargs))
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.output.exists())
        self.opener.open.assert_not_called()

    def test_unknown_manifest_metadata_is_a_cache_miss(self):
        self.opener.open.side_effect = [Response(image_bytes()), Response(image_bytes())]
        self.download([self.record()])
        manifest = json.loads(self.manifest.read_text(encoding='utf-8'))
        entry = next(iter(manifest['records'].values()))
        entry['sha256'] = ['untrusted metadata']
        self.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(self.download([self.record()])[0].read_bytes(), image_bytes())
        self.assertEqual(self.opener.open.call_count, 2)

    def test_missing_pillow_has_clear_error_without_changes(self):
        import builtins
        real_import = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == 'PIL':
                raise ImportError('Pillow not installed')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=without_pillow):
            with self.assertRaisesRegex(RuntimeError, 'Pillow is required'):
                self.download([self.record()])
        self.assertFalse(self.manifest.exists())
        self.opener.open.assert_not_called()

    def test_empty_candidate_list_is_checkpointed_as_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'no candidate URLs'):
            self.download([self.record(urls=[])])
        self.assertEqual(self.entries()[0]['status'], 'failed')
        self.opener.open.assert_not_called()

    def test_empty_batch_has_no_downloads_or_writes(self):
        self.assertEqual(self.download([]), [])
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.output.exists())
        self.opener.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
