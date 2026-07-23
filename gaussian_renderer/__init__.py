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
import math
from diff_surfel_rasterization import GaussianRasterizationSettings, GaussianRasterizer
from scene.gaussian_model import GaussianModel
from utils.sh_utils import eval_sh
from utils.point_utils import depth_to_normal
from utils.general_utils import build_rotation

def shade_anisotropic_ggx(pc, v_dir, l_dir, t_x, t_y, n):
    # Ensure unit tangent space vectors
    t_x = torch.nn.functional.normalize(t_x, dim=-1)
    t_y = torch.nn.functional.normalize(t_y, dim=-1)
    n = torch.nn.functional.normalize(n, dim=-1)

    # Flip normal if it faces away from the viewer (double-sided rendering)
    v_z_raw = (v_dir * n).sum(dim=-1, keepdim=True)
    sign = torch.where(v_z_raw >= 0.0, 1.0, -1.0)
    n = n * sign

    # 1. Project vectors to local tangent space
    v_x = (v_dir * t_x).sum(dim=-1, keepdim=True)
    v_y = (v_dir * t_y).sum(dim=-1, keepdim=True)
    v_z = v_z_raw * sign
    
    l_x = (l_dir * t_x).sum(dim=-1, keepdim=True)
    l_y = (l_dir * t_y).sum(dim=-1, keepdim=True)
    l_z = (l_dir * n).sum(dim=-1, keepdim=True)
    
    # Halfway vector
    h_dir = v_dir + l_dir
    h_dir = h_dir / (torch.norm(h_dir, dim=-1, keepdim=True) + 1e-6)
    
    h_x = (h_dir * t_x).sum(dim=-1, keepdim=True)
    h_y = (h_dir * t_y).sum(dim=-1, keepdim=True)
    h_z = (h_dir * n).sum(dim=-1, keepdim=True)
    
    # Get material parameters
    albedo = pc.get_base_color # [N, 3]
    metallic = pc.get_metallic # [N, 1]
    roughness = pc.get_roughness # [N, 2]
    
    alpha_x = roughness[:, 0:1]
    alpha_y = roughness[:, 1:2]
    
    # Remap roughness: standard GGX uses alpha = roughness^2
    alpha_x = torch.clamp(alpha_x * alpha_x, min=0.001, max=1.0)
    alpha_y = torch.clamp(alpha_y * alpha_y, min=0.001, max=1.0)
    
    # 2. Fresnel term F (Schlick)
    F_0 = 0.04 * (1.0 - metallic) + albedo * metallic
    v_dot_h = (v_dir * h_dir).sum(dim=-1, keepdim=True).clamp(0.0, 1.0)
    F = F_0 + (1.0 - F_0) * torch.pow(1.0 - v_dot_h, 5)
    
    # 3. Normal Distribution Function D (Anisotropic GGX NDF)
    h_z = torch.clamp(h_z, min=0.0)
    term_ndf = (h_x * h_x) / (alpha_x * alpha_x) + (h_y * h_y) / (alpha_y * alpha_y) + h_z * h_z
    D = (h_z > 0.0).float() / (math.pi * alpha_x * alpha_y * term_ndf * term_ndf + 1e-6)
    
    # 4. Height-correlated Smith Masking-Shadowing G_2
    def get_lambda(omega_x, omega_y, omega_z):
        omega_z_sq = torch.clamp(omega_z * omega_z, min=1e-6)
        val = 1.0 + (alpha_x * alpha_x * omega_x * omega_x + alpha_y * alpha_y * omega_y * omega_y) / omega_z_sq
        return (-1.0 + torch.sqrt(torch.clamp(val, min=0.0))) / 2.0
    
    v_z = torch.clamp(v_z, min=0.0)
    l_z = torch.clamp(l_z, min=0.0)
    
    lambda_v = get_lambda(v_x, v_y, v_z)
    lambda_l = get_lambda(l_x, l_y, l_z)
    
    G_2 = ((v_z > 0.0) & (l_z > 0.0)).float() / (1.0 + lambda_v + lambda_l + 1e-6)
    
    # 5. Combined Shading
    cos_l = torch.clamp(l_z, min=0.0)
    cos_v = torch.clamp(v_z, min=1e-3)
    
    diffuse = albedo * (1.0 - metallic) * cos_l
    specular = (D * G_2 * F) / (4.0 * cos_v)
    
    shaded_colors = diffuse + specular
    return torch.clamp(shaded_colors, 0.0, 1.0)

