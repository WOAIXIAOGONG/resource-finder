"""Actual AVIF bytes, disk cache, fallback and host failure recovery."""
import testenv
import sys
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtGui import QImage
from app.imagecodec import prepare_image, is_avif, ImageDecodeError
from app.thumbcache import ThumbCache
from app.config import Settings
from app.net import NetworkError


def encoded(fmt, size=(64, 48), mode='RGB'):
    out = BytesIO()
    im = Image.new(mode, size, (70, 130, 210, 128) if mode == 'RGBA' else (70, 130, 210))
    im.save(out, format=fmt)
    return out.getvalue()


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.http = Mock()
        self.cache = ThumbCache(Settings(), self.http, root=self.tmp.name)

    def tearDown(self):
        self.cache._pool.shutdown(wait=True)
        self.tmp.cleanup()

    def request(self, url):
        event, result = threading.Event(), []
        self.cache.request(url, done=lambda u, p: (result.append(p), event.set()))
        self.assertTrue(event.wait(5))
        return result[0]

    def test_avif_jpg_url_becomes_png_and_cache_is_reused(self):
        data = encoded('AVIF', mode='RGBA')
        self.assertTrue(is_avif(data))
        self.http.get_bytes.return_value = data
        path = self.request('https://i.ytimg.com/vi/abcdefghijk/hq720.jpg')
        self.assertTrue(path)
        self.assertTrue(Path(path).read_bytes().startswith(b'\x89PNG'))
        im = QImage(path)
        self.assertEqual((im.width(), im.height()), (64, 48))
        self.assertTrue(im.hasAlphaChannel())
        self.assertTrue(110 < im.pixelColor(5, 5).alpha() < 145)
        self.assertEqual(self.request('https://i.ytimg.com/vi/abcdefghijk/hq720.jpg'), path)
        self.http.get_bytes.assert_called_once()

    def test_small_valid_png_and_jpeg_and_webp_are_not_rejected_by_byte_count(self):
        for fmt in ('PNG', 'JPEG', 'WEBP'):
            data = encoded(fmt)
            self.assertLess(len(data), 900)
            self.assertFalse(QImage.fromData(prepare_image(data)).isNull())

    def test_invalid_or_tiny_images_are_rejected(self):
        for data in (b'', b'<html>blocked</html>', b'\xff\xd8\xff' + b'x'*2000,
                     encoded('PNG', (1, 1)), b'\x00\x00\x00\x20ftypavif' + b'x'*100):
            with self.assertRaises(ImageDecodeError): prepare_image(data)

    def test_size_limit_before_avif_full_decode(self):
        with patch('app.imagecodec.MAX_PIXELS', 100):
            with self.assertRaises(ImageDecodeError): prepare_image(encoded('AVIF'))

    def test_many_bad_images_do_not_block_valid_fallback_on_same_host(self):
        self.http.get_bytes.return_value = b'<html>bad image</html>'
        for i in range(5):
            self.assertIsNone(self.request('https://i.ytimg.com/bad%d.jpg' % i))
        self.assertFalse(self.cache._host_dead('https://i.ytimg.com/'))
        self.http.get_bytes.return_value = encoded('JPEG')
        event, paths = threading.Event(), []
        self.cache.request_video('https://youtube.com/watch?v=abcdefghijk',
                                 done=lambda u, p: (paths.append(p), event.set()))
        self.assertTrue(event.wait(5))
        self.assertTrue(paths[0])

    def test_skipped_requests_do_not_extend_host_cooldown(self):
        self.http.get_bytes.side_effect = NetworkError('timeout')
        with patch('app.thumbcache.time.monotonic', return_value=100):
            for i in range(4): self.assertIsNone(self.request('https://i.ytimg.com/fail%d' % i))
            self.assertTrue(self.cache._host_dead('https://i.ytimg.com'))
        with patch('app.thumbcache.time.monotonic', return_value=150):
            for i in range(5): self.assertIsNone(self.request('https://i.ytimg.com/skip%d' % i))
            self.assertEqual(self.cache._host_failed_at['i.ytimg.com'], 100)
            self.assertEqual(self.http.get_bytes.call_count, 4)
        self.http.get_bytes.side_effect = None
        self.http.get_bytes.return_value = encoded('JPEG')
        with patch('app.thumbcache.time.monotonic', return_value=161):
            self.assertTrue(self.request('https://i.ytimg.com/skip0'))
        self.assertNotIn('i.ytimg.com', self.cache._failed_hosts)

    def test_late_inflight_failure_does_not_postpone_open_circuit(self):
        with patch('app.thumbcache.time.monotonic', return_value=100):
            for _ in range(4): self.cache._note_host('https://i.ytimg.com/', False)
        with patch('app.thumbcache.time.monotonic', return_value=150):
            self.cache._note_host('https://i.ytimg.com/', False)
            self.assertEqual(self.cache._host_failed_at['i.ytimg.com'], 100)

    def test_404_and_disk_error_do_not_mark_host_unreachable(self):
        self.http.get_bytes.side_effect = NetworkError('missing', status=404)
        for i in range(4): self.assertIsNone(self.request('https://i.ytimg.com/404-%d' % i))
        self.assertFalse(self.cache._host_dead('https://i.ytimg.com'))
        self.http.get_bytes.side_effect = None
        self.http.get_bytes.return_value = encoded('PNG')
        with patch('app.thumbcache.os.replace', side_effect=OSError('disk full')):
            self.assertIsNone(self.request('https://i.ytimg.com/disk'))
        self.assertFalse(self.cache._host_dead('https://i.ytimg.com'))
        self.assertFalse(list(Path(self.tmp.name).rglob('*.part')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
