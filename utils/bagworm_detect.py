import numpy as np
import pandas as pd
import shutil
import math
import pickle
import torch, torchvision
import detectron2
import os, json, cv2, random
import cv2
from PIL import Image

from io import BytesIO


from ultralytics import YOLO
from detectron2.utils.logger import setup_logger
setup_logger()

# import some common detectron2 utilities
from PIL import Image
from detectron2 import model_zoo
from detectron2.structures import BitMasks
from detectron2.engine import DefaultPredictor
from detectron2.config import get_cfg
from detectron2.utils.visualizer import Visualizer
from detectron2.utils.visualizer import ColorMode
from detectron2.data import MetadataCatalog, DatasetCatalog
from detectron2.structures import BoxMode
from matplotlib import pyplot as plt
from detectron2.utils.visualizer import Visualizer
from detectron2.evaluation import COCOEvaluator, inference_on_dataset
from detectron2.data import build_detection_test_loader
from detectron2.engine import DefaultTrainer
from detectron2.data.datasets import register_coco_instances

def load_model(config_path): # file on config.pkl
     with open(config_path, 'rb') as f:
          cfg = pickle.load(f)

     cfg.MODEL.WEIGHTS = os.path.join(cfg.OUTPUT_DIR, "model_final.pth")
     cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.3
     predictor = DefaultPredictor(cfg)
     return predictor

def getmask(im,predictor):
    outputs = predictor(im)
    instances = outputs["instances"]
    masks = instances.pred_masks
    bbox = instances.pred_boxes.tensor.cpu().numpy()
    masks2 = instances.pred_masks.cpu().numpy()
    classes = instances.pred_classes

    class_colors = [
    (255, 0, 0),    # Class 0 - Red
    ]

    # Color masks based on classes
    colored_image = im.copy()
    for mask, class_id in zip(masks, classes):
        color = class_colors[class_id]
        mask_bool = mask.to("cpu").numpy()
        colored_mask = np.zeros((mask_bool.shape[0], mask_bool.shape[1], 3), dtype=np.uint8)
        colored_mask[mask_bool] = color

        # Apply the colored mask to the original image
        colored_image = cv2.addWeighted(colored_image, 1, colored_mask, 1, 0)
        colored_image[mask_bool] = color
        
    return bbox, masks, outputs, instances, colored_image

def classify(img):
    model = YOLO("models/bag-dam.pt")
    try:
        results = model(img, imgsz=640)
    except:
        print("Error")
    obj=[]
    for result in results:
        probs = result.probs.top1  # Probs object for classification outputs
        obj.append(probs)

    return obj

def count_bgwm(bounding_box, im1):
     numb=[]
     for i, bbox in enumerate(bounding_box):
        xmin, ymin, xmax, ymax = bbox 
        height=ymax-ymin
        width=xmax-xmin
        goras = math.sqrt(height**2 + width**2)
        wx=xmin+((xmax-xmin)/2)
        hy=ymin+((ymax-ymin)/2)

        xmin=wx-(goras)
        ymin=hy-(goras)
        xmax=wx+(goras)
        ymax=hy+(goras)
        
        if xmax>1920:
            xmax=1920
        elif ymax>1080:
            ymax=1080
        elif ymin<0:
            ymin=0
        elif xmin<0:
            xmin=0
          
        crop_img=im1.crop((xmin, ymin, xmax, ymax))
        img_resized = crop_img.resize((256, 256))
        img_opencv = np.array(img_resized)
        img_opencv = cv2.cvtColor(img_opencv, cv2.COLOR_RGB2BGR)
        obj = classify(img_opencv)
        if obj==[0]:
            numb.append(i)
        else:
            pass

        # object.append(obj)

     res=round(len(numb))
     return res


    
        

