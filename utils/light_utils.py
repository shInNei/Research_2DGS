import torch
import math
import numpy as np
from PIL import Image

try:
    import imageio
except ImportError:
    imageio = None

def load_hdr_as_sg(hdr_path, num_sg=128, device="cuda"):
    """
    Loads an HDR environment map file (.hdr / .exr / .npy / .png) and projects it
    onto 128 Spherical Gaussians (SG) for fast, analytical PBR rendering.
    """
    if not os.path.exists(hdr_path):
        raise FileNotFoundError(f"HDR environment map file not found: {hdr_path}")
        
    if hdr_path.endswith('.hdr') or hdr_path.endswith('.exr'):
        if imageio is not None:
            hdr_img = imageio.imread(hdr_path)
        else:
            hdr_img = np.array(Image.open(hdr_path))
    elif hdr_path.endswith('.npy'):
        hdr_img = np.load(hdr_path)
    else:
        hdr_img = np.array(Image.open(hdr_path)).astype(np.float32) / 255.0

    if hdr_img.dtype != np.float32 and hdr_img.dtype != np.float64:
        hdr_img = hdr_img.astype(np.float32) / 255.0

    H, W, C = hdr_img.shape
    hdr_tensor = torch.tensor(hdr_img[..., :3], dtype=torch.float32, device=device)
    
    # Generate 128 uniform SG directions on the sphere
    indices = torch.arange(0, num_sg, dtype=torch.float32, device=device)
    phi = torch.arccos(1.0 - 2.0 * (indices + 0.5) / num_sg)
    theta = math.pi * (1.0 + 5.0**0.5) * indices
    
    x = torch.sin(phi) * torch.cos(theta)
    y = torch.sin(phi) * torch.sin(theta)
    z = torch.cos(phi)
    sg_dirs = torch.stack([x, y, z], dim=-1) # [M, 3]
    
    # Lat-Long pixel spherical directions
    v_grid = (torch.arange(H, dtype=torch.float32, device=device) + 0.5) / H * math.pi
    u_grid = (torch.arange(W, dtype=torch.float32, device=device) + 0.5) / W * 2.0 * math.pi
    grid_v, grid_u = torch.meshgrid(v_grid, u_grid, indexing='ij')
    
    dir_x = torch.sin(grid_v) * torch.cos(grid_u)
    dir_y = torch.sin(grid_v) * torch.sin(grid_u)
    dir_z = torch.cos(grid_v)
    pixel_dirs = torch.stack([dir_x, dir_y, dir_z], dim=-1).reshape(-1, 3) # [H*W, 3]
    pixel_colors = hdr_tensor.reshape(-1, 3) # [H*W, 3]
    
    # Solid angle per pixel: dOmega = sin(theta) * dTheta * dPhi
    d_theta = math.pi / H
    d_phi = 2.0 * math.pi / W
    solid_angles = torch.sin(grid_v).reshape(-1, 1) * d_theta * d_phi # [H*W, 1]
    
    # Project pixel colors onto SG directions
    sharpness = 32.0
    dot_products = torch.matmul(pixel_dirs, sg_dirs.T) # [H*W, M]
    weights = torch.exp(sharpness * (dot_products - 1.0)) * solid_angles # [H*W, M]
    
    sg_colors = torch.matmul(weights.T, pixel_colors) * (sharpness / (2.0 * math.pi)) # [M, 3]
    sg_sharps = torch.ones((num_sg, 1), dtype=torch.float32, device=device) * sharpness
    
    return {
        "sg_dir": sg_dirs,
        "sg_sharp": sg_sharps,
        "sg_color": torch.clamp(sg_colors, min=0.0)
    }
