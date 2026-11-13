import json
import os
import base64
from PIL import Image
import io

CLASS_NAMES = ['Bagworm']  # Replace with your actual class list

def encode_image_to_base64(image_path):
    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode('utf-8')
    return encoded

def yolo_to_labelme(yolo_file, image_file, output_file):
    # Load image to get dimensions
    with Image.open(image_file) as img:
        img_width, img_height = img.size

    shapes = []
    with open(yolo_file, 'r') as f:
        for line in f:
            cls_id, x_c, y_c, w, h = map(float, line.strip().split())
            class_name = CLASS_NAMES[int(cls_id)]

            x_c *= img_width
            y_c *= img_height
            w *= img_width
            h *= img_height

            x1 = x_c - w / 2
            y1 = y_c - h / 2
            x2 = x_c + w / 2
            y2 = y_c + h / 2

            shape = {
                "label": class_name,
                "points": [[x1, y1], [x2, y2]],
                "group_id": None,
                "shape_type": "rectangle",
                "flags": {}
            }
            shapes.append(shape)

    labelme_json = {
        "version": "5.0.1",
        "flags": {},
        "shapes": shapes,
        "imagePath": os.path.basename(image_file),
        "imageData": encode_image_to_base64(image_file),
        "imageHeight": img_height,
        "imageWidth": img_width
    }

    with open(output_file, 'w') as out_file:
        json.dump(labelme_json, out_file, indent=2)