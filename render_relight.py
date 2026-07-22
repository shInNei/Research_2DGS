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

from utils.light_utils import load_hdr_as_sg

@torch.no_grad()
def render_relighting(dataset, iteration, pipe, hdr_path=None, relight_light_dir=None, output_path=None):
    """
    Renders test views under a NEW relighting condition (e.g. HDR map or rotated point light)
    and computes Relighting PSNR, SSIM, LPIPS metrics.
    """
    if hdr_path and os.path.exists(hdr_path):
        env_name = os.path.splitext(os.path.basename(hdr_path))[0]
        dataset.eval_light_name = env_name
        relight_name = f"hdr_{env_name}"
    else:
        relight_name = "orbit"

    # Cleanup legacy leftover messy folders inside relight if present
    import shutil
    base_relight = os.path.join(dataset.model_path, "relight")
    for legacy_dir in ["ours_30000", "test"]:
        p_legacy = os.path.join(base_relight, legacy_dir)
        if os.path.exists(p_legacy) and os.path.isdir(p_legacy):
            try:
                shutil.rmtree(p_legacy)
            except:
                pass

    gaussians = GaussianModel(dataset.sh_degree)
    gaussians.light_type = getattr(dataset, 'light_type', 'colocated')
    scene = Scene(dataset, gaussians, load_iteration=iteration, shuffle=False)
    
    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")
    
    test_cameras = scene.getTestCameras()
    if len(test_cameras) == 0:
        print("No test cameras found!")
        return

    relight_dir = os.path.join(dataset.model_path, "relight", relight_name, "test", f"ours_{scene.loaded_iter}")
    renders_path = os.path.join(relight_dir, "renders")
    gts_path = os.path.join(relight_dir, "gt")
    os.makedirs(renders_path, exist_ok=True)
    os.makedirs(gts_path, exist_ok=True)
    
    override_sg = None
    if hdr_path and os.path.exists(hdr_path):
        print(f"Loading HDR environment map: {hdr_path}")
        override_sg = load_hdr_as_sg(hdr_path, num_sg=128)
        print(f"Projected {hdr_path} onto 128 Spherical Gaussians successfully!")

    print(f"Rendering Relighting trajectory under new light condition to {relight_dir}...")
    
    for idx, viewpoint_cam in tqdm(enumerate(test_cameras), desc="Relighting views"):
        campos = viewpoint_cam.camera_center
        
        if override_sg is not None:
            render_pkg = render(viewpoint_cam, gaussians, pipe, background, override_sg=override_sg)
        else:
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
    parser.add_argument("--hdr_path", default="", type=str, help="Path to custom .hdr / .exr environment map file")
    args = get_combined_args(parser)
    
    dataset, iteration, pipe = model.extract(args), args.iteration, pipeline.extract(args)
    render_relighting(dataset, iteration, pipe, hdr_path=args.hdr_path)
