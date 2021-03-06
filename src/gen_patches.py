#%matplotlib inline
from PIL import Image
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os
from skimage.filters import threshold_otsu
from skimage.transform import resize
from skimage.transform import rotate
from skimage.util import view_as_windows
from collections import defaultdict
import cv2

## Steps from https://arxiv.org/pdf/1707.06978.pdf:
## Resize calcifications to between 2750x1500 with random uniform sampling of the factors
## Resize masses to between  1100x600 with random uniform sampling of the factors
## Horizontal flipping 
## Rotation up to 30 degrees
## Use Otsu's segmentationt ot remove all purely black patches

###########################################################################################################################
# Utils
###########################################################################################################################


np.random.seed(1234)

PATH_TO_FILES = '/home/jlandesman/data/cbis-ddsm/calc_training_full_mammogram_images/'
PATH_TO_ROI = '/home/jlandesman/data/cbis-ddsm/calc_training_full_roi_images/'
PATH_TO_ROI_CSV_LABELS = '/home/jlandesman/data/cbis-ddsm/calc_case_description_train_set.csv'

CALC_TARGET_RESIZE = np.array([2750,1500])
MASS_TARGET_RESIZE = np.array([1100, 600])
MAX_ROTATE = 30 ## degrees
STEP_SIZE = 100 ## Stride for getting windows

MASK_CUTOFF = 0 ## If a patch has an average mask value of 0 discard it as it is not in the breast
ROI_CUTOFF = 0 ## If an ROI has an average value of zero, label it "no_tumor" 

###################################################################
# Read in Files
###################################################################
def get_im_as_array(file_name, file_type):
    '''
    Read in an image and yield it as a numpy array
    params: 
        file_name: name of the file
        file_type: either 'full' or 'ROI'
    
    '''
    if file_type == 'full':
        path = PATH_TO_FILES
    elif file_type == 'ROI': ## ROI
        path = PATH_TO_ROI
    else: 
        print("Enter file type as either 'full' or 'ROI'")
        pass

    file_path = os.path.join(path,file_name)
    im = Image.open(file_path)
    return np.asarray(im)

###################################################################
# Associate images with labels
###################################################################

def get_labels(path_to_csv):
    '''
    Concatenates various components of the named files to a list and returns the file_name and pathology
    params:
        path_to_csv: path to the CSV with the file list
    returns: 
        a data frame containing the file_name (as an index) and the pathology.  
    '''
    df = pd.read_csv(path_to_csv)
    df['file_name'] = 'Calc-Training_' + df['patient_id'] + '_' + df['side'] + '_' + df['view'] + '_' + df['abn_num'].astype(str) + '_mask.png'
    df = df[['file_name', 'pathology']]
    df.set_index('file_name', inplace=True)
    return df

def get_mask_list():
    '''
    Associate each file with all of its masses and their pathology (benign, malignant, other).
    Return a dictionary of {file_name: (mask, pathology)}
    '''
    mask_list = defaultdict(list)
    roi_files = os.listdir(PATH_TO_ROI)
    df = get_labels(PATH_TO_ROI_CSV_LABELS)

    for file_name in roi_files:
        mask_list[file_name[:-11]].append((file_name, df.loc[file_name]['pathology']))
    
    return mask_list

###################################################################
# Image transformations
###################################################################

def get_resize_max_min(im, tumor_type):
    '''
    Returns the max and min dimensions for resizing per the paper
    params:
        im = image
        tumor_type = either 'CALC' or 'MASS'
    returns: 
        the minimum and the maxiumum dimensions for resizing.  This is the range that is then sampled uniformly.
    '''
    if tumor_type == 'CALC':
        resize_min, resize_max = CALC_TARGET_RESIZE/np.array(im.shape)
        
    elif tumor_type == 'MASS':
        resize_min, resize_max = MASS_TARGET_RESIZE/np.array(im.shape)
    
    else: 
        print('Enter either CALC or MASS')
        pass
    return resize_min, resize_max                                                        

def rotate_image(im, rotation_angle):
    '''
    Rotates the image to a random angle < max_rotate
    '''
    return rotate(im, rotation_angle)

def normalize(im):
    '''
    Normalize to between 0 and 255
    '''
    im_normalized = (255*(im - np.max(im))/-np.ptp(im))    
    return im_normalized

###################################################################
# Get patches
###################################################################

def get_patches(im, step_size = 20, dimensions = [256, 256]):
    '''
    Return sliding windows along the breast, moving STEP_SIZE pixels at a time.
    
    IMPORTANT: np.reshape() does not guarantee a copy isn't made - this leads to memory errors
    
    params:
        step_size: the stride by which the window jumps
        dimemsions: the dimensions of the patch
    '''
    patches = view_as_windows(im,dimensions,step=step_size)
    patches = patches.reshape([-1, 256, 256])
    
    return patches

def get_zipped_patches(mammogram, roi, step_size, quartile_cutoff = 10, filter_roi=False):
    '''
    Return a zipped generator of the image and the corresponding ROI
    
    Looks at each patch and drops bottom 25% by average value (average of the whole image, black = 0)
    
    '''
    mammogram = get_patches(mammogram, step_size)
    roi = get_patches(roi, step_size)
    
    if filter_roi:
        # On images with more than one ROI, don't repeatedly save the same regions OUTSIDE that ROI. 
        print('Filtering with ROI: ', roi_img)
        patch_means = np.mean(roi, axis = (1,2))
        mask = np.where(patch_means > 0)

        ## filter
        mammogram = mammogram[mask[0],:,:]
        roi = roi[mask[0],:,:]
        
        print('Mammogram/ ROI shape after filtering: ', mammogram.shape)

    else:
        print('Patches array shape before optimization: ', mammogram.shape)

        ## NEW OPTIMIZATION CODE ATTEMPT
        ## Eliminate the bottom quartile_cutoff percent of the image (presumably all black and some of the breast)
        patch_means = np.mean(mammogram, axis = (1,2))
        percentile_cutoff = np.percentile(patch_means, q = quartile_cutoff)

        ## Note mask is GREATER THAN cutoff
        mask = np.where(patch_means > percentile_cutoff)

        ## Apply mask
        mammogram = mammogram[mask[0],:,:]
        roi = roi[mask[0],:,:]
    
    print('Patches array shape after optimization: ', mammogram.shape)
    
    return zip(mammogram, roi) 

