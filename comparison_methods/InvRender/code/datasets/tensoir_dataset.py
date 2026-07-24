import os
import json
import torch
import numpy as np
from PIL import Image
import torch.utils.data

def load_rgb(path):
    img = Image.open(path).convert('RGB')
    return np.array(img).astype(np.float32) / 255.0

def load_mask(path):
    img = Image.open(path).convert('L')
    return (np.array(img).astype(np.float32) / 255.0) > 0.5

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
        
        scale = 2.0
        
        for d in subdirs:
            d_path = os.path.join(target_dir, d)
            jsons = [f for f in os.listdir(d_path) if f.endswith('.json')]
            if not jsons:
                continue
            meta_path = os.path.join(d_path, jsons[0])
            with open(meta_path, 'r') as f:
                meta = json.load(f)
                
            # Find image
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
            
            # Load RGB & Mask
            im_rgba = np.array(img.convert('RGBA')).astype(np.float32) / 255.0
            rgb = im_rgba[:, :, :3] * im_rgba[:, :, 3:4] + (1.0 - im_rgba[:, :, 3:4]) # white bg
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
        uv = torch.from_numpy(uv).float()
        uv = uv.reshape(2, -1).transpose(1, 0)

        sample = {
            'object_mask': self.object_masks[idx],
            'uv': uv,
            'intrinsics': self.intrinsics_all[idx],
            'pose': self.pose_all[idx]
        }
        sample['rgb'] = self.rgb_images[idx]
        return idx, sample
