import exifread
import piexif
from PIL import Image
import piexif.helper

# Function to extract EXIF and GPS data
def read_exif(image_path):
    with open(image_path, 'rb') as f:
        tags = exifread.process_file(f)

    exif_data = {}
    gps_data = {}

    for tag, value in tags.items():
        tag_str = str(tag)
        if tag_str.startswith("GPS"):  # Extract GPS metadata separately
            gps_data[tag_str] = value
        else:
            exif_data[tag_str] = str(value)

    return exif_data, gps_data

# Convert GPS coordinates to EXIF format
def convert_gps(gps_value):
    """ Converts GPS coordinates from EXIF format to degrees, minutes, and seconds. """
    d, m, s = [float(x.num) / float(x.den) for x in gps_value.values]
    return (int(d), int(m), float(s))

# Function to write EXIF (including GPS) to another image
def write_exif(source_image, target_image):
    exif_data, gps_data = read_exif(source_image)
    
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}

    # Copying normal EXIF data
    if "EXIF DateTimeOriginal" in exif_data:
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_data["EXIF DateTimeOriginal"].encode()

    # Copying GPS data (if available)
    if "GPS GPSLatitude" in gps_data and "GPS GPSLongitude" in gps_data:
        lat = convert_gps(gps_data["GPS GPSLatitude"])
        lon = convert_gps(gps_data["GPS GPSLongitude"])

        # EXIF GPS format
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = "N".encode() if lat[0] >= 0 else "S".encode()
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = "E".encode() if lon[0] >= 0 else "W".encode()
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = [(lat[0], 1), (lat[1], 1), (int(lat[2] * 1000000), 1000000)]
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = [(lon[0], 1), (lon[1], 1), (int(lon[2] * 1000000), 1000000)]

    # Convert to EXIF bytes
    exif_bytes = piexif.dump(exif_dict)

    # Apply EXIF to the new image
    img = Image.open(target_image)
    img.save(target_image, exif=exif_bytes)