###################################################################
# Main function
###################################################################

def save_patches(zipped_patches, label, save_file_name):
    '''
    Main save patches file
    '''
    
    ### Basic logging/ error checking
    errors = []
    num_original = 0
    num_rotate = 0
    num_flip = 0
    num_resize = 0
    num_not_breast = 0
    
    
    ## Recall that zipped_patches = zip(original, roi), where each dim is [-1, 256, 256]
    for number, patch in enumerate(zipped_patches):
        if patch[0].mean() == MASK_CUTOFF: ## If the mean of the image patch = 0, then its purely black and not helpful
            num_not_breast +=1
            continue ## Return to start of loop

        elif patch[1].mean() > 0: ## If this is in the tumor
            if label == 'MALIGNANT':
                save_path = '/home/jlandesman/data/patches/calcification/malignant'

            elif label == 'BENIGN':
                save_path = '/home/jlandesman/data/patches/calcification/benign'

            else:
                save_path = '/home/jlandesman/data/patches/calcification/benign_no_callback'

        else: ## Not in the tumor
            save_path = '/home/jlandesman/data/patches/calcification/no_tumor'
            if np.random.randn() < 0.9: ## Only save 10% to have a balanced dataset. 
                save_path = None

        file_name = save_file_name + "_" + str(number) #+ ".png"
        
        try:
            ###############
            # Save Original
            ###############
            np.save(os.path.join(save_path, file_name), patch[0])
            #cv2.imwrite(os.path.join(save_path, file_name), patch[0], [cv2.IMWRITE_PNG_COMPRESSION, 0])
            num_original += 1

            ##############
            # Rotate
            ##############
            rotation_angle = np.random.randint(low = 0, high = MAX_ROTATE)
            im = rotate_image(patch[0], rotation_angle)

            file_name = save_file_name + "_" + "ROTATE_" + str(number)# + "

            np.save(os.path.join(save_path, file_name), im)
            #cv2.imwrite(os.path.join(save_path, file_name), im, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            num_rotate += 1

            ##############
            # Flip
            ##############
            im = np.fliplr(patch[0])

            file_name = save_file_name + "_" + "FLIP_" + str(number)# + ".png"             
            np.save(os.path.join(save_path, file_name), im)
            #cv2.imwrite(os.path.join(save_path, file_name), im, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            num_flip += 1

#         ##############
#         # Resize
#         ##############
#         resize_min, resize_max = get_resize_max_min(mammogram, 'CALC')

#         dim_0 = np.random.uniform(low = resize_min, high = resize_max)
#         dim_1 = np.random.uniform(low = resize_min, high = resize_max)

#         resize_dims = np.round([dim_0*mammogram.shape[0], dim_1*mammogram.shape[1]])

#         im = (resize(patch[0], resize_dims))

#         if im.mean() < 255:
#             file_name = save_file_name + "_" + "RESIZE_" + str(number) + ".png"             
#             np.save(os.path.join(save_path, file_name), im)
#             #cv2.imwrite(os.path.join(save_path, file_name), im, [cv2.IMWRITE_PNG_COMPRESSION, 0])
#             num_resize += 1
            
        except:
#            errors.append(file_name)
             pass
    print ('Original: {}, Rotate: {}, Flip: {}, Resize: {}, Not Breast: {}'.format(num_original, num_rotate, num_flip, num_resize, num_not_breast))
    print (len(errors))
    
    
##############################
## RUN 
#############################


## Get dictionary of {mammogram_file_name: (ROI_file, label)}
file_list = get_mask_list()

for img_num, mammogram_img in enumerate(sorted(list(file_list.keys()))):
    print("Image num: {}, Image name: {}, Number of ROIS: {} ".format(img_num, mammogram_img, len(file_list[mammogram_img])))

    
    ## Get images as np array
    mammogram = get_im_as_array(mammogram_img, 'full')
        
    for roi_num, roi_img in enumerate(file_list[mammogram_img]):
        
        ## Get ROI
        roi = get_im_as_array(roi_img[0], 'ROI')
        
        ## Get label
        label = roi_img[1]
        data_for_saving = '\n' + 'Image Name: ', mammogram_img, 'ROI_name: ', roi_img, 'label: ', label
        with open('/home/jlandesman/logging_file.csv', 'a') as logging_file:
            logging_file.write(str(data_for_saving))
        
        print('label = ', label)
                        
        if roi_num == 0: ## Run through original image
            zipped_patches = get_zipped_patches(mammogram, roi, step_size = STEP_SIZE,  filter_roi = False)
            save_patches(zipped_patches,label, mammogram_img) 

        
        else: ### Dealing with images that have multiple ROIs - only look at the tumor sections
            zipped_patches = get_zipped_patches(mammogram, roi, step_size = STEP_SIZE, filter_roi = True)
            save_patches(zipped_patches,label, mammogram_img) 
            
                    
        ## Memory Management
        del(zipped_patches) 