#!/usr/bin/env python
# coding: utf-8

from __future__ import absolute_import, division, print_function

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torchvision import transforms
import torchvision.transforms.functional as TF
from models.fpn_global_local_fmreg_ensemble import fpn
from utils.metrics import ConfusionMatrix
from PIL import Image

from scipy.special import softmax
import utils.log as track
from functools import partial
import torch.nn as nn

from dataset.aerial import AerialSubdatasetMode2, AerialSubdatasetMode3a, AerialSubdatasetMode3b

# torch.cuda.synchronize()
# torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True

transformer = transforms.Compose([
    transforms.ToTensor(),
])

def emap(fn, iterable):
    """eager map because I'm lazy and don't want to type."""
    return list(map(fn, iterable))

def resize(images, shape, label=False):
    '''
    resize PIL images
    shape: (w, h)
    '''
    resize_fn = partial(TF.resize, size=shape, interpolation=Image.NEAREST if label else Image.BILINEAR)
    return emap(resize_fn, images)

def masks_transform(masks, numpy=False):
    '''
    masks: list of PIL images
    '''
    targets = np.array([np.array(m).astype('int32') for m in masks], dtype=np.int32)
    if numpy:
        return targets
    return torch.from_numpy(targets).long().cuda()

def images_transform(images):
    '''
    images: list of PIL images
    '''
    
    inputs = [transformer(img) for img in images]
    inputs = torch.stack(inputs, dim=0).cuda()
    return inputs

def get_patch_info(shape, p_size):
    '''
    shape: origin image size, (x, y)
    p_size: patch size (square)
    return: n_x, n_y, step_x, step_y
    '''
    x = shape[0]
    y = shape[1]
    n = m = 1
    while x > n * p_size:
        n += 1
    while p_size - 1.0 * (x - p_size) / (n - 1) < 50:
        n += 1
    while y > m * p_size:
        m += 1
    while p_size - 1.0 * (y - p_size) / (m - 1) < 50:
        m += 1
    return n, m, (x - p_size) * 1.0 / (n - 1), (y - p_size) * 1.0 / (m - 1)

def global2patch(images, p_size):
    '''
    image/label => patches
    p_size: patch size
    return: list of PIL patch images; coordinates: images->patches; ratios: (h, w)
    '''
    patches = []; coordinates = []; templates = []; sizes = []; ratios = [(0, 0)] * len(images); patch_ones = np.ones(p_size)
    for i in range(len(images)):
        w, h = images[i].size
        size = (h, w)
        sizes.append(size)
        ratios[i] = (float(p_size[0]) / size[0], float(p_size[1]) / size[1])
        template = np.zeros(size)
        n_x, n_y, step_x, step_y = get_patch_info(size, p_size[0])
        patches.append([images[i]] * (n_x * n_y))
        coordinates.append([(0, 0)] * (n_x * n_y))
        for x in range(n_x):
            if x < n_x - 1: top = int(np.round(x * step_x))
            else: top = size[0] - p_size[0]
            for y in range(n_y):
                if y < n_y - 1: left = int(np.round(y * step_y))
                else: left = size[1] - p_size[1]
                template[top:top+p_size[0], left:left+p_size[1]] += patch_ones
                coordinates[i][x * n_y + y] = (1.0 * top / size[0], 1.0 * left / size[1])
                patches[i][x * n_y + y] = transforms.functional.crop(images[i], top, left, p_size[0], p_size[1])
        templates.append(Variable(torch.Tensor(template).expand(1, 1, -1, -1)))
    return patches, coordinates, templates, sizes, ratios

def patch2global(patches, n_class, sizes, coordinates, p_size):
    '''
    predicted patches (after classify layer) => predictions
    return: list of np.array
    '''
    predictions = [ np.zeros((n_class, size[0], size[1])) for size in sizes ]
    for i in range(len(sizes)):
        for j in range(len(coordinates[i])):
            top, left = coordinates[i][j]
            top = int(np.round(top * sizes[i][0])); left = int(np.round(left * sizes[i][1]))
            predictions[i][:, top: top + p_size[0], left: left + p_size[1]] += patches[i][j]
    return predictions

