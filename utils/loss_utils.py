#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
import torch.nn.functional as F
from torch.autograd import Variable
from math import exp

def l1_loss(network_output, gt):
    return torch.abs((network_output - gt)).mean()

def l2_loss(network_output, gt):
    return ((network_output - gt) ** 2).mean()

def gaussian(window_size, sigma):
    gauss = torch.Tensor([exp(-(x - window_size // 2) ** 2 / float(2 * sigma ** 2)) for x in range(window_size)])
    return gauss / gauss.sum()


def smooth_loss(disp, img):
    grad_disp_x = torch.abs(disp[:,1:-1, :-2] + disp[:,1:-1,2:] - 2 * disp[:,1:-1,1:-1])
    grad_disp_y = torch.abs(disp[:,:-2, 1:-1] + disp[:,2:,1:-1] - 2 * disp[:,1:-1,1:-1])
    grad_img_x = torch.mean(torch.abs(img[:, 1:-1, :-2] - img[:, 1:-1, 2:]), 0, keepdim=True) * 0.5
    grad_img_y = torch.mean(torch.abs(img[:, :-2, 1:-1] - img[:, 2:, 1:-1]), 0, keepdim=True) * 0.5
    grad_disp_x *= torch.exp(-grad_img_x)
    grad_disp_y *= torch.exp(-grad_img_y)
    return grad_disp_x.mean() + grad_disp_y.mean()

def create_window(window_size, channel):
    _1D_window = gaussian(window_size, 1.5).unsqueeze(1)
    _2D_window = _1D_window.mm(_1D_window.t()).float().unsqueeze(0).unsqueeze(0)
    window = Variable(_2D_window.expand(channel, 1, window_size, window_size).contiguous())
    return window

def ssim(img1, img2, window_size=11, size_average=True):
    channel = img1.size(-3)
    window = create_window(window_size, channel)

    if img1.is_cuda:
        window = window.cuda(img1.get_device())
    window = window.type_as(img1)

    return _ssim(img1, img2, window, window_size, channel, size_average)

def _ssim(img1, img2, window, window_size, channel, size_average=True):
    mu1 = F.conv2d(img1, window, padding=window_size // 2, groups=channel)
    mu2 = F.conv2d(img2, window, padding=window_size // 2, groups=channel)

    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2

    sigma1_sq = F.conv2d(img1 * img1, window, padding=window_size // 2, groups=channel) - mu1_sq
    sigma2_sq = F.conv2d(img2 * img2, window, padding=window_size // 2, groups=channel) - mu2_sq
    sigma12 = F.conv2d(img1 * img2, window, padding=window_size // 2, groups=channel) - mu1_mu2

    C1 = 0.01 ** 2
    C2 = 0.03 ** 2

    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))

    if size_average:
        return ssim_map.mean()
    else:
        return ssim_map.mean(1).mean(1).mean(1)

def knn_3d_material_smoothness_loss(gaussians, k=5, sample_size=4096):
    """
    Computes 3D KNN Bilateral Material Smoothness Loss directly on 3D Gaussians.
    gaussians: GaussianModel object containing get_xyz, get_base_color, get_roughness, get_metallic
    k: Number of nearest 3D neighbors (default 5)
    sample_size: Subsampled batch size for ultra-fast GPU computation (<0.5ms)
    """
    xyz = gaussians.get_xyz
    num_pts = xyz.shape[0]
    if num_pts <= k + 1:
        return torch.tensor(0.0, device="cuda")

    # Subsample indices for fast GPU cdist
    if num_pts > sample_size:
        indices = torch.randint(0, num_pts, (sample_size,), device="cuda")
        xyz_sub = xyz[indices]
        color_sub = gaussians.get_base_color[indices]
        rough_sub = gaussians.get_roughness[indices]
        metal_sub = gaussians.get_metallic[indices]
        amb_sub = gaussians.get_ambient[indices]
    else:
        xyz_sub = xyz
        color_sub = gaussians.get_base_color
        rough_sub = gaussians.get_roughness
        metal_sub = gaussians.get_metallic
        amb_sub = gaussians.get_ambient

    # Compute 3D pairwise Euclidean distances: [B, B]
    dists = torch.cdist(xyz_sub, xyz_sub, p=2)

    # Get top-k nearest 3D neighbors (excluding self at index 0)
    top_dists, top_idxs = torch.topk(dists, k=k+1, largest=False, dim=-1)
    knn_idxs = top_idxs[:, 1:]
    knn_dists = top_dists[:, 1:]

    # Bilateral Gaussian spatial distance weights
    sigma = torch.mean(knn_dists).detach() + 1e-6
    spatial_weights = torch.exp(-knn_dists / sigma).unsqueeze(-1)

    # Gather material properties of nearest neighbors
    color_neighbors = color_sub[knn_idxs]
    rough_neighbors = rough_sub[knn_idxs]
    metal_neighbors = metal_sub[knn_idxs]
    amb_neighbors = amb_sub[knn_idxs]

    # Compute weighted L1 differences
    diff_color = (torch.abs(color_sub.unsqueeze(1) - color_neighbors) * spatial_weights).mean()
    diff_rough = (torch.abs(rough_sub.unsqueeze(1) - rough_neighbors) * spatial_weights).mean()
    diff_metal = (torch.abs(metal_sub.unsqueeze(1) - metal_neighbors) * spatial_weights).mean()
    diff_amb = (torch.abs(amb_sub.unsqueeze(1) - amb_neighbors) * spatial_weights).mean()

    return diff_color + diff_rough + diff_metal + diff_amb