def shade_anisotropic_ggx_sg_point(pc, v_dir, n):
    # 1. Flip normal if it faces away from viewer
    v_z = (v_dir * n).sum(dim=-1, keepdim=True)
    sign = torch.where(v_z >= 0.0, 1.0, -1.0)
    normal = n * sign
    v_z = v_z * sign
    
    # Get material parameters
    albedo = pc.get_base_color # [N, 3]
    metallic = pc.get_metallic # [N, 1]
    roughness = pc.get_roughness # [N, 2]
    
    alpha_x = roughness[:, 0:1]
    alpha_y = roughness[:, 1:2]
    alpha_x = torch.clamp(alpha_x * alpha_x, min=0.001, max=1.0)
    alpha_y = torch.clamp(alpha_y * alpha_y, min=0.001, max=1.0)
    alpha = torch.sqrt(alpha_x * alpha_y)
    
    # Normalize SG directions and clamp sharpness/colors
    sg_dir = torch.nn.functional.normalize(pc.sg_dir, dim=-1) # [M, 3]
    sg_sharp = torch.clamp(pc.sg_sharp, min=0.1, max=1000.0) # [M, 1]
    sg_color = torch.clamp(pc.sg_color, min=0.0) # [M, 3]
    
    # 2. DIFFUSE SHADING (Cosine Convolution with SGs)
    normal_dot_dir = normal @ sg_dir.T # [N, M]
    cos_term = torch.clamp(normal_dot_dir, min=0.0)
    sg_integral = (2.0 * math.pi / sg_sharp.T) * (1.0 - torch.exp(-2.0 * sg_sharp.T)) # [1, M]
    diffuse_light = (cos_term * sg_integral) @ sg_color # [N, 3]
    
    diffuse = albedo * (1.0 - metallic) * diffuse_light
    
    # 3. SPECULAR SHADING (Isotropic analytical SG Specular Convolution)
    r = 2.0 * v_z * normal - v_dir
    r = torch.nn.functional.normalize(r, dim=-1)
    
    # Specular lobe sharpness: lambda_spec = 2 / (alpha^2)
    lambda_spec = 2.0 / (alpha * alpha + 1e-5) # [N, 1]
    
    # Analytical SG Specular Convolution:
    r_dot_dir = r @ sg_dir.T # [N, M]
    sharp_env = sg_sharp.T # [1, M]
    lambda_total = sharp_env + lambda_spec # [N, M]
    
    exp_factor = (sharp_env * lambda_spec / lambda_total) * (r_dot_dir - 1.0)
    exp_factor = torch.clamp(exp_factor, min=-40.0, max=0.0)
    
    spec_intensity = (2.0 * math.pi / lambda_total) * torch.exp(exp_factor) # [N, M]
    specular_light = spec_intensity @ sg_color # [N, 3]
    
    # Lazarov/UE4 Split-Sum envBRDF approximation
    F_0 = 0.04 * (1.0 - metallic) + albedo * metallic
    v_dot_n = v_z.clamp(0.0, 1.0)
    
    # Fit coefficients
    r_x = alpha * -1.0 + 1.0
    r_y = alpha * -0.0275 + 0.0422
    r_z = alpha * -0.572 + 1.047
    r_w = alpha * 0.022 - 0.040
    
    a004 = torch.min(r_x * r_x, torch.exp2(-9.28 * v_dot_n)) * r_x + r_y
    scale = -1.04 * a004 + r_z
    bias = 1.04 * a004 + r_w
    
    specular = specular_light * (F_0 * scale + bias)
    
    shaded_colors = diffuse + specular
    return torch.clamp(shaded_colors, 0.0, 1.0)

