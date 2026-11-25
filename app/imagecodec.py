"""Decode covers by content, converting AVIF to a Qt-readable cache file."""
from io import BytesIO

from PIL import Image, AvifImagePlugin  # Explicit import also includes the codec in frozen builds.
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader

MAX_PIXELS = 20_000_000


class ImageDecodeError(ValueError):
    pass


def is_avif(data):
    # ISO BMFF ftyp compatible brands, independent of extension / MIME type.
    if len(data) < 16 or data[4:8] != b'ftyp':
        return False
    end = min(int.from_bytes(data[:4], 'big'), len(data), 256)
    return any(data[i:i + 4] in (b'avif', b'avis') for i in range(8, end, 4))


def _check_size(width, height):
    if min(width, height) < 16 or width * height > MAX_PIXELS:
        raise ImageDecodeError('图片尺寸不可用或超过封面解码上限')


def prepare_image(data):
    """Called only in a thumbnail worker; returns fully decoded, Qt-readable bytes."""
    if not data:
        raise ImageDecodeError('图片响应为空')
    try:
        if is_avif(data):
            with Image.open(BytesIO(data), formats=['AVIF']) as im:
                _check_size(*im.size)
                im.seek(0)
                im.load()
                im.thumbnail((2048, 2048))
                converted = im.convert('RGBA' if 'A' in im.getbands() else 'RGB')
                out = BytesIO()
                converted.save(out, format='PNG', compress_level=3)
                data = out.getvalue()
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        _check_size(size.width(), size.height())
        image = reader.read()
        if image.isNull():
            raise ImageDecodeError('图片解码失败：' + reader.errorString())
        return data
    except ImageDecodeError:
        raise
    except Exception as exc:
        raise ImageDecodeError('图片格式无法解码：%s' % exc) from exc