def template_patch2global(size_g, size_p, n, step):
    template = np.zeros(size_g)
    coordinates = [(0, 0)] * n ** 2
    patch = np.ones(size_p)
    step = (size_g[0] - size_p[0]) // (n - 1)
    x = y = 0
    i = 0
    while x + size_p[0] <= size_g[0]:
        while y + size_p[1] <= size_g[1]:
            template[x:x+size_p[0], y:y+size_p[1]] += patch
            coordinates[i] = (1.0 * x / size_g[0], 1.0 * y / size_g[1])
            i += 1
            y += step
        x += step
        y = 0
    return Variable(torch.Tensor(template).expand(1, 1, -1, -1)).cuda(), coordinates

def one_hot_gaussian_blur(index, classes):
    '''
    index: numpy array b, h, w
    classes: int
    '''
    mask = np.transpose((np.arange(classes) == index[..., None]).astype(float), (0, 3, 1, 2))
    b, c, _, _ = mask.shape
    for i in range(b):
        for j in range(c):
            mask[i][j] = cv2.GaussianBlur(mask[i][j], (0, 0), 8)

    return mask

def collate_mode3b(batch):
    import random
    timeid = random.randint(0,100)
    track.start("collate_"+str(timeid))
    label_patches = []
    fl = []
    fg = []
    ratios = []
    coords = []
    ids = []
    for b in batch:
        bid = b['id']
        _id = 0
        if bid in ids:
            _id = ids.index(bid)
        else:
            ids.append(bid)
            _id = len(ids) - 1
            label_patches.append([])
            fl.append([])
            coords.append([])
            ratios.append(b['ratio'])
        label_patches[_id].append(b['label'])
        fl[_id].append(b['fl'])
        coords[_id].append(b['coord'])
    label_patches = [torch.stack(i, dim=0) for i in label_patches]
    fl = [torch.stack(i, dim=0) for i in fl]
    track.end("collate_"+str(timeid))
    return {
        'id': ids,
        'label_patches': label_patches,
        'fl': fl,
        'ratios': ratios,
        'coords': coords
    }

def collate_mode3a(batch):
    patches = []
    images_glb = []
    ratios = []
    coords = []
    templates = []
    coord_ids = []
    ids = []
    for b in batch:
        bid = b['id']
        _id = 0
        if bid in ids:
            _id = ids.index(bid)
        else:
            ids.append(bid)
            _id = len(ids) - 1
            patches.append([])
            images_glb.append(b['image_glob'])
            ratios.append(b['ratio'])
            coords.append(b['coord'])
            templates.append(b['template'])
            coord_ids.append([])
        patches[_id].append(b['patch'])
        coord_ids[_id].append(b['coord_id'])
    patches = [torch.stack(i, dim=0) for i in patches]
    coord_ids = [x for x in coord_ids]
    return {
        'patches': patches,
        'images_glb': images_glb,
        'ratios': ratios,
        'coords': coords,
        'templates': templates,
        'coord_ids': coord_ids
    }

def collate_mode2(batch):
    patches = []
    labels = []
    coords = []
    n_patches = []
    ratios = []
    images_glob = []
    ids = []
    for b in batch:
        bid = b['id']
        _id = 0
        if bid in ids:
            _id = ids.index(bid)
        else:
            ids.append(bid)
            _id = len(ids) - 1
            patches.append([])
            labels.append([])
            coords.append([])
            n_patches.append(b['n_patch'])
            ratios.append(b['ratio'])
            images_glob.append(b['image_glob'])
        patches[_id].append(b['patch'])
        labels[_id].append(b['label'])
        coords[_id].append(b['coord'])
    patches = [torch.stack(i, dim=0) for i in patches]
    labels = [torch.stack(i, dim=0) for i in labels]
    return {'patches': patches, \
        'labels': labels, \
        'images_glob': images_glob, \
        'ratio': ratios, \
        'n_patch': n_patches, \
        'coords': coords}