def shade_deferred_anisotropic_ggx_2d(albedo_map, normal_map, roughness_map, metallic_map, v_dir_map, l_dir_map=None):
    """
    Evaluates Anisotropic GGX PBR shading vectorized directly on 2D G-Buffer image tensors.
    albedo_map: [3, H, W]
    normal_map: [3, H, W]
    roughness_map: [2, H, W]
    metallic_map: [1, H, W]
    v_dir_map: [3, H, W]
    """
    albedo = albedo_map.permute(1, 2, 0)       # [H, W, 3]
    n = normal_map.permute(1, 2, 0)            # [H, W, 3]
    roughness = roughness_map.permute(1, 2, 0)  # [H, W, 2]
    metallic = metallic_map.permute(1, 2, 0)    # [H, W, 1]
    v_dir = v_dir_map.permute(1, 2, 0)          # [H, W, 3]
    l_dir = v_dir if l_dir_map is None else l_dir_map.permute(1, 2, 0)

    n = torch.nn.functional.normalize(n, dim=-1)
    v_dir = torch.nn.functional.normalize(v_dir, dim=-1)
    l_dir = torch.nn.functional.normalize(l_dir, dim=-1)

    # Flip normal if facing away from viewer
    v_z_raw = (v_dir * n).sum(dim=-1, keepdim=True)
    sign = torch.where(v_z_raw >= 0.0, 1.0, -1.0)
    n = n * sign
    v_z = v_z_raw * sign

    # Construct tangent frame (t_x, t_y) from normal n in view space
    up = torch.tensor([0.0, 1.0, 0.0], device=n.device).expand_as(n)
    dot_up = (n * up).sum(dim=-1, keepdim=True).abs()
    alt_up = torch.tensor([1.0, 0.0, 0.0], device=n.device).expand_as(n)
    up = torch.where(dot_up > 0.9, alt_up, up)
    
    t_x = torch.cross(up, n, dim=-1)
    t_x = torch.nn.functional.normalize(t_x, dim=-1)
    t_y = torch.cross(n, t_x, dim=-1)
    t_y = torch.nn.functional.normalize(t_y, dim=-1)

    # Projections
    v_x = (v_dir * t_x).sum(dim=-1, keepdim=True)
    v_y = (v_dir * t_y).sum(dim=-1, keepdim=True)

    l_x = (l_dir * t_x).sum(dim=-1, keepdim=True)
    l_y = (l_dir * t_y).sum(dim=-1, keepdim=True)
    l_z = (l_dir * n).sum(dim=-1, keepdim=True)

    # Halfway vector
    h_dir = v_dir + l_dir
    h_dir = torch.nn.functional.normalize(h_dir, dim=-1)

    h_x = (h_dir * t_x).sum(dim=-1, keepdim=True)
    h_y = (h_dir * t_y).sum(dim=-1, keepdim=True)
    h_z = (h_dir * n).sum(dim=-1, keepdim=True)

    # Roughness alpha = roughness^2
    alpha_x = roughness[..., 0:1]
    alpha_y = roughness[..., 1:2]
    alpha_x = torch.clamp(alpha_x * alpha_x, min=0.001, max=1.0)
    alpha_y = torch.clamp(alpha_y * alpha_y, min=0.001, max=1.0)

    # 1. Fresnel F (Schlick)
    F_0 = 0.04 * (1.0 - metallic) + albedo * metallic
    v_dot_h = (v_dir * h_dir).sum(dim=-1, keepdim=True).clamp(0.0, 1.0)
    F = F_0 + (1.0 - F_0) * torch.pow(1.0 - v_dot_h, 5)

    # 2. Anisotropic GGX NDF
    h_z = torch.clamp(h_z, min=0.0)
    term_ndf = (h_x * h_x) / (alpha_x * alpha_x) + (h_y * h_y) / (alpha_y * alpha_y) + h_z * h_z
    D = (h_z > 0.0).float() / (math.pi * alpha_x * alpha_y * term_ndf * term_ndf + 1e-6)

    # 3. Smith Masking-Shadowing G_2
    def get_lambda(omega_x, omega_y, omega_z):
        omega_z_sq = torch.clamp(omega_z * omega_z, min=1e-6)
        val = 1.0 + (alpha_x * alpha_x * omega_x * omega_x + alpha_y * alpha_y * omega_y * omega_y) / omega_z_sq
        return (-1.0 + torch.sqrt(torch.clamp(val, min=0.0))) / 2.0

    v_z = torch.clamp(v_z, min=0.0)
    l_z = torch.clamp(l_z, min=0.0)

    lambda_v = get_lambda(v_x, v_y, v_z)
    lambda_l = get_lambda(l_x, l_y, l_z)

    G_2 = ((v_z > 0.0) & (l_z > 0.0)).float() / (1.0 + lambda_v + lambda_l + 1e-6)

    # 4. Combined Shading
    cos_l = torch.clamp(l_z, min=0.0)
    cos_v = torch.clamp(v_z, min=1e-3)

    diffuse = albedo * (1.0 - metallic) * cos_l
    specular = (D * G_2 * F) / (4.0 * cos_v)

    shaded_colors = diffuse + specular
    shaded_colors = torch.clamp(shaded_colors, 0.0, 1.0)

    return shaded_colors.permute(2, 0, 1)

