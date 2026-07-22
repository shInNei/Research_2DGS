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

import os
import random
import json
import torch
from utils.system_utils import searchForMaxIteration
from scene.dataset_readers import sceneLoadTypeCallbacks
from scene.gaussian_model import GaussianModel
from arguments import ModelParams
from utils.camera_utils import cameraList_from_camInfos, camera_to_JSON

class Scene:

    gaussians : GaussianModel

    def __init__(self, args : ModelParams, gaussians : GaussianModel, load_iteration=None, shuffle=True, resolution_scales=[1.0]):
        """b
        :param path: Path to colmap scene main folder.
        """
        self.model_path = args.model_path
        self.loaded_iter = None
        self.gaussians = gaussians

        if load_iteration:
            if load_iteration == -1:
                self.loaded_iter = searchForMaxIteration(os.path.join(self.model_path, "point_cloud"))
            else:
                self.loaded_iter = load_iteration
            print("Loading trained model at iteration {}".format(self.loaded_iter))

        self.train_cameras = {}
        self.test_cameras = {}

        # Check if the folder contains 'train' with subfolders like 'train_000', 'train_001', etc.
        # or if the source_path itself has subfolders representing views.
        is_tensoir = False
        train_path = os.path.join(args.source_path, "train")
        check_path = train_path if os.path.exists(train_path) else args.source_path
        if os.path.exists(check_path):
            subdirs = [d for d in os.listdir(check_path) if os.path.isdir(os.path.join(check_path, d)) and ('train' in d or d.isdigit())]
            if len(subdirs) > 0:
                is_tensoir = True

        if os.path.exists(os.path.join(args.source_path, "sparse")):
            scene_info = sceneLoadTypeCallbacks["Colmap"](args.source_path, args.images, args.eval)
        elif is_tensoir:
            print("Detected TensoIR data structure!")
            scene_info = sceneLoadTypeCallbacks["TensoIR"](args.source_path, args.white_background, args.eval, getattr(args, 'eval_light_name', None))
        elif os.path.exists(os.path.join(args.source_path, "transforms_train.json")):
            print("Found transforms_train.json file, assuming Blender data set!")
            scene_info = sceneLoadTypeCallbacks["Blender"](args.source_path, args.white_background, args.eval)
        else:
            assert False, "Could not recognize scene type!"

        if not self.loaded_iter:
            with open(scene_info.ply_path, 'rb') as src_file, open(os.path.join(self.model_path, "input.ply") , 'wb') as dest_file:
                dest_file.write(src_file.read())
            json_cams = []
            camlist = []
            if scene_info.test_cameras:
                camlist.extend(scene_info.test_cameras)
            if scene_info.train_cameras:
                camlist.extend(scene_info.train_cameras)
            for id, cam in enumerate(camlist):
                json_cams.append(camera_to_JSON(id, cam))
            with open(os.path.join(self.model_path, "cameras.json"), 'w') as file:
                json.dump(json_cams, file)

        if shuffle:
            random.shuffle(scene_info.train_cameras)  # Multi-res consistent random shuffling
            random.shuffle(scene_info.test_cameras)  # Multi-res consistent random shuffling

        self.cameras_extent = scene_info.nerf_normalization["radius"]

        for resolution_scale in resolution_scales:
            print("Loading Training Cameras")
            self.train_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.train_cameras, resolution_scale, args)
            print("Loading Test Cameras")
            self.test_cameras[resolution_scale] = cameraList_from_camInfos(scene_info.test_cameras, resolution_scale, args)
        
        if self.loaded_iter:
            point_cloud_dir = os.path.join(self.model_path, "point_cloud", "iteration_" + str(self.loaded_iter))
            self.gaussians.load_ply(os.path.join(point_cloud_dir, "point_cloud.ply"))
            sg_params_path = os.path.join(point_cloud_dir, "sg_params.pth")
            if os.path.exists(sg_params_path):
                sg_data = torch.load(sg_params_path)
                self.gaussians.sg_dir.data.copy_(sg_data["sg_dir"])
                self.gaussians.sg_sharp.data.copy_(sg_data["sg_sharp"])
                self.gaussians.sg_color.data.copy_(sg_data["sg_color"])
            material_palette_path = os.path.join(point_cloud_dir, "material_palette.pth")
            if hasattr(self.gaussians, 'material_palette') and os.path.exists(material_palette_path):
                self.gaussians.material_palette.data.copy_(torch.load(material_palette_path))
        else:
            self.gaussians.create_from_pcd(scene_info.point_cloud, self.cameras_extent)

    def save(self, iteration):
        point_cloud_path = os.path.join(self.model_path, "point_cloud/iteration_{}".format(iteration))
        self.gaussians.save_ply(os.path.join(point_cloud_path, "point_cloud.ply"))
        # Save environment SG map
        torch.save({
            "sg_dir": self.gaussians.sg_dir,
            "sg_sharp": self.gaussians.sg_sharp,
            "sg_color": self.gaussians.sg_color
        }, os.path.join(point_cloud_path, "sg_params.pth"))
        if hasattr(self.gaussians, 'material_palette'):
            torch.save(self.gaussians.material_palette, os.path.join(point_cloud_path, "material_palette.pth"))

    def getTrainCameras(self, scale=1.0):
        return self.train_cameras[scale]

    def getTestCameras(self, scale=1.0):
        return self.test_cameras[scale]