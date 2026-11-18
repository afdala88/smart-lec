"""Latitude / longitude per Baris-Pokok, read from the EXIF of the second F1 photo.

Note: for drone photos this is the drone's position when the photo was taken,
not the exact tree position.

Used for the GPS CSV download in Historical Census. Coordinates are read
during the census run (while the photos still exist) and stored per session
in census_sessions.gps_data as {"<Baris-Pokok>": [lat, lon], ...}.
"""
import os
import re

_IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')


def _num(v):
    if isinstance(v, tuple) and len(v) == 2:          # old Pillow: (num, den)
        return float(v[0]) / float(v[1]) if v[1] else 0.0
    return float(v)                                   # IFDRational


def read_latlon(path):
    """(lat, lon) in decimal degrees from EXIF GPS, or None."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            gps = im.getexif().get_ifd(0x8825)        # GPSInfo IFD
        # 1 LatitudeRef, 2 Latitude, 3 LongitudeRef, 4 Longitude
        if not gps or 2 not in gps or 4 not in gps:
            return None

        def _dec(dms, ref):
            d, m, s = (_num(x) for x in dms)
            val = d + m / 60.0 + s / 3600.0
            if isinstance(ref, bytes):
                ref = ref.decode(errors='ignore')
            return -val if str(ref).strip().upper().startswith(('S', 'W')) else val

        lat = _dec(gps[2], gps.get(1, 'N'))
        lon = _dec(gps[4], gps.get(3, 'E'))
        if lat == 0 and lon == 0:
            return None
        return round(lat, 7), round(lon, 7)
    except Exception:
        return None


def _find_frond_dir(row_dir, frond='F2'):
    try:
        for d in os.listdir(row_dir):
            if d.strip().upper() == frond and os.path.isdir(os.path.join(row_dir, d)):
                return os.path.join(row_dir, d)
    except OSError:
        pass
    return None


SOURCE_FROND = 'F1'
SOURCE_INDEX = 1                     # 0-based: 1 = second photo
SOURCE_TAG   = f'{SOURCE_FROND}#{SOURCE_INDEX + 1}'   # stored as "_src" in gps_data


def _natural_key(name):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', name)]


def coords_for_folder(root_folder, frond=SOURCE_FROND, index=SOURCE_INDEX):
    """{Baris-Pokok: [lat, lon], "_src": "F1#2"} from the photo at `index`
    (sorted by name) in each row's frond folder. If that photo has no GPS,
    the next photos are tried, then the earlier ones."""
    out = {}
    if not root_folder or not os.path.isdir(root_folder):
        return out
    for sf in sorted(os.listdir(root_folder)):
        fdir = _find_frond_dir(os.path.join(root_folder, sf), frond)
        if not fdir:
            continue
        imgs = sorted((n for n in os.listdir(fdir) if n.lower().endswith(_IMG_EXT)),
                      key=_natural_key)
        if not imgs:
            continue
        start = min(index, len(imgs) - 1)
        for name in imgs[start:] + imgs[:start][::-1]:
            ll = read_latlon(os.path.join(fdir, name))
            if ll:
                out[sf] = list(ll)
                break
    if out:
        out['_src'] = SOURCE_TAG
    return out
