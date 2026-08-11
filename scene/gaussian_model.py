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
import numpy as np
import math
from utils.general_utils import inverse_sigmoid, get_expon_lr_func, build_rotation
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from utils.sh_utils import RGB2SH
from simple_knn._C import distCUDA2
from utils.graphics_utils import BasicPointCloud
from utils.general_utils import strip_symmetric, build_scaling_rotation

class GaussianModel:

    def setup_functions(self):
        def build_covariance_from_scaling_rotation(center, scaling, scaling_modifier, rotation):
            RS = build_scaling_rotation(torch.cat([scaling * scaling_modifier, torch.ones_like(scaling)], dim=-1), rotation).permute(0,2,1)
            trans = torch.zeros((center.shape[0], 4, 4), dtype=torch.float, device="cuda")
            trans[:,:3,:3] = RS
            trans[:, 3,:3] = center
            trans[:, 3, 3] = 1
            return trans
        
        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation
        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid
        self.rotation_activation = torch.nn.functional.normalize

        self.base_color_activation = torch.sigmoid
        self.base_color_inverse_activation = inverse_sigmoid

        self.metallic_activation = torch.sigmoid
        self.metallic_inverse_activation = inverse_sigmoid

        self.roughness_activation = torch.sigmoid
        self.roughness_inverse_activation = inverse_sigmoid
        self.ambient_activation = torch.sigmoid
        self.ambient_inverse_activation = inverse_sigmoid


    def __init__(self, sh_degree : int):
        self.active_sh_degree = 0
        self.max_sh_degree = sh_degree  
        self._xyz = torch.empty(0)
        self._base_color = torch.empty(0)
        self._metallic = torch.empty(0)
        self._roughness = torch.empty(0)
        self._ambient_dc = torch.empty(0)
        self._ambient_rest = torch.empty(0)
        self._visibility_dc = torch.empty(0)
        self._visibility_rest = torch.empty(0)
        self._scaling = torch.empty(0)
        self._rotation = torch.empty(0)
        self._opacity = torch.empty(0)
        self.max_radii2D = torch.empty(0)
        self.xyz_gradient_accum = torch.empty(0)
        self.denom = torch.empty(0)
        self.optimizer = None
        self.percent_dense = 0
        self.spatial_lr_scale = 0
        self.setup_functions()

        # Initialize global Spherical Gaussian (SG) Mixture envmap (M = 128)
        M = 128
        indices = torch.arange(0, M, dtype=torch.float32)
        phi = torch.arccos(1.0 - 2.0 * (indices + 0.5) / M)
        theta = math.pi * (1.0 + 5.0**0.5) * indices
        
        x = torch.sin(phi) * torch.cos(theta)
        y = torch.sin(phi) * torch.sin(theta)
        z = torch.cos(phi)
        
        sg_dir_init = torch.stack([x, y, z], dim=-1).cuda()
        sg_sharp_init = torch.ones((M, 1), dtype=torch.float32, device="cuda") * 10.0
        sg_color_init = torch.ones((M, 3), dtype=torch.float32, device="cuda") * 0.5
        
        self.sg_dir = nn.Parameter(sg_dir_init.requires_grad_(True))
        self.sg_sharp = nn.Parameter(sg_sharp_init.requires_grad_(True))
        self.sg_color = nn.Parameter(sg_color_init.requires_grad_(True))

    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._base_color,
            self._metallic,
            self._roughness,
            self._ambient_dc,
            self._ambient_rest,
            self._visibility_dc,
            self._visibility_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.spatial_lr_scale,
            self.sg_dir,
            self.sg_sharp,
            self.sg_color,
        )
    
    def restore(self, model_args, training_args):
        (self.active_sh_degree, 
        self._xyz, 
        self._base_color, 
        self._metallic,
        self._roughness,
        self._ambient_dc,
            self._ambient_rest,
            self._visibility_dc,
            self._visibility_rest,
        self._scaling, 
        self._rotation, 
        self._opacity,
        self.max_radii2D, 
        xyz_gradient_accum, 
        denom,
        opt_dict, 
        self.spatial_lr_scale,
        sg_dir,
        sg_sharp,
        sg_color) = model_args[:17]
        
        self.sg_dir.data.copy_(sg_dir)
        self.sg_sharp.data.copy_(sg_sharp)
        self.sg_color.data.copy_(sg_color)

        self.training_setup(training_args)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)

    @property
    def get_scaling(self):
        return self.scaling_activation(self._scaling) #.clamp(max=1)
    
    @property
    def get_rotation(self):
        return self.rotation_activation(self._rotation)
    
    @property
    def get_xyz(self):
        return self._xyz
    
    @property
    def get_base_color(self):
        return self.base_color_activation(self._base_color)

    @property
    def get_metallic(self):
        return self.metallic_activation(self._metallic)

    @property
    def get_roughness(self):
        return self.roughness_activation(self._roughness)

    @property
    def get_ambient_dc(self):
        return self._ambient_dc

    @property
    def get_ambient_rest(self):
        return self._ambient_rest

    @property
    def get_visibility_dc(self):
        return self._visibility_dc

    @property
    def get_visibility_rest(self):
        return self._visibility_rest

    
    @property
    def get_opacity(self):
        return self.opacity_activation(self._opacity)
    
    def get_covariance(self, scaling_modifier = 1):
        return self.covariance_activation(self.get_xyz, self.get_scaling, scaling_modifier, self._rotation)

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        
        # Convert colors directly to base_color parameter using inverse sigmoid
        pcd_colors = torch.tensor(np.asarray(pcd.colors)).float().cuda()
        pcd_colors = torch.clamp(pcd_colors, 0.001, 0.999)
        base_color = self.base_color_inverse_activation(pcd_colors)

        num_points = fused_point_cloud.shape[0]
        print("Number of points at initialisation : ", num_points)

        dist2 = torch.clamp_min(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001)
        scales = torch.log(torch.sqrt(dist2))[...,None].repeat(1, 2)
        rots = torch.rand((num_points, 4), device="cuda")

        opacities = self.inverse_opacity_activation(0.1 * torch.ones((num_points, 1), dtype=torch.float, device="cuda"))
        metallic_init = self.metallic_inverse_activation(0.1 * torch.ones((num_points, 1), dtype=torch.float, device="cuda"))
        roughness_init = self.roughness_inverse_activation(0.5 * torch.ones((num_points, 2), dtype=torch.float, device="cuda"))
        ambient_init = 0.15 * torch.ones((num_points, 3), dtype=torch.float, device="cuda")
        ambient_dc = RGB2SH(ambient_init)
        ambient_rest = torch.zeros((num_points, (self.max_sh_degree + 1)**2 - 1, 3), dtype=torch.float, device="cuda")
        
        vis_init = 1.0 * torch.ones((num_points, 1), dtype=torch.float, device="cuda")
        visibility_dc = RGB2SH(vis_init)
        visibility_rest = torch.zeros((num_points, (self.max_sh_degree + 1)**2 - 1, 1), dtype=torch.float, device="cuda")

        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._base_color = nn.Parameter(base_color.requires_grad_(True))
        self._metallic = nn.Parameter(metallic_init.requires_grad_(True))
        self._roughness = nn.Parameter(roughness_init.requires_grad_(True))
        self._ambient_dc = nn.Parameter(ambient_dc.requires_grad_(True))
        self._ambient_rest = nn.Parameter(ambient_rest.requires_grad_(True))
        self._visibility_dc = nn.Parameter(visibility_dc.requires_grad_(True))
        self._visibility_rest = nn.Parameter(visibility_rest.requires_grad_(True))
        
        self._scaling = nn.Parameter(scales.requires_grad_(True))
        self._rotation = nn.Parameter(rots.requires_grad_(True))
        self._opacity = nn.Parameter(opacities.requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def training_setup(self, training_args):
        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")

        l = [
            {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
            {'params': [self._base_color], 'lr': training_args.feature_lr, "name": "base_color"},
            {'params': [self._metallic], 'lr': training_args.feature_lr, "name": "metallic"},
            {'params': [self._roughness], 'lr': training_args.feature_lr, "name": "roughness"},
            {'params': [self._ambient_dc], 'lr': training_args.feature_lr * 0.5, "name": "ambient_dc"},
            {'params': [self._ambient_rest], 'lr': training_args.feature_lr * 0.5 / 20.0, "name": "ambient_rest"},
            {'params': [self._visibility_dc], 'lr': 0.01, "name": "visibility_dc"},
            {'params': [self._visibility_rest], 'lr': 0.01 / 20.0, "name": "visibility_rest"},
            {'params': [self._opacity], 'lr': training_args.opacity_lr, "name": "opacity"},
            {'params': [self._scaling], 'lr': training_args.scaling_lr, "name": "scaling"},
            {'params': [self._rotation], 'lr': training_args.rotation_lr, "name": "rotation"},
            {'params': [self.sg_dir], 'lr': 0.0025, "name": "sg_dir"},
            {'params': [self.sg_sharp], 'lr': 0.01, "name": "sg_sharp"},
            {'params': [self.sg_color], 'lr': 0.01, "name": "sg_color"}
        ]

        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.position_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.position_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps)

    def update_learning_rate(self, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group['lr'] = lr
                return lr

    def construct_list_of_attributes(self):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        for i in range(self._base_color.shape[1]):
            l.append('base_color_{}'.format(i))
        l.append('metallic')
        for i in range(2):
            l.append('roughness_{}'.format(i))
        for i in range((self.max_sh_degree + 1)**2 * 3):
            l.append('ambient_{}'.format(i))
        for i in range((self.max_sh_degree + 1)**2 * 1):
            l.append('visibility_{}'.format(i))
        l.append('opacity')
        for i in range(self._scaling.shape[1]):
            l.append('scale_{}'.format(i))
        for i in range(self._rotation.shape[1]):
            l.append('rot_{}'.format(i))
        return l

    def save_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        base_color = self._base_color.detach().cpu().numpy()
        metallic = self._metallic.detach().cpu().numpy()
        roughness = self._roughness.detach().cpu().numpy()
        
        ambient_dc = self._ambient_dc.detach().cpu().numpy()
        ambient_rest = self._ambient_rest.detach().cpu().numpy()
        ambient = np.concatenate((ambient_dc, ambient_rest.reshape(ambient_rest.shape[0], -1)), axis=1)
        
        vis_dc = self._visibility_dc.detach().cpu().numpy()
        vis_rest = self._visibility_rest.detach().cpu().numpy()
        visibility = np.concatenate((vis_dc, vis_rest.reshape(vis_rest.shape[0], -1)), axis=1)
        
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()

        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes()]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        attributes = np.concatenate((xyz, normals, base_color, metallic, roughness, ambient, visibility, opacities, scale, rotation), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)

    def reset_opacity(self):
        opacities_new = self.inverse_opacity_activation(torch.min(self.get_opacity, torch.ones_like(self.get_opacity)*0.01))
        optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        self._opacity = optimizable_tensors["opacity"]

    def load_ply(self, path):
        plydata = PlyData.read(path)

        xyz = np.stack((np.asarray(plydata.elements[0]["x"]),
                        np.asarray(plydata.elements[0]["y"]),
                        np.asarray(plydata.elements[0]["z"])),  axis=1)
        opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

        # Read base color
        if "base_color_0" in plydata.elements[0]:
            base_color = np.zeros((xyz.shape[0], 3))
            base_color[:, 0] = np.asarray(plydata.elements[0]["base_color_0"])
            base_color[:, 1] = np.asarray(plydata.elements[0]["base_color_1"])
            base_color[:, 2] = np.asarray(plydata.elements[0]["base_color_2"])
        else:
            features_dc = np.zeros((xyz.shape[0], 3))
            features_dc[:, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
            features_dc[:, 1] = np.asarray(plydata.elements[0]["f_dc_1"])
            features_dc[:, 2] = np.asarray(plydata.elements[0]["f_dc_2"])
            rgb = np.clip(features_dc * 0.28209479177387814 + 0.5, 0.001, 0.999)
            base_color = np.log(rgb / (1.0 - rgb))

        scale_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("scale_")]
        scale_names = sorted(scale_names, key = lambda x: int(x.split('_')[-1]))
        scales = np.zeros((xyz.shape[0], len(scale_names)))
        for idx, attr_name in enumerate(scale_names):
            scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

        # Read metallic, roughness, and ambient (raw logits saved by save_ply)
        if "metallic" in plydata.elements[0]:
            metallic = np.asarray(plydata.elements[0]["metallic"])[..., np.newaxis]
        else:
            metallic = self.metallic_inverse_activation(torch.ones((xyz.shape[0], 1), device="cuda") * 0.1).cpu().numpy()

        if "roughness_0" in plydata.elements[0]:
            roughness = np.zeros((xyz.shape[0], 2))
            roughness[:, 0] = np.asarray(plydata.elements[0]["roughness_0"])
            roughness[:, 1] = np.asarray(plydata.elements[0]["roughness_1"])
        else:
            roughness = self.roughness_inverse_activation(torch.ones((xyz.shape[0], 2), device="cuda") * 0.5).cpu().numpy()

        if "ambient_0" in plydata.elements[0]:
            ambient = np.zeros((xyz.shape[0], 3))
            ambient[:, 0] = np.asarray(plydata.elements[0]["ambient_0"])
            ambient[:, 1] = np.asarray(plydata.elements[0]["ambient_1"])
            ambient[:, 2] = np.asarray(plydata.elements[0]["ambient_2"])
        else:
            ambient = self.ambient_inverse_activation(torch.ones((xyz.shape[0], 3), device="cuda") * 0.15).cpu().numpy()


        rot_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("rot")]
        rot_names = sorted(rot_names, key = lambda x: int(x.split('_')[-1]))
        rots = np.zeros((xyz.shape[0], len(rot_names)))
        for idx, attr_name in enumerate(rot_names):
            rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

        self._xyz = nn.Parameter(torch.tensor(xyz, dtype=torch.float, device="cuda").requires_grad_(True))
        self._base_color = nn.Parameter(torch.tensor(base_color, dtype=torch.float, device="cuda").requires_grad_(True))
        self._metallic = nn.Parameter(torch.tensor(metallic, dtype=torch.float, device="cuda").requires_grad_(True))
        self._roughness = nn.Parameter(torch.tensor(roughness, dtype=torch.float, device="cuda").requires_grad_(True))
        self._ambient = nn.Parameter(torch.tensor(ambient, dtype=torch.float, device="cuda").requires_grad_(True))

        
        self._opacity = nn.Parameter(torch.tensor(opacities, dtype=torch.float, device="cuda").requires_grad_(True))
        self._scaling = nn.Parameter(torch.tensor(scales, dtype=torch.float, device="cuda").requires_grad_(True))
        self._rotation = nn.Parameter(torch.tensor(rots, dtype=torch.float, device="cuda").requires_grad_(True))

        self.active_sh_degree = 0

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] in ["material_palette", "sg_dir", "sg_sharp", "sg_color"]:
                continue
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._base_color = optimizable_tensors["base_color"]
        self._metallic = optimizable_tensors["metallic"]
        self._roughness = optimizable_tensors["roughness"]
        self._ambient_dc = optimizable_tensors["ambient_dc"]
        self._ambient_rest = optimizable_tensors["ambient_rest"]
        self._visibility_dc = optimizable_tensors["visibility_dc"]
        self._visibility_rest = optimizable_tensors["visibility_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]

        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]

        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] in ["material_palette", "sg_dir", "sg_sharp", "sg_color"]:
                continue
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:

                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, new_base_color, new_metallic, new_roughness, new_ambient_dc, new_ambient_rest, new_visibility_dc, new_visibility_rest, new_opacities, new_scaling, new_rotation):
        d = {"xyz": new_xyz,
        "base_color": new_base_color,
        "metallic": new_metallic,
        "roughness": new_roughness,
        "ambient_dc": new_ambient_dc,
        "ambient_rest": new_ambient_rest,
        "visibility_dc": new_visibility_dc,
        "visibility_rest": new_visibility_rest,
        "opacity": new_opacities,
        "scaling" : new_scaling,
        "rotation" : new_rotation}

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._base_color = optimizable_tensors["base_color"]
        self._metallic = optimizable_tensors["metallic"]
        self._roughness = optimizable_tensors["roughness"]
        self._ambient_dc = optimizable_tensors["ambient_dc"]
        self._ambient_rest = optimizable_tensors["ambient_rest"]
        self._visibility_dc = optimizable_tensors["visibility_dc"]
        self._visibility_rest = optimizable_tensors["visibility_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]

        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def densify_and_split(self, grads, grad_threshold, scene_extent, N=2):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)

        stds = self.get_scaling[selected_pts_mask].repeat(N,1)
        stds = torch.cat([stds, 0 * torch.ones_like(stds[:,:1])], dim=-1)
        means = torch.zeros_like(stds)
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self._rotation[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        new_scaling = self.scaling_inverse_activation(self.get_scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
        new_rotation = self._rotation[selected_pts_mask].repeat(N,1)
        new_base_color = self._base_color[selected_pts_mask].repeat(N, 1)
        new_metallic = self._metallic[selected_pts_mask].repeat(N, 1)
        new_roughness = self._roughness[selected_pts_mask].repeat(N, 1)
        new_ambient_dc = self._ambient_dc[selected_pts_mask].repeat(N, 1)
        new_ambient_rest = self._ambient_rest[selected_pts_mask].repeat(N, 1, 1)
        new_visibility_dc = self._visibility_dc[selected_pts_mask].repeat(N, 1)
        new_visibility_rest = self._visibility_rest[selected_pts_mask].repeat(N, 1, 1)
        new_opacity = self._opacity[selected_pts_mask].repeat(N, 1)

        self.densification_postfix(new_xyz, new_base_color, new_metallic, new_roughness, new_ambient_dc, new_ambient_rest, new_visibility_dc, new_visibility_rest, new_opacity, new_scaling, new_rotation)

        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, scene_extent):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.where(torch.norm(grads, dim=-1) >= grad_threshold, True, False)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)
        
        new_xyz = self._xyz[selected_pts_mask]
        new_base_color = self._base_color[selected_pts_mask]
        new_metallic = self._metallic[selected_pts_mask]
        new_roughness = self._roughness[selected_pts_mask]
        new_ambient_dc = self._ambient_dc[selected_pts_mask]
        new_ambient_rest = self._ambient_rest[selected_pts_mask]
        new_visibility_dc = self._visibility_dc[selected_pts_mask]
        new_visibility_rest = self._visibility_rest[selected_pts_mask]
        new_opacities = self._opacity[selected_pts_mask]
        new_scaling = self._scaling[selected_pts_mask]
        new_rotation = self._rotation[selected_pts_mask]

        self.densification_postfix(new_xyz, new_base_color, new_metallic, new_roughness, new_ambient_dc, new_ambient_rest, new_visibility_dc, new_visibility_rest, new_opacities, new_scaling, new_rotation)


    def densify_and_prune(self, max_grad, min_opacity, extent, max_screen_size):
        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0

        self.densify_and_clone(grads, max_grad, extent)
        self.densify_and_split(grads, max_grad, extent)

        prune_mask = (self.get_opacity < min_opacity).squeeze()
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
        
        # Prune Gaussians with extreme aspect ratios (spiky needles)
        scales = self.get_scaling
        aspect_ratio = scales.max(dim=1).values / (scales.min(dim=1).values + 1e-6)
        extreme_aspect = aspect_ratio > 10.0
        prune_mask = torch.logical_or(prune_mask, extreme_aspect)

        self.prune_points(prune_mask)
        torch.cuda.empty_cache()

    def prune_floaters_and_large_scales(self, min_opacity=0.005, extent=None, max_aspect_ratio=None):
        prune_mask = (self.get_opacity < min_opacity).squeeze()
        if extent is not None:
            big_points_ws = self.get_scaling.max(dim=1).values > 0.2 * extent
            prune_mask = torch.logical_or(prune_mask, big_points_ws)
        if max_aspect_ratio is not None:
            scales = self.get_scaling
            aspect_ratio = scales.max(dim=1).values / (scales.min(dim=1).values + 1e-6)
            extreme_aspect = aspect_ratio > max_aspect_ratio
            prune_mask = torch.logical_or(prune_mask, extreme_aspect)
        if prune_mask.any():
            print(f"Pruned {prune_mask.sum().item()} floater/spiky Gaussians.")
            self.prune_points(prune_mask)
            torch.cuda.empty_cache()


    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter], dim=-1, keepdim=True)
        self.denom[update_filter] += 1
    def get_visibility(self):
        visibility_dc = self._visibility_dc
        visibility_rest = self._visibility_rest
        return torch.cat((visibility_dc, visibility_rest), dim=1)

    @torch.no_grad()
    def update_visibility(self, sample_num):
        from submodules.bvh import RayTracer
        from utils.graphics_utils import fibonacci_sphere_sampling
        def sample_incident_rays(normals, is_training=False, sample_num=24):
            if is_training:
                incident_dirs, incident_areas = fibonacci_sphere_sampling(normals, sample_num, random_rotate=True)
            else:
                incident_dirs, incident_areas = fibonacci_sphere_sampling(normals, sample_num, random_rotate=False)
            return incident_dirs, incident_areas

        raytracer = RayTracer(self.get_xyz, self.get_scaling, self.get_rotation)
        gaussians_xyz = self.get_xyz
        
        # 2DGS only has 2 scales, so we pad it to 3 to pass into BVH (actually it was patched in bvh/__init__.py, but we don't have covariance, bvh uses scale and rotation to build tree)
        # RayTracer trace_visibility expects: rays_o, rays_d, means3D, symm_inv, opacity, normals
        # For 2DGS, we don't have symm_inv (3D inverse covariance). We will pass a dummy tensor since bvh ray tracer can use it, or we will just use it as is if it accepts None.
        # Wait, the bvh trace.cu requires symm_inv to evaluate 3D Gaussian opacity!
        # Since 2DGS uses 2D splats, the BVH trace might not work perfectly without modification, but it's a good approximation.
        # Let's construct a dummy symm_inv (N, 6) from scaling and rotation.
        from utils.general_utils import build_scaling_rotation
        L = build_scaling_rotation(torch.cat([self.get_scaling, torch.zeros_like(self.get_scaling[:, :1])], dim=1), self.get_rotation)
        actual_covariance = L @ L.transpose(1, 2)
        symm = torch.zeros((gaussians_xyz.shape[0], 6), dtype=torch.float, device="cuda")
        symm[:, 0] = actual_covariance[:, 0, 0]
        symm[:, 1] = actual_covariance[:, 0, 1]
        symm[:, 2] = actual_covariance[:, 0, 2]
        symm[:, 3] = actual_covariance[:, 1, 1]
        symm[:, 4] = actual_covariance[:, 1, 2]
        symm[:, 5] = actual_covariance[:, 2, 2]
        # Invert it
        cov_inv = torch.inverse(actual_covariance + torch.eye(3, device="cuda").unsqueeze(0) * 1e-4)
        symm_inv = torch.zeros((gaussians_xyz.shape[0], 6), dtype=torch.float, device="cuda")
        symm_inv[:, 0] = cov_inv[:, 0, 0]
        symm_inv[:, 1] = cov_inv[:, 0, 1]
        symm_inv[:, 2] = cov_inv[:, 0, 2]
        symm_inv[:, 3] = cov_inv[:, 1, 1]
        symm_inv[:, 4] = cov_inv[:, 1, 2]
        symm_inv[:, 5] = cov_inv[:, 2, 2]

        gaussians_inverse_covariance = symm_inv

        gaussians_opacity = self.get_opacity[:, 0]
        
        # Normals for 2DGS
        from utils.general_utils import build_rotation
        rot = build_rotation(self.get_rotation)
        gaussians_normal = rot[:, :, 2] # The Z axis is the normal for 2D splats

        incident_visibility_results = []
        chunk_size = gaussians_xyz.shape[0] // ((sample_num - 1) // 24 + 1)
        if chunk_size == 0:
            chunk_size = gaussians_xyz.shape[0]

        from tqdm import tqdm
        for offset in tqdm(range(0, gaussians_xyz.shape[0], chunk_size), "Update visibility with raytracing."):
            end = min(offset + chunk_size, gaussians_xyz.shape[0])
            incident_dirs, incident_areas = sample_incident_rays(gaussians_normal[offset:end], False, sample_num)
            trace_results = raytracer.trace_visibility(
                gaussians_xyz[offset:end, None].expand_as(incident_dirs),
                incident_dirs,
                gaussians_xyz,
                gaussians_inverse_covariance,
                gaussians_opacity,
                gaussians_normal)
            incident_visibility = trace_results["visibility"]
            incident_visibility_results.append(incident_visibility)
        incident_visibility_result = torch.cat(incident_visibility_results, dim=0)
        self._visibility_tracing = incident_visibility_result

    def finetune_visibility(self, iterations=1000):
        from utils.sh_utils import eval_sh
        from submodules.bvh import RayTracer
        from tqdm import tqdm
        import torch.nn.functional as F

        visibility_sh_lr = 1e-2
        optimizer = torch.optim.Adam([
            {'params': [self._visibility_dc], 'lr': visibility_sh_lr},
            {'params': [self._visibility_rest], 'lr': visibility_sh_lr}
        ])
        means3D = self.get_xyz
        opacity = self.get_opacity[:, 0]
        scaling = self.get_scaling
        rotation = self.get_rotation
        
        from utils.general_utils import build_rotation
        rot = build_rotation(rotation)
        normal = rot[:, :, 2]
        
        from utils.general_utils import build_scaling_rotation
        L = build_scaling_rotation(torch.cat([scaling, torch.zeros_like(scaling[:, :1])], dim=1), rotation)
        actual_covariance = L @ L.transpose(1, 2)
        cov_inv = torch.inverse(actual_covariance + torch.eye(3, device="cuda").unsqueeze(0) * 1e-4)
        symm_inv = torch.zeros((means3D.shape[0], 6), dtype=torch.float, device="cuda")
        symm_inv[:, 0] = cov_inv[:, 0, 0]
        symm_inv[:, 1] = cov_inv[:, 0, 1]
        symm_inv[:, 2] = cov_inv[:, 0, 2]
        symm_inv[:, 3] = cov_inv[:, 1, 1]
        symm_inv[:, 4] = cov_inv[:, 1, 2]
        symm_inv[:, 5] = cov_inv[:, 2, 2]
        cov_inv_param = symm_inv

        tbar = tqdm(range(iterations), desc="Finetuning visibility shs")
        raytracer = RayTracer(means3D, scaling, rotation)
        visibility_shs_view = self.get_visibility().transpose(1, 2)
        vis_sh_degree = int(np.sqrt(visibility_shs_view.shape[-1])) - 1
        rays_o = means3D
        for iteration in tbar:
            rays_d = torch.randn_like(rays_o)
            rays_d = F.normalize(rays_d, dim=-1)
            mask = (rays_d * normal).sum(-1) < 0
            rays_d[mask] *= -1
            sample_sh2vis = eval_sh(vis_sh_degree, visibility_shs_view, rays_d)
            sample_vis = torch.clamp(sample_sh2vis + 0.5, 0.0, 1.0)
            trace_results = raytracer.trace_visibility(
                rays_o,
                rays_d,
                means3D,
                cov_inv_param,
                opacity,
                normal)
            visibility = trace_results["visibility"]
            loss = F.l1_loss(visibility, sample_vis)
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
