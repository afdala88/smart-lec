import piexif
import os
from PIL import Image
from pyproj import Proj, transform
import math 
import pandas as pd
import numpy as np
import string
# import shutil

def get_decimal_from_dms(dms, ref):
    degrees = dms[0][0] / dms[0][1]
    minutes = dms[1][0] / dms[1][1]
    seconds = dms[2][0] / dms[2][1]

    decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
    if ref in ['S', 'W']:
        decimal = -decimal

    return decimal

def extract_gps_info(image_path):
    img = Image.open(image_path)
    exif_data = img.info.get('exif')

    if not exif_data:
        raise ValueError("Image has no EXIF metadata.")

    exif_dict = piexif.load(exif_data)
    gps_data = exif_dict.get('GPS')

    if not gps_data:
        raise ValueError("Image has no GPS information.")

    latitude = get_decimal_from_dms(gps_data[piexif.GPSIFD.GPSLatitude], gps_data[piexif.GPSIFD.GPSLatitudeRef].decode())
    longitude = get_decimal_from_dms(gps_data[piexif.GPSIFD.GPSLongitude], gps_data[piexif.GPSIFD.GPSLongitudeRef].decode())

    altitude = None
    if piexif.GPSIFD.GPSAltitude in gps_data:
        altitude = gps_data[piexif.GPSIFD.GPSAltitude][0] / gps_data[piexif.GPSIFD.GPSAltitude][1]

    return latitude, longitude, altitude

def decimal_degrees_to_utm(lat, lon):
    easting, northing = transform(wgs84, utm, lon, lat)
    return easting, northing

def distance_m(lat1, lon1, lat2, lon2):
    distance = math.sqrt(((lat1-lat2)**2) +((lon1-lon2)**2))
    return distance

def count_files_in_folder(folder_path):
    # Get a list of all files and directories in the specified folder
    items = os.listdir(folder_path)
    # Filter out the files from the list and count them
    file_count = sum(1 for item in items if os.path.isfile(os.path.join(folder_path, item)))
    return file_count

def count_row(gps_data):
    df = pd.read_excel(gps_data)
    row_sum = df.dropna(how='all').shape[0]
    return row_sum

# Define the projection for WGS84 (latitude and longitude)
wgs84 = Proj(proj='latlong', datum='WGS84')

# Define the projection for UTM (you need to specify the UTM zone)
# For example, for UTM zone 47N:
utm = Proj(proj='utm', zone=47, datum='WGS84')

def matching_gps(folder_path, folder_output, gps_data): ### gps_data in xls

    if not os.path.exists(folder_output):
        os.mkdir(folder_output)
    characters = string.ascii_uppercase

    # coord=[]
    for filepath in os.listdir(folder_path):
        if filepath=="Thumbs.db":
            os.remove(folder_path+filepath)
        else:
            image_path = folder_path+filepath
            try:
                latitude, longitude, altitude = extract_gps_info(image_path)
            except ValueError as e:
                print(e)
            # excel_path= gps_data
            df = pd.read_excel(gps_data)
            dist=[]
            id=[]

            for index2, row2 in df.iterrows():
                latitude2 = float(row2['LATITUDE'])
                longitude2 = float(row2['LONGITUDE'])

                lat1, lon1 = decimal_degrees_to_utm(latitude, longitude)
                lat2, lon2 = decimal_degrees_to_utm(latitude2, longitude2)

                distance=distance_m(lat1, lon1, lat2, lon2)
                dist.append(distance)
                id.append(row2['NO_BP'])

            min_indx = np.argmin(np.array(dist))
            if dist[min_indx]<=70.0:
                # res=[filepath,id[min_indx],dist[min_indx]]
                # coord.append(res)
                for char in characters:
                    try:
                        # shutil.copy(folder_path + filepath, folder_output + str(id[min_indx]) + f'_{char}.jpg')
                        os.rename(folder_path + filepath, folder_output + str(id[min_indx]) + f'_{char}.jpg')
                    except:
                        pass
            else:
                # res=[filepath,id[min_indx],"Out_of_Area"]
                # coord.append(res)
                pass

    number_file_ori = count_files_in_folder(folder_path)
    number_file_out = count_files_in_folder(folder_output)
    
    return number_file_ori, number_file_out