def collate(batch):
    image = [ b['image'] for b in batch ] # w, h
    label = [ b['label'] for b in batch ]
    id = [ b['id'] for b in batch ]
    label_npy = np.stack([ b['label_npy'] for b in batch ])
    image_glb = torch.stack([ b['image_glb'] for b in batch ], dim=0)
    return {'image': image, 'label': label, 'id': id, 'label_npy': label_npy, 'image_glb': image_glb}

def collate_test(batch):
    image = [ b['image'] for b in batch ] # w, h
    id = [ b['id'] for b in batch ]
    return {'image': image, 'id': id}


def create_model_load_weights(n_class, mode=1, evaluation=False, path_g=None, path_g2l=None, path_l2g=None):
    model = fpn(n_class)
    model = nn.DataParallel(model)
    model = model.cuda()

    if (mode == 2 and not evaluation) or (mode == 1 and evaluation):
        # load fixed basic global branch
        partial = torch.load(path_g)
        state = model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in partial.items() if k in state and "local" not in k}
        # 2. overwrite entries in the existing state dict
        state.update(pretrained_dict)
        # 3. load the new state dict
        model.load_state_dict(state)

    if (mode == 3 and not evaluation) or (mode == 2 and evaluation):
        partial = torch.load(path_g2l)
        state = model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in partial.items() if k in state}# and "global" not in k}
        # 2. overwrite entries in the existing state dict
        state.update(pretrained_dict)
        # 3. load the new state dict
        model.load_state_dict(state)

    global_fixed = None
    if mode == 3:
        # load fixed basic global branch
        global_fixed = fpn(n_class)
        global_fixed = nn.DataParallel(global_fixed)
        global_fixed = global_fixed.cuda()
        partial = torch.load(path_g)
        state = global_fixed.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in partial.items() if k in state and "local" not in k}
        # 2. overwrite entries in the existing state dict
        state.update(pretrained_dict)
        # 3. load the new state dict
        global_fixed.load_state_dict(state)
        global_fixed.eval()

    if mode == 3 and evaluation:
        partial = torch.load(path_l2g)
        state = model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in partial.items() if k in state}# and "global" not in k}
        # 2. overwrite entries in the existing state dict
        state.update(pretrained_dict)
        # 3. load the new state dict
        model.load_state_dict(state)

    if mode == 1 or mode == 3:
        model.module.resnet_local.eval()
        model.module.fpn_local.eval()
    else:
        model.module.resnet_global.eval()
        model.module.fpn_global.eval()
    
    return model, global_fixed


def get_optimizer(model, mode=1, learning_rate=2e-5):
    if mode == 1 or mode == 3:
        # train global
        optimizer = torch.optim.Adam([
                {'params': model.module.resnet_global.parameters(), 'lr': learning_rate},
                {'params': model.module.resnet_local.parameters(), 'lr': 0},
                {'params': model.module.fpn_global.parameters(), 'lr': learning_rate},
                {'params': model.module.fpn_local.parameters(), 'lr': 0},
                {'params': model.module.ensemble_conv.parameters(), 'lr': learning_rate},
            ], weight_decay=5e-4)
    else:
        # train local
        optimizer = torch.optim.Adam([
                {'params': model.module.resnet_global.parameters(), 'lr': 0},
                {'params': model.module.resnet_local.parameters(), 'lr': learning_rate},
                {'params': model.module.fpn_global.parameters(), 'lr': 0},
                {'params': model.module.fpn_local.parameters(), 'lr': learning_rate},
                {'params': model.module.ensemble_conv.parameters(), 'lr': learning_rate},
            ], weight_decay=5e-4)
    return optimizer

