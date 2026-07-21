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

def shade_anisotropic_ggx_env(pc, v_dir, t_x, t_y, n):
    # Flip normal if it faces away from the viewer (double-sided rendering)
    v_z_raw = (v_dir * n).sum(dim=-1, keepdim=True)
    sign = torch.where(v_z_raw >= 0.0, 1.0, -1.0)
    n = n * sign
    v_z = v_z_raw * sign

    # Get material parameters
    albedo = pc.get_base_color # [N, 3]
    metallic = pc.get_metallic # [N, 1]
    roughness = pc.get_roughness # [N, 2]
    
    alpha_x = roughness[:, 0:1]
    alpha_y = roughness[:, 1:2]
    
    alpha_x = torch.clamp(alpha_x * alpha_x, min=0.001, max=1.0)
    alpha_y = torch.clamp(alpha_y * alpha_y, min=0.001, max=1.0)
    
    # 1. Diffuse component
    # We evaluate the global environment map (pc.env_sh) at the normal direction
    # env_sh shape: [9, 3] -> transpose to [3, 9] -> unsqueeze to [1, 3, 9] -> expand to [N, 3, 9]
    N = n.shape[0]
    sh_coeffs = pc.env_sh.T.unsqueeze(0).expand(N, -1, -1)
    
    diffuse_light = eval_sh(2, sh_coeffs, n)
    diffuse_light = torch.clamp(diffuse_light, min=0.0)
    diffuse = albedo * (1.0 - metallic) * diffuse_light

    # 2. Specular component
    # Reflection vector
    r = 2.0 * v_z * n - v_dir
    r = r / (torch.norm(r, dim=-1, keepdim=True) + 1e-6)
    
    # Roughness-dependent scale factors for SH bands
    alpha = torch.sqrt(alpha_x * alpha_y)
    scale_0 = torch.ones_like(alpha)
    scale_1 = torch.exp(-1.0 * alpha * alpha)
    scale_2 = torch.exp(-3.0 * alpha * alpha)
    
    scales = torch.cat([
        scale_0, # l=0 (1 coeff)
        scale_1, scale_1, scale_1, # l=1 (3 coeffs)
        scale_2, scale_2, scale_2, scale_2, scale_2 # l=2 (5 coeffs)
    ], dim=-1).unsqueeze(1) # shape [N, 1, 9]
    
    sh_coeffs_specular = sh_coeffs * scales
    specular_light = eval_sh(2, sh_coeffs_specular, r)
    specular_light = torch.clamp(specular_light, min=0.0)
    
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

def render(viewpoint_camera, pc : GaussianModel, pipe, bg_color : torch.Tensor, scaling_modifier = 1.0, override_color = None):
    """
    Render the scene. 
    
    Background tensor (bg_color) must be on GPU!
    """
 
    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    # Set up rasterization configuration
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
        debug=False,
        # pipe.debug
    )

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    means3D = pc.get_xyz
    means2D = screenspace_points
    opacity = pc.get_opacity

    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None
    cov3D_precomp = None
    if pipe.compute_cov3D_python:
        # currently don't support normal consistency loss if use precomputed covariance
        splat2world = pc.get_covariance(scaling_modifier)
        W, H = viewpoint_camera.image_width, viewpoint_camera.image_height
        near, far = viewpoint_camera.znear, viewpoint_camera.zfar
        ndc2pix = torch.tensor([
            [W / 2, 0, 0, (W-1) / 2],
            [0, H / 2, 0, (H-1) / 2],
            [0, 0, far-near, near],
            [0, 0, 0, 1]]).float().cuda().T
        world2pix =  viewpoint_camera.full_proj_transform @ ndc2pix
        cov3D_precomp = (splat2world[:, [0,1,3]] @ world2pix[:,[0,1,3]]).permute(0,2,1).reshape(-1, 9) # column major
    else:
        scales = pc.get_scaling
        rotations = pc.get_rotation
    
    shs = None
    colors_precomp = None
    if override_color is None:
        # Calculate local tangent space
        R = build_rotation(pc._rotation)
        t_x = R[:, :, 0]
        t_y = R[:, :, 1]
        n = R[:, :, 2]
        
        # Calculate view direction
        campos = viewpoint_camera.camera_center
        dir_to_cam = campos.unsqueeze(0) - pc.get_xyz
        v_dir = dir_to_cam / (torch.norm(dir_to_cam, dim=-1, keepdim=True) + 1e-6)
        
        colors_precomp = shade_anisotropic_ggx_env(pc, v_dir, t_x, t_y, n)
    else:
        colors_precomp = override_color
    
    rendered_image, radii, allmap = rasterizer(
        means3D = means3D,
        means2D = means2D,
        shs = shs,
        colors_precomp = colors_precomp,
        opacities = opacity,
        scales = scales,
        rotations = rotations,
        cov3D_precomp = cov3D_precomp
    )
    
    # Those Gaussians that were frustum culled or had a radius of 0 were not visible.
    # They will be excluded from value updates used in the splitting criteria.
    rets =  {"render": rendered_image,
            "viewspace_points": means2D,
            "visibility_filter" : radii > 0,
            "radii": radii,
    }


    # additional regularizations
    render_alpha = allmap[1:2]

    # get normal map
    # transform normal from view space to world space
    render_normal = allmap[2:5]
    render_normal = (render_normal.permute(1,2,0) @ (viewpoint_camera.world_view_transform[:3,:3].T)).permute(2,0,1)
    
    # get median depth map
    render_depth_median = allmap[5:6]
    render_depth_median = torch.nan_to_num(render_depth_median, 0, 0)

    # get expected depth map
    render_depth_expected = allmap[0:1]
    render_depth_expected = (render_depth_expected / render_alpha)
    render_depth_expected = torch.nan_to_num(render_depth_expected, 0, 0)
    
    # get depth distortion map
    render_dist = allmap[6:7]

    # psedo surface attributes
    # surf depth is either median or expected by setting depth_ratio to 1 or 0
    # for bounded scene, use median depth, i.e., depth_ratio = 1; 
    # for unbounded scene, use expected depth, i.e., depth_ration = 0, to reduce disk anliasing.
    surf_depth = render_depth_expected * (1-pipe.depth_ratio) + (pipe.depth_ratio) * render_depth_median
    
    # assume the depth points form the 'surface' and generate psudo surface normal for regularizations.
    surf_normal = depth_to_normal(viewpoint_camera, surf_depth)
    surf_normal = surf_normal.permute(2,0,1)
    # remember to multiply with accum_alpha since render_normal is unnormalized.
    surf_normal = surf_normal * (render_alpha).detach()


    rets.update({
            'rend_alpha': render_alpha,
            'rend_normal': render_normal,
            'rend_dist': render_dist,
            'surf_depth': surf_depth,
            'surf_normal': surf_normal,
    })

    return rets