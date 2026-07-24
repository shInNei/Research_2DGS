from PIL import Image
import os
import torch
import numpy as np

import utils.general as utils
from utils import rend_util
import json
import imageio


class SynDataset(torch.utils.data.Dataset):
    def __init__(self,
                 instance_dir,
                 frame_skip,
                 split='train'
                 ):
        self.instance_dir = instance_dir
        print('Creating dataset from: ', self.instance_dir)
        assert os.path.exists(self.instance_dir), "Data directory is empty"

        self.split = split

        json_path = os.path.join(self.instance_dir, 'transforms_{}.json'.format(split))
        print('Read cam from {}'.format(json_path))
        with open(json_path, 'r') as fp:
            meta = json.load(fp)
        
        image_paths = []
        mask_paths = []
        poses = []
        envmap6_image_paths = []
        envmap12_image_paths = []
        for frame in meta['frames']:
            poses.append(np.array(frame['transform_matrix']))
            if split == 'train':
                image_paths.append(os.path.join(self.instance_dir, frame['file_path'] + '_rgb.exr'))
                mask_paths.append(os.path.join(self.instance_dir, frame['file_path'] + '_mask.png'))
            if split == 'test':
                ind = frame['file_path'].split('/')[1]
                image_paths.append(os.path.join(self.instance_dir, frame['file_path'] + '_rgba.png'))
                envmap6_image_paths.append(os.path.join(self.instance_dir, 'test_rli/envmap6_'+ ind + '.png'))
                envmap12_image_paths.append(os.path.join(self.instance_dir, 'test_rli/envmap12_'+ ind + '.png'))
        
        img_h, img_w = rend_util.load_rgb(image_paths[0]).shape[:2]
        camera_angle_x = float(meta['camera_angle_x'])
        focal = .5 * img_w / np.tan(.5 * camera_angle_x)
        poses = np.array(poses)
        print("focal {}, img_w {}, img_h {}".format(focal, img_w, img_h))
        scale = 2.0
        print("Scale {}".format(scale))
        poses[..., 3] /= scale

        # skip for training
        image_paths = image_paths[::frame_skip]
        poses = poses[::frame_skip, ...]
        print('Training image: {}'.format(len(image_paths)))
        self.n_cameras = len(image_paths)
        self.image_paths = image_paths

        self.single_imgname = None
        self.single_imgname_idx = None
        self.sampling_idx = None

        self.intrinsics_all = []
        self.pose_all = []
        intrinsics = [[focal, 0, img_w / 2],[0, focal, img_h / 2], [0, 0, 1]]
        intrinsics = np.array(intrinsics).astype(np.float32)
        for i in range(self.n_cameras):
            self.intrinsics_all.append(torch.from_numpy(intrinsics).float())
            self.pose_all.append(torch.from_numpy(poses[i]).float())

        self.rgb_images = []
        self.object_masks = []

        H, W = rend_util.load_rgb(image_paths[0]).shape[:2]
        self.img_res = [H, W]
        self.total_pixels = self.img_res[0] * self.img_res[1]

        # read training images
        for path in image_paths:
            rgb = rend_util.load_rgb(path).reshape(-1, 3)
            self.rgb_images.append(torch.from_numpy(rgb).float())
            self.has_groundtruth = True

        # read mask images
        if self.split == 'train':
            mask_paths = mask_paths[::frame_skip]
            for path in mask_paths:
                print('Loaded mask: ', path)
                object_mask = rend_util.load_mask(path)
                object_mask = object_mask.reshape(-1)
                self.object_masks.append(torch.from_numpy(object_mask).bool())

        # read relight image only for test
        if self.split == 'test':
            self.envmap6_images = []
            self.envmap12_images = []
            envmap6_image_paths = envmap6_image_paths[::frame_skip]
            envmap12_image_paths = envmap12_image_paths[::frame_skip]
            for path in image_paths:
                object_mask = imageio.imread(path)[:, :, 3] 
                object_mask = object_mask > 128
                self.object_masks.append(torch.from_numpy(object_mask.reshape(-1)).bool())
            for path in envmap6_image_paths:
                rgb = rend_util.load_rgb(path).reshape(-1, 3)
                self.envmap6_images.append(torch.from_numpy(rgb).float())
            for path in envmap12_image_paths:
                rgb = rend_util.load_rgb(path).reshape(-1, 3)
                self.envmap12_images.append(torch.from_numpy(rgb).float())

    def __len__(self):
        return (self.n_cameras)

    def __getitem__(self, idx):
        if self.single_imgname_idx is not None:
            idx = self.single_imgname_idx
        
        uv = np.mgrid[0:self.img_res[0], 0:self.img_res[1]].astype(np.int32)
        uv = torch.from_numpy(np.flip(uv, axis=0).copy()).float()
        uv = uv.reshape(2, -1).transpose(1, 0)

        sample = {
            "uv": uv,
            "intrinsics": self.intrinsics_all[idx],
            "pose": self.pose_all[idx],
            "object_mask": self.object_masks[idx],
        }
        ground_truth = {
            "rgb": self.rgb_images[idx]
        }

        if self.split == 'test':
            ground_truth["envmap6_rgb"] = self.envmap6_images[idx]
            ground_truth["envmap12_rgb"] = self.envmap12_images[idx]

        if self.sampling_idx is not None:
            ground_truth["rgb"] = self.rgb_images[idx][self.sampling_idx, :]
            sample["object_mask"] = self.object_masks[idx][self.sampling_idx]
            sample["uv"] = uv[self.sampling_idx, :]

        return idx, sample, ground_truth

    def collate_fn(self, batch_list):
        # get list of dictionaries and returns input, 
        # ground_true as dictionary for all batch instances
        batch_list = zip(*batch_list)

        all_parsed = []
        for entry in batch_list:
            if type(entry[0]) is dict:
                # make them all into a new dict
                ret = {}
                for k in entry[0].keys():
                    ret[k] = torch.stack([obj[k] for obj in entry])
                all_parsed.append(ret)
            else:
                all_parsed.append(torch.LongTensor(entry))

        return tuple(all_parsed)

    def change_sampling_idx(self, sampling_size):
        if sampling_size == -1:
            self.sampling_idx = None
        else:
            self.sampling_idx = torch.randperm(self.total_pixels)[:sampling_size]




