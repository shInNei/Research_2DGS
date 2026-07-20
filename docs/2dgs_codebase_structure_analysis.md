# 2D Gaussian Splatting (2DGS) Codebase Structure Analysis

This document provides a detailed breakdown of the official 2DGS codebase cloned from [hbb1/2d-gaussian-splatting](https://github.com/hbb1/2d-gaussian-splatting). It is designed to map the components of standard 2DGS and prepare for integrating the Anisotropic BRDF model.

---

## 1. Directory Tree Overview

Here are the main files and directories in the project:

```text
relightable2DGS/
├── train.py                     # Main training entry point
├── render.py                    # Rendering script for view synthesis
├── metrics.py                   # Quantitative metrics evaluator (PSNR, SSIM, LPIPS)
├── convert.py                   # COLMAP sparse reconstruction automation
├── view.py                      # Interactive real-time visualizer (Viser)
│
├── scene/                       # Data structures and scene representations
│   ├── gaussian_model.py        # GaussianModel parameter management
│   ├── cameras.py               # Camera intrinsics and extrinsics representation
│   ├── dataset_readers.py       # Reads COLMAP/NeRF synthetic datasets
│   └── colmap_loader.py         # Helper for parsing COLMAP binary exports
│
├── gaussian_renderer/           # Render pipeline & CUDA wrapper
│   └── __init__.py              # Defines the core render() interface
│
├── submodules/                  # CUDA-accelerated submodules
│   ├── diff-surfel-rasterization/ # 2D Surfel CUDA Rasterizer (2DGS core)
│   └── simple-knn/              # K-Nearest Neighbors calculator for scaling init
│
└── utils/                       # System, math, and rendering utilities
    ├── camera_utils.py          # Camera loading and setup helpers
    ├── graphics_utils.py        # Point cloud and projection helpers
    ├── loss_utils.py            # SSIM, L1, normal consistency, distortion losses
    └── sh_utils.py              # Spherical Harmonics (SH) math and evaluation
```

---

## 2. Detailed Breakdown of Key Modules

### A. Main Entry Scripts

*   **[train.py](file:///e:/Learning_material/relightable2DGS/train.py)**:
    *   **Purpose**: Manages the training loop from step 0 to 30,000 iterations.
    *   **Flow**:
        1. Parses hyperparameters (`ModelParams`, `PipelineParams`, `OptimizationParams`).
        2. Instantiates `GaussianModel` and prepares scene loaders.
        3. At each step: updates learning rates, calls [render](file:///e:/Learning_material/relightable2DGS/gaussian_renderer/__init__.py#L19) to get the rendered image, computes L1 and SSIM losses, runs backward pass, and steps the optimizer.
        4. Handles **densification and pruning** (cloning, splitting, and deleting Gaussians) between steps 500 and 15,000.
        5. Saves checkpoints and PLY files.
*   **[render.py](file:///e:/Learning_material/relightable2DGS/render.py)**:
    *   **Purpose**: Loads a trained model and renders images for all cameras (train/test splits) to output folders for validation.
*   **[metrics.py](file:///e:/Learning_material/relightable2DGS/metrics.py)**:
    *   **Purpose**: Computes standard metrics (PSNR, SSIM, LPIPS) comparing rendered images with the ground truth.

---

### B. Core Scene Representation

*   **[scene/gaussian_model.py](file:///e:/Learning_material/relightable2DGS/scene/gaussian_model.py)**:
    *   Contains the [GaussianModel](file:///e:/Learning_material/relightable2DGS/scene/gaussian_model.py#L24) class which defines and updates the learnable parameters of the 2D Gaussians.
    *   **Learnable Parameters**:
        *   `_xyz` (Positions: $N \times 3$)
        *   `_features_dc` (Base color Spherical Harmonics: $N \times 1 \times 3$)
        *   `_features_rest` (Higher-order Spherical Harmonics: $N \times 15 \times 3$)
        *   `_scaling` (Tangent scale factors: $N \times 2$ - representing major/minor axes $s_x, s_y$)
        *   `_rotation` (Rotation quaternions: $N \times 4$)
        *   `_opacity` (Opacity: $N \times 1$)
    *   **Key Functions**:
        *   `get_scaling()`: Applies exponential activation to `_scaling`.
        *   `get_rotation()`: Normalizes the `_rotation` quaternion.
        *   `save_ply()` / `load_ply()`: Serializes the Gaussians to PLY files.
        *   `densify_and_split()` / `densify_and_clone()`: Increases the density of Gaussians based on the gradient of positions.
*   **[scene/cameras.py](file:///e:/Learning_material/relightable2DGS/scene/cameras.py)**:
    *   Defines the `Camera` class, representing viewpoint coordinates, projection matrices, FoV, and image width/height.

---

### C. Rendering Wrapper

*   **[gaussian_renderer/__init__.py](file:///e:/Learning_material/relightable2DGS/gaussian_renderer/__init__.py)**:
    *   Defines the [render](file:///e:/Learning_material/relightable2DGS/gaussian_renderer/__init__.py#L19) function which wraps the CUDA rasterizer.
    *   **2DGS Specific Extensions**:
        *   Unlike 3DGS, the rasterizer returns a multi-channel map (`allmap`) containing:
            *   `render_alpha`: Rendered opacity map.
            *   `render_normal`: Rendered normal map.
            *   `render_depth_median` / `render_depth_expected`: Rendered depth maps.
            *   `render_dist`: Depth distortion map (used to reduce depth artifacts).
        *   Computes pseudo surface normals (`surf_normal`) from the depth map using [depth_to_normal](file:///e:/Learning_material/relightable2DGS/utils/point_utils.py) and applies a **normal consistency loss** between the rasterized normals and pseudo normals.

---

### D. CUDA Submodules

*   **[submodules/diff-surfel-rasterization/](file:///e:/Learning_material/relightable2DGS/submodules/diff-surfel-rasterization)**:
    *   **Purpose**: The CUDA rendering engine of 2DGS.
    *   **How it works**: It treats each Gaussian as a flat 2D surfel. It projects these surfels onto the image plane, performs ray-splat intersection to compute accurate depths and normals, and blends them using front-to-back alpha compositing.

---

## 3. Designing Code Modifications for Anisotropic PBR

To implement Phase 1 & 2 (Anisotropic GGX PBR model via Deferred Shading), we will need to modify the following components:

### 1. Update [scene/gaussian_model.py](file:///e:/Learning_material/relightable2DGS/scene/gaussian_model.py)
*   **Modify Parameters**:
    *   Remove `_features_dc` and `_features_rest` (Spherical Harmonics).
    *   Add:
        *   `_base_color` (Albedo: $N \times 3$, activated with sigmoid to stay in $[0, 1]$).
        *   `_metallic` (Metallic: $N \times 1$, activated with sigmoid).
        *   `_roughness` (Roughness X & Y: $N \times 2$, activated with sigmoid or clamped exponential to stay in $(0, 1]$).
*   **Modify save/load**:
    *   Update `save_ply()` and `load_ply()` to write/read `base_color`, `metallic`, and `roughness_x, roughness_y` properties.

### 2. Update [gaussian_renderer/__init__.py](file:///e:/Learning_material/relightable2DGS/gaussian_renderer/__init__.py)
*   Instead of calling `rasterizer()` directly to get a shaded RGB image (which was blended using SH), we will use the rasterizer to output G-buffers:
    1. **Albedo Map** (rasterizing the `_base_color` parameter).
    2. **Normal Map** (the analytical normals `render_normal` already returned by the 2DGS rasterizer).
    3. **Material Map** (rasterizing the `_roughness` and `_metallic` parameters).
*   Implement the **Anisotropic GGX Shading Function** in Python:
    *   Compute the tangent vectors $t_x, t_y$ and normal $n$ from the viewpoint camera's orientation.
    *   Compute the PBR shading using the projected vectors under the chosen light source (e.g. a point light or Spherical Harmonics/Spherical Gaussians environment map).
    *   Blend the G-buffers to produce the final rendered image.

### 3. Update [train.py](file:///e:/Learning_material/relightable2DGS/train.py)
*   Update loss calculation to compare the PBR shaded images against the training images.
*   Update optimizer setup to tune the learning rates for `base_color`, `metallic`, and `roughness` parameters.
