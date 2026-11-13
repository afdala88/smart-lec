import os
import cv2
from PIL import Image

def load_img(img_path):
    im1 = Image.open(img_path)
    im = cv2.imread(img_path)
    ima = im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
    imf = cv2.cvtColor(ima, cv2.COLOR_RGB2BGR)
    return imf, im1

    