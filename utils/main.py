import os
from bagworm_detect import load_model, getmask, count_bgwm
from input_load import load_img
from gps_match import matching_gps, count_row,count_files_in_folder

config_path = 'models/lec_v5/config.pkl'
img_ori = 'img_ori/'
img_in = 'img_input/'
gps = 'gps_data/NGLE/D29.xls'

numb_ori = int(count_files_in_folder(img_ori))
numb_target = count_row(gps)

if ((numb_ori)/2)>=numb_target:
    n_ori, n_input = matching_gps(img_ori, img_in, gps)
    result=[]
    for filename in os.listdir(img_in):
        im, img=load_img(img_in+filename)
        pred=load_model(config_path)
        bounding_box, masks, data, instances, colored_images = getmask(im, pred)
        numb = count_bgwm(bounding_box, img)
        res=[filename,numb]
        result.append(res)
    print(result)

else:
    print('your image not match with target')