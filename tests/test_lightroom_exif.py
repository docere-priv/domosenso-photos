import importlib
import io
import sys
import types
from datetime import datetime
from pathlib import Path

from PIL import Image

pkg = types.ModuleType('lightroom_exif_pkg')
pkg.__path__ = [str(Path(__file__).parents[1] / 'custom_components/domosenso_photos')]
sys.modules[pkg.__name__] = pkg
le = importlib.import_module('lightroom_exif_pkg.lightroom_exif')
lc = importlib.import_module('lightroom_exif_pkg.lightroom_catalog')
album_image = importlib.import_module('lightroom_exif_pkg.album_image')

EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def _jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (400, 300), 'gray').save(buf, 'JPEG')
    return buf.getvalue()


def _photo(meta):
    return lc.Photo('abc', 0, None, datetime(2024, 5, 3, 17, 3, 26), 'x', False, (), meta)


def _read(photo):
    data = album_image.normalize(_jpeg(), 200, 85, le.exif_adder(photo))
    return Image.open(io.BytesIO(data)).getexif()


def test_exif_round_trip_through_normalize():
    meta = lc.CameraMeta('Fabrikam', 'FX-100', 'Fabrikam 35mm F1.8', 35.0, 52.0, 1.8, 0.0025, 200)
    exif = _read(_photo(meta))
    sub = exif.get_ifd(EXIF_IFD)
    assert exif[306] == '2024:05:03 17:03:26' and sub[36867] == '2024:05:03 17:03:26'
    assert exif[271] == 'Fabrikam' and exif[272] == 'FX-100'
    assert sub[0xA434] == 'Fabrikam 35mm F1.8'
    assert float(sub[0x920A]) == 35.0
    assert abs(float(sub[0x829D]) - 1.8) < 1e-6
    assert abs(float(sub[0x829A]) - 0.0025) < 1e-6
    assert sub[0xA405] == 52 and sub[0x8827] == 200


def test_exif_skips_missing_values():
    exif = _read(_photo(lc.CameraMeta(None, None, None, None, None, None, None, None)))
    sub = exif.get_ifd(EXIF_IFD)
    assert exif[306] == '2024:05:03 17:03:26' and sub[36867] == '2024:05:03 17:03:26'
    assert {t for t in exif if t not in (EXIF_IFD, GPS_IFD, 0x0112)} == {306}
    assert set(sub) == {36867}


def test_exif_writes_no_gps():
    meta = lc.CameraMeta('Fabrikam', 'FX-100', 'L', 35.0, 52.0, 1.8, 0.0025, 200)
    exif = _read(_photo(meta))
    assert not exif.get_ifd(GPS_IFD)  # a pointer to an empty IFD may remain; no tag does