class Trainer(object):
    def __init__(self, criterion, optimizer, n_class, size_g, size_p, sub_batch_size=6, mode=1, lamb_fmreg=0.15):
        self.criterion = criterion
        self.optimizer = optimizer
        self.metrics_global = ConfusionMatrix(n_class)
        self.metrics_local = ConfusionMatrix(n_class)
        self.metrics = ConfusionMatrix(n_class)
        self.n_class = n_class
        self.size_g = size_g
        self.size_p = size_p
        self.sub_batch_size = sub_batch_size
        self.mode = mode
        self.lamb_fmreg = lamb_fmreg
    
    def set_train(self, model):
        model.module.ensemble_conv.train()
        if self.mode == 1 or self.mode == 3:
            model.module.resnet_global.train()
            model.module.fpn_global.train()
        else:
            model.module.resnet_local.train()
            model.module.fpn_local.train()

    def get_scores(self):
        score_train = self.metrics.get_scores()
        score_train_local = self.metrics_local.get_scores()
        score_train_global = self.metrics_global.get_scores()
        return score_train, score_train_global, score_train_local

    def reset_metrics(self):
        self.metrics.reset()
        self.metrics_local.reset()
        self.metrics_global.reset()

    def train(self, sample, model, global_fixed):
        images, labels, labels_npy, images_glb = sample['image'], sample['label'], sample['label_npy'], sample['image_glb'] # PIL images
        #labels_npy = masks_transform(labels, numpy=True) # label of origin size in numpy
        #images_glb = resize(images, self.size_g) # list of resized PIL images
        #images_glb = images_transform(images_glb)
        labels_glb = resize(labels, (self.size_g[0] // 4, self.size_g[1] // 4), label=True) # FPN down 1/4, for loss
        labels_glb = masks_transform(labels_glb)
        if self.mode == 2 or self.mode == 3:
            patches, coordinates, templates, sizes, ratios = global2patch(images, self.size_p)
            label_patches, _, _, _, _ = global2patch(labels, self.size_p)
            #predicted_patches = [ np.zeros((len(coordinates[i]), self.n_class, self.size_p[0], self.size_p[1])) for i in range(len(images)) ]
            #predicted_ensembles = [ np.zeros((len(coordinates[i]), self.n_class, self.size_p[0], self.size_p[1])) for i in range(len(images)) ]
            #outputs_global = [ None for i in range(len(images)) ]

        if self.mode == 1:
            # training with only (resized) global image #########################################
            outputs_global, _ = model.forward(images_glb, None, None, None)
            loss = self.criterion(outputs_global, labels_glb)
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()
            ##############################################

        if self.mode == 2:
            
            # training with patches ###########################################
            subdataset = AerialSubdatasetMode2(images_glb, ratios, coordinates, patches, label_patches, (self.size_p[0] // 4, self.size_p[1] // 4))
            data_loader = torch.utils.data.DataLoader(dataset=subdataset, \
                                                    batch_size=self.sub_batch_size, \
                                                    num_workers=20, \
                                                    collate_fn=collate_mode2, \
                                                    shuffle=False, pin_memory=True)
            for batch_sample in data_loader:
                for sub_batch_id in range(len(batch_sample['n_patch'])):
                    patches_var = batch_sample['patches'][sub_batch_id].cuda()
                    label_patches_var = batch_sample['labels'][sub_batch_id].cuda()
                    output_ensembles, output_global, output_patches, fmreg_l2 = model.forward(batch_sample['images_glob'][sub_batch_id].cuda(), \
                                                                                            patches_var, \
                                                                                            batch_sample['coords'][sub_batch_id], \
                                                                                            batch_sample['ratio'][sub_batch_id], mode=self.mode, \
                                                                                            n_patch=batch_sample['n_patch'][sub_batch_id])

                    loss = self.criterion(output_patches, label_patches_var) + self.criterion(output_ensembles, label_patches_var) + self.lamb_fmreg * fmreg_l2
                    loss.backward()
            
            ''' 
            for i in range(len(images)):
                j = 0
                print("LEN", len(coordinates[i]))
                while j < len(coordinates[i]):
                    track.start("transform_internal")
                    patches_var = images_transform(patches[i][j : j+self.sub_batch_size]) # b, c, h, w
                    label_patches_var = masks_transform(resize(label_patches[i][j : j+self.sub_batch_size], (self.size_p[0] // 4, self.size_p[1] // 4), label=True)) # down 1/4 for loss
                    track.end("transform_internal")
                    
                    track.start("ff_internal")
                    output_ensembles, output_global, output_patches, fmreg_l2 = model.forward(images_glb[i:i+1], patches_var, coordinates[i][j : j+self.sub_batch_size], ratios[i], mode=self.mode, n_patch=len(coordinates[i]))
                    track.end("ff_internal")
                    loss = self.criterion(output_patches, label_patches_var) + self.criterion(output_ensembles, label_patches_var) + self.lamb_fmreg * fmreg_l2
                    loss.backward()

                    # patch predictions
                    #predicted_patches[i][j:j+output_patches.size()[0]] = F.interpolate(output_patches, size=self.size_p, mode='nearest').data.cpu().numpy()
                    #predicted_ensembles[i][j:j+output_ensembles.size()[0]] = F.interpolate(output_ensembles, size=self.size_p, mode='nearest').data.cpu().numpy()
                    j += self.sub_batch_size
                #outputs_global[i] = output_global
            #outputs_global = torch.cat(outputs_global, dim=0)
            '''
            self.optimizer.step()
            self.optimizer.zero_grad()
            #####################################################################################

        if self.mode == 3:
            # train global with help from patches ##################################################
            # go through local patches to collect feature maps
            # collect predictions from patches
            
            track.start("Collect patches")
            # import pdb; pdb.set_trace();
            subdataset = AerialSubdatasetMode3a(patches, coordinates, images_glb, ratios, templates)
            data_loader = torch.utils.data.DataLoader(dataset=subdataset, \
                                                    batch_size=self.sub_batch_size, \
                                                    num_workers=20, \
                                                    collate_fn=collate_mode3a, \
                                                    shuffle=False, pin_memory=True)
            for batch_sample in data_loader:
                for sub_batch_id in range(len(batch_sample['ratios'])):
                    patches_var = batch_sample['patches'][sub_batch_id].cuda()
                    coord = batch_sample['coords'][sub_batch_id]
                    j = batch_sample['coord_ids'][sub_batch_id]
                    fm_patches, _ = model.module.collect_local_fm(batch_sample['images_glb'][sub_batch_id].cuda(), \
                                                                  patches_var, \
                                                                  batch_sample['ratios'][sub_batch_id], \
                                                                  coord, \
                                                                  [min(j), max(j) + 1], \
                                                                  len(images), \
                                                                  global_model=global_fixed, \
                                                                  template= batch_sample['templates'][sub_batch_id].cuda(), \
                                                                  n_patch_all=len(coord))
            # for i in range(len(images)):
            #     j = 0
            #     while j < len(coordinates[i]):
            #         patches_var = images_transform(patches[i][j : j+self.sub_batch_size]) # b, c, h, w
            #         fm_patches, _ = model.module.collect_local_fm(images_glb[i:i+1], patches_var, ratios[i], coordinates[i], [j, j+self.sub_batch_size], len(images), global_model=global_fixed, template=templates[i], n_patch_all=len(coordinates[i]))
            #         j += self.sub_batch_size
            
            track.end("Collect patches")
            
            images_glb = images_glb.cuda()
            # train on global image
            outputs_global, fm_global = model.forward(images_glb, None, None, None, mode=self.mode)
            loss = self.criterion(outputs_global, labels_glb)
            loss.backward(retain_graph=True)
            
            subdataset = AerialSubdatasetMode3b(label_patches, \
                                                (self.size_p[0] // 4, self.size_p[1] // 4), \
                                                fm_patches,\
                                                coordinates, ratios)
            data_loader = torch.utils.data.DataLoader(dataset=subdataset, \
                                                    batch_size=self.sub_batch_size, \
                                                    num_workers=20, \
                                                    collate_fn=collate_mode3b, \
                                                    shuffle=False, pin_memory=True)
            track.start("load_mode_3b")
            for batch_idx, batch_sample in enumerate(data_loader):
                for sub_batch_id in range(len(batch_sample['ratios'])):
                    label_patches_var = batch_sample['label_patches'][sub_batch_id].cuda()
                    fl = batch_sample['fl'][sub_batch_id].cuda()
                    image_id = batch_sample['id'][sub_batch_id]
                    track.end("load_mode_3b")
                    fg = model.module._crop_global(fm_global[image_id: image_id+1], \
                                                   batch_sample['coords'][sub_batch_id], \
                                                   batch_sample['ratios'][sub_batch_id])[0]
                    fg = F.interpolate(fg, size=fl.size()[2:], mode='bilinear')
                    output_ensembles = model.module.ensemble(fl, fg)
                    loss = self.criterion(output_ensembles, label_patches_var)# + 0.15 * mse(fl, fg)
                    if batch_idx == len(data_loader) - 1 and sub_batch_id == len(batch_sample['ratios']) - 1:
                        loss.backward()
                    else:
                        loss.backward(retain_graph=True)
                    track.start("load_mode_3b")
            # fmreg loss
            # generate ensembles & calc loss
            """
            track.start("load_mode_3b")
            for i in range(len(images)):
                j = 0
                while j < len(coordinates[i]):
                    label_patches_var = masks_transform(resize(label_patches[i][j : j+self.sub_batch_size], (self.size_p[0] // 4, self.size_p[1] // 4), label=True))
                    fl = fm_patches[i][j : j+self.sub_batch_size].cuda()
                    track.end("load_mode_3b")
                    fg = model.module._crop_global(fm_global[i:i+1], coordinates[i][j:j+self.sub_batch_size], ratios[i])[0]
                    fg = F.interpolate(fg, size=fl.size()[2:], mode='bilinear')
                    output_ensembles = model.module.ensemble(fl, fg)
                    loss = self.criterion(output_ensembles, label_patches_var)# + 0.15 * mse(fl, fg)
                    if i == len(images) - 1 and j + self.sub_batch_size >= len(coordinates[i]):
                        loss.backward()
                    else:
                        loss.backward(retain_graph=True)
                    track.start("load_mode_3b")
                    # ensemble predictions
                    #predicted_ensembles[i][j:j+output_ensembles.size()[0]] = F.interpolate(output_ensembles, size=self.size_p, mode='nearest').data.cpu().numpy()
                    j += self.sub_batch_size
            """
            self.optimizer.step()
            self.optimizer.zero_grad()
        '''
        # global predictions ###########################
        outputs_global = outputs_global.cpu()
        predictions_global = [F.interpolate(outputs_global[i:i+1], images[i].size[::-1], mode='nearest').argmax(1).detach().numpy() for i in range(len(images))]
        self.metrics_global.update(labels_npy, predictions_global)
        
        if self.mode == 2 or self.mode == 3:
            # patch predictions ###########################
            scores_local = np.array(patch2global(predicted_patches, self.n_class, sizes, coordinates, self.size_p)) # merge softmax scores from patches (overlaps)
            predictions_local = scores_local.argmax(1) # b, h, w
            self.metrics_local.update(labels_npy, predictions_local)
            ###################################################
            # combined/ensemble predictions ###########################
            scores = np.array(patch2global(predicted_ensembles, self.n_class, sizes, coordinates, self.size_p)) # merge softmax scores from patches (overlaps)
            predictions = scores.argmax(1) # b, h, w
            self.metrics.update(labels_npy, predictions)
        '''
        return loss


class Evaluator(object):
    def __init__(self, n_class, size_g, size_p, sub_batch_size=6, mode=1, test=False):
        self.metrics_global = ConfusionMatrix(n_class)
        self.metrics_local = ConfusionMatrix(n_class)
        self.metrics = ConfusionMatrix(n_class)
        self.n_class = n_class
        self.size_g = size_g
        self.size_p = size_p
        self.sub_batch_size = sub_batch_size
        self.mode = mode
        self.test = test

        if test:
            self.flip_range = [False, True]
            self.rotate_range = [0, 1, 2, 3]
        else:
            self.flip_range = [False]
            self.rotate_range = [0]
    
    def get_scores(self):
        score_train = self.metrics.get_scores()
        score_train_local = self.metrics_local.get_scores()
        score_train_global = self.metrics_global.get_scores()
        return score_train, score_train_global, score_train_local

    def reset_metrics(self):
        self.metrics.reset()
        self.metrics_local.reset()
        self.metrics_global.reset()

    def eval_test(self, sample, model, global_fixed):
        with torch.no_grad():
            images = sample['image']
            if not self.test:
                labels = sample['label'] # PIL images
                labels_npy = sample['label_npy'] #masks_transform(labels, numpy=True)

            images_global = resize(images, self.size_g)
            outputs_global = np.zeros((len(images), self.n_class, self.size_g[0] // 4, self.size_g[1] // 4))
            if self.mode == 2 or self.mode == 3:
                images_local = [ image.copy() for image in images ]
                scores_local = [ np.zeros((1, self.n_class, images[i].size[1], images[i].size[0])) for i in range(len(images)) ]
                scores = [ np.zeros((1, self.n_class, images[i].size[1], images[i].size[0])) for i in range(len(images)) ]

            for flip in self.flip_range:
                if flip:
                    # we already rotated images for 270'
                    for b in range(len(images)):
                        images_global[b] = transforms.functional.rotate(images_global[b], 90) # rotate back!
                        images_global[b] = transforms.functional.hflip(images_global[b])
                        if self.mode == 2 or self.mode == 3:
                            images_local[b] = transforms.functional.rotate(images_local[b], 90) # rotate back!
                            images_local[b] = transforms.functional.hflip(images_local[b])
                for angle in self.rotate_range:
                    if angle > 0:
                        for b in range(len(images)):
                            images_global[b] = transforms.functional.rotate(images_global[b], 90)
                            if self.mode == 2 or self.mode == 3:
                                images_local[b] = transforms.functional.rotate(images_local[b], 90)

                    # prepare global images onto cuda
                    images_glb = images_transform(images_global) # b, c, h, w
                    images_glb = images_glb.cuda()
                    if self.mode == 2 or self.mode == 3:
                        patches, coordinates, templates, sizes, ratios = global2patch(images, self.size_p)
                        predicted_patches = [ np.zeros((len(coordinates[i]), self.n_class, self.size_p[0], self.size_p[1])) for i in range(len(images)) ]
                        predicted_ensembles = [ np.zeros((len(coordinates[i]), self.n_class, self.size_p[0], self.size_p[1])) for i in range(len(images)) ]

                    if self.mode == 1:
                        # eval with only resized global image ##########################
                        if flip:
                            outputs_global += np.flip(np.rot90(model.forward(images_glb, None, None, None)[0].data.cpu().numpy(), k=angle, axes=(3, 2)), axis=3)
                        else:
                            outputs_global += np.rot90(model.forward(images_glb, None, None, None)[0].data.cpu().numpy(), k=angle, axes=(3, 2))
                        ################################################################

                    if self.mode == 2:
                        # eval with patches ###########################################
                        for i in range(len(images)):
                            j = 0
                            while j < len(coordinates[i]):
                                patches_var = images_transform(patches[i][j : j+self.sub_batch_size]) # b, c, h, w
                                output_ensembles, output_global, output_patches, _ = model.forward(images_glb[i:i+1], patches_var, coordinates[i][j : j+self.sub_batch_size], ratios[i], mode=self.mode, n_patch=len(coordinates[i]))

                                # patch predictions
                                predicted_patches[i][j:j+output_patches.size()[0]] += F.interpolate(output_patches, size=self.size_p, mode='nearest').data.cpu().numpy()
                                predicted_ensembles[i][j:j+output_ensembles.size()[0]] += F.interpolate(output_ensembles, size=self.size_p, mode='nearest').data.cpu().numpy()
                                j += patches_var.size()[0]
                            if flip:
                                outputs_global[i] += np.flip(np.rot90(output_global[0].data.cpu().numpy(), k=angle, axes=(2, 1)), axis=2)
                                scores_local[i] += np.flip(np.rot90(np.array(patch2global(predicted_patches[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)), axis=3) # merge softmax scores from patches (overlaps)
                                scores[i] += np.flip(np.rot90(np.array(patch2global(predicted_ensembles[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)), axis=3) # merge softmax scores from patches (overlaps)
                            else:
                                outputs_global[i] += np.rot90(output_global[0].data.cpu().numpy(), k=angle, axes=(2, 1))
                                scores_local[i] += np.rot90(np.array(patch2global(predicted_patches[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)) # merge softmax scores from patches (overlaps)
                                scores[i] += np.rot90(np.array(patch2global(predicted_ensembles[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)) # merge softmax scores from patches (overlaps)
                        ###############################################################

                    if self.mode == 3:
                        # eval global with help from patches ##################################################
                        # go through local patches to collect feature maps
                        # collect predictions from patches
                        for i in range(len(images)):
                            j = 0
                            while j < len(coordinates[i]):
                                patches_var = images_transform(patches[i][j : j+self.sub_batch_size]) # b, c, h, w
                                #import pdb; pdb.set_trace()
                                fm_patches, output_patches = model.module.collect_local_fm(images_glb[i:i+1], patches_var, ratios[i], coordinates[i], [j, j+self.sub_batch_size], len(images), global_model=global_fixed, template=templates[i].cuda(), n_patch_all=len(coordinates[i]))
                                predicted_patches[i][j:j+output_patches.size()[0]] += F.interpolate(output_patches, size=self.size_p, mode='nearest').data.cpu().numpy()
                                j += self.sub_batch_size
                        # go through global image
                        tmp, fm_global = model.forward(images_glb, None, None, None, mode=self.mode)
                        if flip:
                            outputs_global += np.flip(np.rot90(tmp.data.cpu().numpy(), k=angle, axes=(3, 2)), axis=3)
                        else:
                            outputs_global += np.rot90(tmp.data.cpu().numpy(), k=angle, axes=(3, 2))
                        # generate ensembles
                        for i in range(len(images)):
                            j = 0
                            while j < len(coordinates[i]):
                                fl = fm_patches[i][j : j+self.sub_batch_size].cuda()
                                fg = model.module._crop_global(fm_global[i:i+1], coordinates[i][j:j+self.sub_batch_size], ratios[i])[0]
                                fg = F.interpolate(fg, size=fl.size()[2:], mode='bilinear')
                                output_ensembles = model.module.ensemble(fl, fg) # include cordinates

                                # ensemble predictions
                                predicted_ensembles[i][j:j+output_ensembles.size()[0]] += F.interpolate(output_ensembles, size=self.size_p, mode='nearest').data.cpu().numpy()
                                j += self.sub_batch_size
                            if flip:
                                scores_local[i] += np.flip(np.rot90(np.array(patch2global(predicted_patches[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)), axis=3)[0] # merge softmax scores from patches (overlaps)
                                scores[i] += np.flip(np.rot90(np.array(patch2global(predicted_ensembles[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)), axis=3)[0] # merge softmax scores from patches (overlaps)
                            else:
                                scores_local[i] += np.rot90(np.array(patch2global(predicted_patches[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)) # merge softmax scores from patches (overlaps)
                                scores[i] += np.rot90(np.array(patch2global(predicted_ensembles[i:i+1], self.n_class, sizes[i:i+1], coordinates[i:i+1], self.size_p)), k=angle, axes=(3, 2)) # merge softmax scores from patches (overlaps)
                        ###################################################

            # global predictions ###########################
            outputs_global = torch.Tensor(outputs_global)
            predictions_global = [F.interpolate(outputs_global[i:i+1], images[i].size[::-1], mode='nearest').argmax(1).detach().numpy()[0] for i in range(len(images))]
            if not self.test:
                self.metrics_global.update(labels_npy, predictions_global)

            if self.mode == 2 or self.mode == 3:
                # patch predictions ###########################
                if self.test:
                    predictions_local = [softmax(score.astype(np.float32), axis=1)[0,1, :, :] for score in scores_local ]
                else:    
                    predictions_local = [ score.argmax(1)[0] for score in scores_local ]
                if not self.test:
                    self.metrics_local.update(labels_npy, predictions_local)
                ###################################################
                # combined/ensemble predictions ###########################
                if self.test:
                    predictions = [ softmax(score.astype(np.float32), axis=1)[0,1, :, :] for score in scores ]
                    #import pdb; pdb.set_trace()
                else:
                    predictions = [ score.argmax(1)[0] for score in scores ]
                if not self.test:
                    self.metrics.update(labels_npy, predictions)
                return predictions, predictions_global, predictions_local
            else:
                return None, predictions_global, None
