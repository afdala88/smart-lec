import cv2,os
import numpy as np
from skimage import exposure
from PIL import Image

def match_histograms(target_image, reference_image):
    # Load the images
    target_image1 = cv2.imread(target_image)
    reference_image1 = cv2.imread(reference_image)

    # Convert the images from BGR to RGB for histogram matching
    target_image_rgb = cv2.cvtColor(target_image1, cv2.COLOR_BGR2RGB)
    reference_image_rgb = cv2.cvtColor(reference_image1, cv2.COLOR_BGR2RGB)
    matched_image = exposure.match_histograms(target_image_rgb, reference_image_rgb, channel_axis=-1)

    # Convert the matched image back to BGR for displaying/saving
    matched_image_bgr = cv2.cvtColor(matched_image, cv2.COLOR_RGB2BGR)

    # Match histograms (assuming images are in BGR format)
    return matched_image_bgr

def remove_similar_img(folder_path, similarity_threshold=0.3):

    def get_main_contour(image_path):
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _, thresh = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            return max(contours, key=cv2.contourArea)
        return None

    images = [f for f in os.listdir(folder_path) if f.lower().endswith((".png", ".jpg", ".jpeg"))]
    checked = set()
    deleted = []

    for i, img1 in enumerate(images):
        if img1 in checked:
            continue

        path1 = os.path.join(folder_path, img1)
        contour1 = get_main_contour(path1)
        if contour1 is None:
            continue

        for j in range(i + 1, len(images)):
            img2 = images[j]
            if img2 in checked:
                continue

            path2 = os.path.join(folder_path, img2)
            contour2 = get_main_contour(path2)
            if contour2 is None:
                continue

            similarity = cv2.matchShapes(contour1, contour2, cv2.CONTOURS_MATCH_I1, 0.0)

            if similarity < similarity_threshold:
                os.remove(path2)
                deleted.append(img2)
                checked.add(img2)

        checked.add(img1)

    return deleted