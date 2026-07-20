# Comparative Analysis & Evaluation Plan

This document outlines the research plan for comparing our **Anisotropic PBR 2DGS** model against current State-of-the-Art (SOTA) inverse rendering and relighting pipelines.

---

## 1. Selected Baselines for Comparison

To demonstrate the scientific value of this work, we will compare our pipeline against three classes of baselines:

### A. Non-Relightable Geometry Baselines (Sanity Check)
*   **Vanilla 2DGS (hbb1/2d-gaussian-splatting)**: The base geometry pipeline.
*   **Vanilla 3DGS (Inria)**: The standard 3D Gaussian Splatting pipeline.
*   *Purpose*: Prove that replacing Spherical Harmonics (SH) with physical material properties does not degrade the novel view synthesis quality under the original lighting conditions.

### B. Isotropic PBR Relighting Baselines (Our Core Novelty Evaluation)
*   **Isotropic 2DGS (Ours - Ablated)**: Restricting our model to a single roughness parameter ($\alpha_x = \alpha_y$).
*   **Relightable 3DGS (R3DG) / GS-IR (CVPR 2024)**: Popular 3DGS inverse rendering pipelines that use Isotropic GGX shading.
*   **DeferredGS / RGS-DR (2025/2026)**: Current State-of-the-Art surfel-based (2DGS) PBR relighting pipelines that render G-buffers for deferred shading but are limited to Isotropic BRDFs.
*   *Purpose*: Directly validate the hypothesis that **anisotropic parameterization ($\alpha_x, \alpha_y$) in the 2D tangent space** is necessary to accurately reconstruct specular highlights on complex, brushed, or directional materials, which is currently unsupported by SOTA 2025/2026 pipelines.

### C. Geometry-Guided Inverse Rendering Baselines (SOTA Comparison)
*   **GeoSplatting (ICCV 2025)**: SOTA 3DGS pipeline that utilizes optimized mesh-guided normals to solve the "noisy normal" issue in 3DGS.
*   *Purpose*: Demonstrate that our 2DGS foundation naturally yields superior geometric normals and tangent fields compared to 3DGS, matching or exceeding GeoSplatting's quality without needing explicit mesh guidance.

---

## 2. Evaluation Metrics

We will evaluate the models using three sets of quantitative metrics:

| Evaluation Area | Metric | Formula / Description |
| :--- | :--- | :--- |
| **Novel View Synthesis** | **PSNR** (dB) <br> **SSIM** <br> **LPIPS** | Measure the visual quality of rendered novel views against ground truth test images. |
| **Material Accuracy** | **Albedo L1** <br> **Roughness L1** <br> **Metallic L1** | Compare the optimized parameter maps against the ground-truth maps available in synthetic datasets. |
| **Geometry Quality** | **Normal MAE** (Mean Angular Error) | Measure the angular difference (in degrees) between the estimated normals and the true normals. |

---

## 3. Evaluation Datasets

We will use two standard academic benchmarks:

### A. Shiny Blender (Ref-NeRF)
*   **Scenes**: `helmet`, `car`, `coffee`, `toaster`, `teapot`, `ball`.
*   **Why**: This dataset is specifically designed for highly specular and metallic objects. It contains public ground truth meshes and camera files. It is the gold standard for testing specularity.

### B. TensoIR Synthetic
*   **Scenes**: `lego`, `ficus`, `hotdog`, `armadillo`.
*   **Why**: Features multiple environmental maps, making it ideal for evaluating multi-light relighting capabilities.

---

## 4. Execution Workflow on Google Colab

To collect these comparison metrics:

1.  **Train Baselines**:
    *   Clone official implementations of `GS-IR` or run our ablated `Isotropic 2DGS` on Google Colab using the provided `colab_setup.ipynb`.
2.  **Evaluate metrics**:
    *   Run `metrics.py` (already in the 2DGS codebase) to compute test PSNR, SSIM, and LPIPS for each model:
        ```bash
        python metrics.py -m output/shiny_blender_helmet
        ```
3.  **Export Material Maps**:
    *   Save render channels (`albedo`, `roughness_x`, `roughness_y`, `metallic`, `normal`) using the updated `render.py`.
