import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import os, cv2
import matplotlib.pyplot as plt
import math

def depths_to_points(view, depthmap):
    c2w = (view.world_view_transform.T).inverse()
    W, H = view.image_width, view.image_height
    ndc2pix = torch.tensor([
        [W / 2, 0, 0, (W) / 2],
        [0, H / 2, 0, (H) / 2],
        [0, 0, 0, 1]]).float().cuda().T
    projection_matrix = c2w.T @ view.full_proj_transform
    intrins = (projection_matrix @ ndc2pix)[:3,:3].T
    
    grid_x, grid_y = torch.meshgrid(torch.arange(W, device='cuda').float(), torch.arange(H, device='cuda').float(), indexing='xy')
    points = torch.stack([grid_x, grid_y, torch.ones_like(grid_x)], dim=-1).reshape(-1, 3)
    rays_d = points @ intrins.inverse().T @ c2w[:3,:3].T
    rays_o = c2w[:3,3]
    points = depthmap.reshape(-1, 1) * rays_d + rays_o
    return points

def depth_to_normal(view, depth):
    """
        view: view camera
        depth: depthmap (shape: [1, H, W])
    """
    points = depths_to_points(view, depth).reshape(*depth.shape[1:], 3) # [H, W, 3]
    H, W, C = points.shape
    
    # Permute to [C, 1, H, W] for conv2d
    x = points.permute(2, 0, 1).unsqueeze(1) # [3, 1, H, W]
    
    # Sobel kernels (kx is horizontal gradient, ky is vertical gradient)
    kx = torch.tensor([[-1.,  0.,  1.],
                       [-2.,  0.,  2.],
                       [-1.,  0.,  1.]], device=depth.device).view(1, 1, 3, 3)
    ky = torch.tensor([[-1., -2., -1.],
                       [ 0.,  0.,  0.],
                       [ 1.,  2.,  1.]], device=depth.device).view(1, 1, 3, 3)
    
    # Pad input with replicate padding to avoid edge artifacts
    x_padded = F.pad(x, (1, 1, 1, 1), mode='replicate')
    
    dx = F.conv2d(x_padded, kx, padding=0) # horizontal gradient [3, 1, H, W]
    dy = F.conv2d(x_padded, ky, padding=0) # vertical gradient [3, 1, H, W]
    
    # Reshape back to [H, W, 3]
    dx = dx.squeeze(1).permute(1, 2, 0)
    dy = dy.squeeze(1).permute(1, 2, 0)
    
    # Cross product to get normal: cross(dy, dx) since dy is vertical (Y) and dx is horizontal (X)
    normal_map = torch.cross(dy, dx, dim=-1)
    normal_map = torch.nn.functional.normalize(normal_map, dim=-1)
    
    return normal_map