# =========================================================
from PIL import Image
import json

# =========================================================
# Custom TensoIRDataset for TensoIR Lego Dataset
# =========================================================
class TensoIRDataset(torch.utils.data.Dataset):
    def __init__(self, instance_dir, frame_skip=1, split='train'):
        self.instance_dir = instance_dir
        self.split = split
        print(f'Creating TensoIRDataset from: {self.instance_dir} (split: {split})')
        
        target_dir = os.path.join(self.instance_dir, split)
        if not os.path.exists(target_dir):
            target_dir = self.instance_dir
            
        subdirs = sorted([d for d in os.listdir(target_dir) if os.path.isdir(os.path.join(target_dir, d))])
        subdirs = subdirs[::frame_skip]
        
        self.n_cameras = len(subdirs)
        self.rgb_images = []
        self.object_masks = []
        self.intrinsics_all = []
        self.pose_all = []
        self.image_paths = []
        self.sampling_idx = None
        scale = 2.0
        
        for d in subdirs:
            d_path = os.path.join(target_dir, d)
            jsons = [f for f in os.listdir(d_path) if f.endswith('.json')]
            if not jsons:
                continue
            with open(os.path.join(d_path, jsons[0]), 'r') as f:
                meta = json.load(f)
                
            img_path = None
            for cand in ['rgba.png', 'rgb.png', 'image.png', f'{d}.png']:
                p = os.path.join(d_path, cand)
                if os.path.exists(p):
                    img_path = p
                    break
            if img_path is None:
                pngs = [f for f in os.listdir(d_path) if f.endswith('.png') and f not in ['albedo.png', 'normal.png', 'depth.png', 'roughness.png']]
                if pngs:
                    img_path = os.path.join(d_path, pngs[0])
            if img_path is None:
                continue
                
            img = Image.open(img_path)
            W, H = img.size
            
            if 'cam_K' in meta:
                K = np.array(meta['cam_K']).reshape(3, 3).astype(np.float32)
                R_w2c = np.array(meta['cam_R']).reshape(3, 3)
                T_w2c = np.array(meta['cam_T']).flatten()
                w2c = np.eye(4)
                w2c[:3, :3] = R_w2c
                w2c[:3, 3] = T_w2c
                c2w = np.linalg.inv(w2c)
            elif 'cam_transform_mat' in meta:
                val = meta['cam_transform_mat']
                c2w = np.fromstring(val, sep=',').reshape(4, 4) if isinstance(val, str) else np.array(val).reshape(4, 4)
                K = np.array([[1.2*W, 0, W/2], [0, 1.2*W, H/2], [0, 0, 1]], dtype=np.float32)
            elif 'transform_matrix' in meta:
                c2w = np.array(meta['transform_matrix'])
                angle_x = meta.get('camera_angle_x', 0.8)
                focal = 0.5 * W / np.tan(0.5 * angle_x)
                K = np.array([[focal, 0, W/2], [0, focal, H/2], [0, 0, 1]], dtype=np.float32)
            else:
                c2w = np.eye(4)
                K = np.array([[1.2*W, 0, W/2], [0, 1.2*W, H/2], [0, 0, 1]], dtype=np.float32)
                
            c2w[:3, 3] /= scale
            
            im_rgba = np.array(img.convert('RGBA')).astype(np.float32) / 255.0
            rgb = im_rgba[:, :, :3] * im_rgba[:, :, 3:4] + (1.0 - im_rgba[:, :, 3:4])
            mask = im_rgba[:, :, 3] > 0.5
            
            self.rgb_images.append(torch.from_numpy(rgb.reshape(-1, 3)).float())
            self.object_masks.append(torch.from_numpy(mask.reshape(-1)).bool())
            self.intrinsics_all.append(torch.from_numpy(K).float())
            self.pose_all.append(torch.from_numpy(c2w).float())
            self.image_paths.append(img_path)
            
        self.img_res = [H, W]
        self.total_pixels = H * W
        self.has_groundtruth = True

    def __len__(self):
        return self.n_cameras

    def __getitem__(self, idx):
        uv = np.mgrid[0:self.img_res[0], 0:self.img_res[1]]
        uv = torch.from_numpy(uv).float().reshape(2, -1).transpose(1, 0)
        
        sample = {
            'object_mask': self.object_masks[idx],
            'uv': uv,
            'intrinsics': self.intrinsics_all[idx],
            'pose': self.pose_all[idx],
            'rgb': self.rgb_images[idx]
        }
        
        ground_truth = {
            'rgb': self.rgb_images[idx]
        }
        
        if self.sampling_idx is not None:
            sample['object_mask'] = sample['object_mask'][self.sampling_idx]
            sample['uv'] = sample['uv'][self.sampling_idx]
            sample['rgb'] = sample['rgb'][self.sampling_idx]
            ground_truth['rgb'] = ground_truth['rgb'][self.sampling_idx]
            
        return idx, sample, ground_truth

    def collate_fn(self, batch_list):
        batch_list = zip(*batch_list)
        all_parsed = []
        for entry in batch_list:
            if type(entry[0]) is dict:
                ret = {}
                for k in entry[0].keys():
                    ret[k] = torch.stack([obj[k] for obj in entry])
                all_parsed.append(ret)
            else:
                all_parsed.append(torch.LongTensor(entry))
        return tuple(all_parsed)

    def change_sampling_idx(self, sampling_size):
        if sampling_size == -1:
            self.sampling_idx = None
        else:
            self.sampling_idx = torch.randperm(self.total_pixels)[:sampling_size]