def render(viewpoint_camera, pc : GaussianModel, pipe, bg_color : torch.Tensor, scaling_modifier = 1.0, override_color = None, override_sg = None):
    """
    Render the scene using Deferred 2D G-Buffer PBR Pipeline.
    """
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg_color,
        scale_modifier=scaling_modifier,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=False
    )

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    means3D = pc.get_xyz
    means2D = screenspace_points
    opacity = pc.get_opacity

    scales = pc.get_scaling
    rotations = pc.get_rotation
    cov3D_precomp = None

    shs = None
    
    # 1. MULTI-PASS 3-CHANNEL DEFERRED G-BUFFER RASTERIZATION
    if override_color is not None:
        albedo_map, radii, allmap = rasterizer(
            means3D = means3D,
            means2D = means2D,
            shs = shs,
            colors_precomp = override_color,
            opacities = opacity,
            scales = scales,
            rotations = rotations,
            cov3D_precomp = cov3D_precomp
        )
        rendered_image = albedo_map
        roughness_map = torch.zeros((2, albedo_map.shape[1], albedo_map.shape[2]), device="cuda")
        metallic_map = torch.zeros((1, albedo_map.shape[1], albedo_map.shape[2]), device="cuda")
        normal_map = allmap[2:5]
        render_alpha = allmap[1:2]
    else:
        # Pass 1: Albedo (3 channels) + Geometry Normals (View space from allmap[2:5])
        albedo_precomp = pc.get_base_color # [N, 3]
        albedo_map, radii, allmap = rasterizer(
            means3D = means3D,
            means2D = means2D,
            shs = shs,
            colors_precomp = albedo_precomp,
            opacities = opacity,
            scales = scales,
            rotations = rotations,
            cov3D_precomp = cov3D_precomp
        )
        normal_map = allmap[2:5] # [3, H, W]
        render_alpha = allmap[1:2] # [1, H, W]

        # Pass 2: Material Attributes (2 channels roughness + 1 channel metallic = 3 channels)
        mat_precomp = torch.cat([pc.get_roughness, pc.get_metallic], dim=-1) # [N, 3]
        rendered_mat_features, _, _ = rasterizer(
            means3D = means3D,
            means2D = means2D,
            shs = shs,
            colors_precomp = mat_precomp,
            opacities = opacity,
            scales = scales,
            rotations = rotations,
            cov3D_precomp = cov3D_precomp
        )
        roughness_map = rendered_mat_features[0:2] # [2, H, W]
        metallic_map = rendered_mat_features[2:3]  # [1, H, W]

        # Compute 2D Per-pixel Camera View Ray Direction Map in View Space
        H, W = int(viewpoint_camera.image_height), int(viewpoint_camera.image_width)
        grid_y, grid_x = torch.meshgrid(
            torch.linspace(-tanfovy, tanfovy, H, device="cuda"),
            torch.linspace(-tanfovx, tanfovx, W, device="cuda"),
            indexing="ij"
        )
        v_dir_cam = torch.stack([grid_x, grid_y, torch.ones_like(grid_x)], dim=0) # [3, H, W]
        v_dir_cam = torch.nn.functional.normalize(v_dir_cam, dim=0)

        # 2. DEFERRED 2D IMAGE-SPACE PBR SHADER
        rendered_image = shade_deferred_anisotropic_ggx_2d(
            albedo_map = albedo_map,
            normal_map = normal_map,
            roughness_map = roughness_map,
            metallic_map = metallic_map,
            v_dir_map = v_dir_cam
        )

    # Additional regularizations & depth
    render_normal = (normal_map.permute(1,2,0) @ (viewpoint_camera.world_view_transform[:3,:3].T)).permute(2,0,1)
    render_depth_median = torch.nan_to_num(allmap[5:6], 0, 0)
    render_depth_expected = torch.nan_to_num(allmap[0:1] / (render_alpha + 1e-6), 0, 0)
    render_dist = allmap[6:7]

    surf_depth = render_depth_expected * (1-pipe.depth_ratio) + (pipe.depth_ratio) * render_depth_median
    surf_normal = depth_to_normal(viewpoint_camera, surf_depth).permute(2,0,1) * render_alpha.detach()

    rets = {
        "render": rendered_image,
        "viewspace_points": means2D,
        "visibility_filter": radii > 0,
        "radii": radii,
        "albedo_map": albedo_map,
        "roughness_map": roughness_map,
        "metallic_map": metallic_map,
        "normal_map": normal_map,
        "rend_alpha": render_alpha,
        "rend_dist": render_dist,
        "rend_normal": render_normal,
        "surf_normal": surf_normal
    }
    return rets