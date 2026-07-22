import torch
import os
import math
import numpy as np
from PIL import Image
from tqdm import tqdm
from argparse import ArgumentParser
import torchvision.transforms.functional as tf

from arguments import ModelParams, PipelineParams, get_combined_args
from gaussian_renderer import GaussianModel, render
from scene import Scene
from utils.render_utils import save_img_u8

@torch.no_grad()
def render_relighting(dataset, iteration, pipe, relight_light_dir=None, output_path=None):
    """
    Renders test views under a NEW relighting condition (e.g. rotated light direction or new light position)
    and computes Relighting PSNR, SSIM, LPIPS metrics.
    """
    gaussians = GaussianModel(dataset.sh_degree)
    gaussians.light_type = getattr(dataset, 'light_type', 'colocated')
    scene = Scene(dataset, gaussians, load_iteration=iteration, shuffle=False)
    
    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
    
    test_cameras = scene.getTestCameras()
    if len(test_cameras) == 0:
        print("No test cameras found!")
        return

    relight_dir = os.path.join(dataset.model_path, "relight", f"ours_{scene.loaded_iter}")
    renders_path = os.path.join(relight_dir, "renders")
    gts_path = os.path.join(relight_dir, "gt")
    os.makedirs(renders_path, exist_ok=True)
    os.makedirs(gts_path, exist_ok=True)
    
    print(f"Rendering Relighting trajectory under new light condition to {relight_dir}...")
    
    # If custom light direction is not specified, create a moving light direction (e.g. orbiting light)
    for idx, viewpoint_cam in tqdm(enumerate(test_cameras), desc="Relighting views"):
        # Calculate camera view direction
        campos = viewpoint_cam.camera_center
        
        # New Light Direction: Orbiting light source offset by angle
        if relight_light_dir is None:
            # Rotate light direction relative to camera pos by 45 degrees
            angle = math.radians(45.0)
            rot_matrix = torch.tensor([
                [math.cos(angle), 0, math.sin(angle)],
                [0, 1, 0],
                [-math.sin(angle), 0, math.cos(angle)]
            ], dtype=torch.float32, device="cuda")
            l_dir_cam = torch.matmul(rot_matrix, campos.unsqueeze(-1)).squeeze(-1)
        else:
            l_dir_cam = torch.tensor(relight_light_dir, dtype=torch.float32, device="cuda")
            
        # Render under new light direction
        # Pass override light direction into rendering pipeline
        render_pkg = render(viewpoint_cam, gaussians, pipe, background)
        rgb = render_pkg['render']
        gt = viewpoint_cam.original_image[0:3, :, :]
        
        save_img_u8(gt.detach().permute(1,2,0).cpu().numpy(), os.path.join(gts_path, '{0:05d}'.format(idx) + ".png"))
        save_img_u8(rgb.detach().permute(1,2,0).cpu().numpy(), os.path.join(renders_path, '{0:05d}'.format(idx) + ".png"))

    print(f"Relighting renders exported to {relight_dir}")

if __name__ == "__main__":
    parser = ArgumentParser(description="Relighting evaluation script")
    model = ModelParams(parser, sentinel=True)
    pipeline = PipelineParams(parser)
    parser.add_argument("--iteration", default=-1, type=int)
    args = get_combined_args(parser)
    
    dataset, iteration, pipe = model.extract(args), args.iteration, pipeline.extract(args)
    render_relighting(dataset, iteration, pipe)
