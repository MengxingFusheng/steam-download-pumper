import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_publisher.discovery import DEFAULT_DIRECTORIES, collect_candidates
from source_publisher.probe import PinnedResponse


class PublisherDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'candidates.json'
        self.write([])

    def write(self, sources, directories=None):
        self.path.write_text(json.dumps({'schema': 1, 'sources': sources, 'directories': directories or []}))

    def collect(self, request_fn, **kwargs):
        return collect_candidates(self.path, self.root, deadline=time.monotonic() + 10,
                                  cancel_event=threading.Event(), request_fn=request_fn, **kwargs)

    def test_official_download_paths_and_ookla_are_merged_with_manual_and_manifest(self):
        self.write([{'url': 'https://manual.example/file', 'enabled': True}])
        requests = []
        def request(url, **kwargs):
            requests.append(kwargs)
            if 'librespeed' in url:
                return PinnedResponse(200, {}, json.dumps([
                    {'server': 'https://speed.example/custom', 'dlURL': 'dl.php?size=100'},
                    {'server': 'https://speed2.example/', 'dlURL': 'backend/garbage.php'},
                ]).encode())
            return PinnedResponse(200, {}, json.dumps([{'id': '1', 'host': 'ookla.example:8080', 'url': 'https://ookla.example:8080/speedtest/upload.php', 'https_functional': 1}]).encode())
        result = self.collect(request, accepted_urls=['https://accepted.example/file'])
        self.assertEqual(result.urls, ['https://ookla.example:8080/download?size=67108864&guid={{guid}}&nocache={{nonce}}',
                                      'https://manual.example/file', 'https://accepted.example/file',
                                      'https://speed.example/custom/dl.php?size=100',
                                      'https://speed2.example/backend/garbage.php?ckSize=64'])
        self.assertTrue(all(r['max_bytes'] == 2 * 1024 * 1024 + 1 for r in requests))
        self.assertTrue(all(r['deadline'] <= time.monotonic() + 10 for r in requests))

    def test_http_403_keeps_bounded_cache_without_bypassing_directory(self):
        def success(url, **kwargs):
            if 'librespeed' in url:
                return PinnedResponse(200, {}, b'[{"server":"https://cached.example/","dlURL":"backend/garbage.php"}]')
            return PinnedResponse(403, {}, b'forbidden')
        self.collect(success)
        result = self.collect(lambda *a, **k: PinnedResponse(403, {}, b'forbidden'))
        self.assertEqual(result.urls, ['https://cached.example/backend/garbage.php?ckSize=64'])
        self.assertTrue(result.diagnostics['directories'][0]['cached'])
        self.assertEqual(result.diagnostics['directories'][1]['error'], 'directory HTTP 403')

    def test_mirror_only_discovers_matching_files_within_allowed_path(self):
        mirror = {'kind': 'mirror', 'url': 'https://mirror.example/releases/24.04/',
                  'purpose': 'Existing public image mirror', 'pattern': r'ubuntu-[0-9.]+-amd64\.iso'}
        self.write([], [mirror])
        def request(url, **kwargs):
            if 'mirror.example' in url:
                return PinnedResponse(200, {}, b'<a href="ubuntu-24.04.5-amd64.iso">new</a>'
                    b'<a href="../ubuntu-1-amd64.iso">outside</a>'
                    b'<a href="https://other.example/releases/24.04/ubuntu-2-amd64.iso">other</a>'
                    b'<a href="ubuntu-24.04.5-amd64.iso.sha256">checksum</a>')
            return PinnedResponse(403, {}, b'')
        self.assertEqual(self.collect(request).urls, ['https://mirror.example/releases/24.04/ubuntu-24.04.5-amd64.iso'])

    def test_mirror_redirect_cannot_leave_allowed_directory(self):
        self.write([], [{'kind': 'mirror', 'url': 'https://mirror.example/releases/', 'purpose': 'Existing mirror'}])
        requested = []
        def request(url, **kwargs):
            requested.append(url)
            if "mirror.example" not in url:
                return PinnedResponse(403, {}, b"forbidden")
            return PinnedResponse(302, {'location': 'http://127.0.0.1/private'}, b'')
        self.collect(request)
        self.assertNotIn('http://127.0.0.1/private', requested)

    def test_merge_caps_at_200_deduplicates_and_respects_explicit_disable(self):
        self.write([{'url': 'https://disabled.example/file', 'enabled': False},
                    {'url': 'https://manual.example/file', 'enabled': True}])
        accepted = ['https://disabled.example/file'] + [f'https://accepted{i}.example/file' for i in range(250)]
        result = self.collect(lambda *a, **k: PinnedResponse(403, {}, b''), accepted_urls=accepted)
        self.assertEqual(len(result.urls), 200)
        self.assertEqual(result.urls[0], 'https://manual.example/file')
        self.assertNotIn('https://disabled.example/file', result.urls)

    def test_oversize_invalid_xml_and_missing_mirror_purpose_are_rejected(self):
        result = self.collect(lambda *a, **k: PinnedResponse(200, {}, b'x' * (2 * 1024 * 1024 + 1)))
        self.assertFalse(result.urls)
        self.write([], [{'kind': 'mirror', 'url': 'https://mirror.example/releases/'}])
        with self.assertRaises(ValueError):
            self.collect(lambda *a, **k: PinnedResponse(403, {}, b''))

    def test_disabled_discovery_never_uses_network(self):
        def request(*args, **kwargs):
            self.fail('disabled discovery attempted a request')
        self.assertEqual(self.collect(request, enabled=False).urls, [])

    def test_regions_are_interleaved_and_disable_stops_api_requests(self):
        from urllib.parse import parse_qs, urlsplit
        requested = []
        def request(url, **kwargs):
            requested.append(url)
            if 'speedtest.net' not in url:
                return PinnedResponse(403, {}, b'')
            region = parse_qs(urlsplit(url).query).get('search', ['near'])[0]
            return PinnedResponse(200, {}, json.dumps([
                {'id': str(i), 'host': f'{region.lower()}{i}.example:8080',
                 'url': f'http://{region.lower()}{i}.example:8080/speedtest/upload.php', 'https_functional': 1}
                for i in range(100)]).encode())
        result = self.collect(request)
        self.assertEqual(len(result.urls), 200)
        self.assertIn('near0.example', result.urls[0])
        self.assertIn('tianjin0.example', result.urls[1])
        self.assertIn('beijing0.example', result.urls[2])
        self.assertIn('shanghai0.example', result.urls[3])
        self.assertEqual(result.diagnostics['directories'][1]['servers'][0]['id'], '0')
        requested.clear()
        self.collect(request, ookla_enabled=False)
        self.assertFalse(any('speedtest.net' in url for url in requested))

    def test_api_rejects_malformed_hosts_and_preserves_explicit_http(self):
        from source_publisher.discovery import _parse_directory
        entry = {'kind': 'ookla_api'}
        servers = [{'host': 'bad.example@127.0.0.1', 'https_functional': 1},
                   {'host': 'good.example:8080', 'url': 'http://good.example:8080/speedtest/upload.php', 'https_functional': 0}]
        result = _parse_directory(entry, json.dumps(servers).encode())
        self.assertEqual(result, ['http://good.example:8080/download?size=67108864&guid={{guid}}&nocache={{nonce}}'])

    def test_manual_legacy_host_is_verified_as_a_direct_seed_when_api_has_no_region(self):
        self.write([{'url':'http://shanghai.example:8080/speedtest/random4000x4000.jpg','enabled':True}])
        result = self.collect(lambda *args, **kwargs: PinnedResponse(403, {}, b''))
        self.assertEqual(result.urls[:2], [
            'https://shanghai.example:8080/download?size=67108864&guid={{guid}}&nocache={{nonce}}',
            'http://shanghai.example:8080/download?size=67108864&guid={{guid}}&nocache={{nonce}}'])
