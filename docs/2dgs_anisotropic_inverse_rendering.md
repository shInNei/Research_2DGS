# Research Exploration: Anisotropic BRDF in 2D Gaussian Splatting for Inverse Rendering

This document explores the potential research direction of integrating **Anisotropic BRDF** (e.g., Anisotropic GGX) into **2D Gaussian Splatting (2DGS)** for physically-based inverse rendering and relighting.

---

## 1. Methodology & Mathematical Formulation (Toán học / Kiến trúc)

### How 2DGS Naturally Supports Tangent Space
A major limitation of 3D Gaussian Splatting (3DGS) is that the primitives are 3D ellipsoids. While normals can be approximated (e.g., by regularizing the shortest axis of the ellipsoid or using depth-based normal estimation), there is no true, geometrically consistent local tangent frame.

In contrast, **2D Gaussian Splatting (2DGS)** defines each primitive as a flat, 2D oriented planar disk.
A 2D Gaussian $k$ is parameterized by:
- Center position $x_k \in \mathbb{R}^3$
- Scale parameters along two local axes $S_k = (s_x, s_y) \in \mathbb{R}^2$
- Rotation matrix $R_k \in SO(3)$ (represented by a quaternion $q_k$)

The rotation matrix $R_k$ maps the local 2D coordinate space $(u, v)$ to the 3D world space. The three columns of $R_k$ correspond directly to the local coordinate axes:
$$R_k = \begin{bmatrix} | & | & | \\ t_x & t_y & n \\ | & | & | \end{bmatrix}$$
Where:
- $t_x = R_k[:, 0]$ is the tangent vector (corresponding to the local $u$-axis, aligned with scale $s_x$).
- $t_y = R_k[:, 1]$ is the bitangent vector (corresponding to the local $v$-axis, aligned with scale $s_y$).
- $n = R_k[:, 2] = t_x \times t_y$ is the mathematically exact surface normal vector of the 2D Gaussian.

This is a powerful property: **2DGS possesses an analytical, geometrically defined local tangent space for each primitive.** This makes it uniquely suited for anisotropic BRDF modeling, as we do not need to construct arbitrary, non-differentiable tangent vectors!

### Anisotropic GGX Formulation on 2D Gaussians
To represent the BRDF of each 2D Gaussian, we assign the following learnable parameters per Gaussian:
1. **Albedo / Base Color** $a \in \mathbb{R}^3$
2. **Metallic** $m \in [0, 1]$
3. **Anisotropic Roughness** $(\alpha_x, \alpha_y) \in (0, 1]^2$ aligned with $t_x$ and $t_y$.

For a view direction $v$ and light direction $l$ (both normalized and in world space), we compute the halfway vector $h = \frac{v + l}{\|v + l\|}$.
To calculate the anisotropic GGX BRDF, we project the vectors into the local tangent space of the 2D Gaussian:
- $h_x = h \cdot t_x$, $h_y = h \cdot t_y$, $h_z = h \cdot n$
- $v_x = v \cdot t_x$, $v_y = v \cdot t_y$, $v_z = v \cdot n$
- $l_x = l \cdot t_x$, $l_y = l \cdot t_y$, $l_z = l \cdot n$

#### 1. Anisotropic Normal Distribution Function (NDF) $D(h)$
The probability distribution of microfacet normals is:
$$D(h) = \frac{\chi^+(h \cdot n)}{\pi \alpha_x \alpha_y \left( \frac{(h \cdot t_x)^2}{\alpha_x^2} + \frac{(h \cdot t_y)^2}{\alpha_y^2} + (h \cdot n)^2 \right)^2}$$
Where $\chi^+(x)$ is the Heaviside step function (1 if $x > 0$, else 0).

#### 2. Height-Correlated Anisotropic Smith Masking-Shadowing Function $G_2(v, l)$
Using the height-correlated Smith model (critical for energy conservation in PBR):
$$G_2(v, l) = \frac{\chi^+(v \cdot n) \chi^+(l \cdot n)}{1 + \Lambda(v) + \Lambda(l)}$$
Where the anisotropic lambda function $\Lambda(\omega)$ for a direction $\omega \in \{v, l\}$ is:
$$\Lambda(\omega) = \frac{-1 + \sqrt{1 + \frac{\alpha_x^2 (\omega \cdot t_x)^2 + \alpha_y^2 (\omega \cdot t_y)^2}{(\omega \cdot n)^2}}}{2}$$

#### 3. Fresnel Term $F(v, h)$
Using Schlick's approximation:
$$F(v, h) = F_0 + (1 - F_0) (1 - v \cdot h)^5$$
Where $F_0 = 0.04 (1 - m) + a \cdot m$.

#### 4. Combined BRDF
$$f(v, l) = \frac{a}{\pi}(1 - m) + \frac{D(h) G_2(v, l) F(v, h)}{4 (v \cdot n)(l \cdot n)}$$

---

## 2. Effectiveness (Độ chính xác)

- **Novel Views & Relighting on Complex Materials:** Parameterizing roughness via $(\alpha_x, \alpha_y)$ on 2D surfels will significantly improve rendering quality (PSNR, SSIM, LPIPS) for anisotropic materials such as brushed metals (cookware, appliances), fabrics (satin, silk, velvet), hair, and carbon fiber.
- **Why Isotropic Fails:** Isotropic BRDFs assume light reflects symmetrically around the normal, producing circular specular highlights. For brushed surfaces, microscopic scratches stretch the specular highlights into lines or lobes perpendicular to the scratches. Anisotropic GGX captures this by scaling roughness differently along $t_x$ and $t_y$.
- **Disentanglement:** Using the true tangent space of 2DGS prevents the optimization from "faking" anisotropic highlights by stretching the shape of the Gaussians themselves (which would distort the underlying geometry). It separates the **spatial shape** of the Gaussians (geometric scales $s_x, s_y$) from the **appearance reflectance** (material roughness $\alpha_x, \alpha_y$).

---

## 3. Efficiency (Hiệu năng & Chi phí tính toán)

- **VRAM and Storage:**
  - Standard 3D/2DGS uses Spherical Harmonics (SH) to represent view-dependent appearance (often degree 3, requiring $3 \times 16 = 48$ coefficients for color).
  - Anisotropic BRDF replaces SH with: Albedo (3), Metallic (1), Roughness (2: $\alpha_x, \alpha_y$). This is **6 parameters** total for appearance, which is **significantly lower** than 48 floats!
  - Therefore, storage and VRAM footprint for the Gaussians will **decrease** compared to SH-based splats.
- **Training and Rendering Overhead:**
  - Evaluating the BRDF equations requires trigonometric/square root operations in PyTorch/CUDA. However, this is done in local coordinates, which are fast.
  - **Deferred Shading Approach:** If using deferred shading (rasterizing Normals, Albedo, Roughness, Metallic to 2D G-buffers, then shading per pixel on the screen), the overhead is extremely low. This can easily run at **100+ FPS** and is perfectly suitable for a **4GB GPU** during rendering.
  - **Ray Tracing Approach:** If using differentiable ray tracing (for inter-reflections/global illumination, like IRGS/IRGS++), training will be computationally heavier. However, IRGS++ uses mesh-based ray tracing or accelerated MC integration to run efficiently on standard consumer GPUs.

---

## 4. Recommended Baseline Codebases

Here is a comparison of potential baselines:

| Baseline | Link | Description | Pros | Cons |
| :--- | :--- | :--- | :--- | :--- |
| **2D-GS (Official)** | [surreal-graphics/2d-gaussian-splatting](https://github.com/surreal-graphics/2d-gaussian-splatting) | The official 2DGS implementation (SIGGRAPH 2024). | Cleanest geometric base; exact normals and tangent plane coordinates. | Only outputs Radiance (SH); no built-in PBR shading or relighting pipeline. |
| **IRGS** | [fudan-zvg/IRGS](https://github.com/fudan-zvg/IRGS) | Inter-Reflective Gaussian Splatting (CVPR 2025). | The most advanced 2DGS-based inverse rendering framework; handles indirect light. | Complex codebase; requires more GPU power for ray-tracing optimization. |
| **Relightable3DGaussian** | [NJU-3DV/Relightable3DGaussian](https://github.com/NJU-3DV/Relightable3DGaussian) | Relightable 3D Gaussians (ECCV 2024). | Well-documented PBR inverse rendering pipeline (uses isotropic GGX). | Based on 3DGS, so normals and tangent frames are coarse/noisy. |

---

## 5. Suggested Research Roadmap

To execute this research successfully:
1. **Phase 1: Basic Integration (Deferred Shading)**
   - Start with [surreal-graphics/2d-gaussian-splatting](https://github.com/surreal-graphics/2d-gaussian-splatting).
   - Modify the Gaussian attributes to store `base_color` (3), `metallic` (1), and `roughness_x, roughness_y` (2) instead of SH.
   - Implement a deferred shading rasterizer that outputs Normals, Albedo, and (Metallic, Roughness_x, Roughness_y) to G-buffers, then applies anisotropic GGX shading under a single point light or environment map.
2. **Phase 2: Advanced Illumination (Relighting)**
   - Integrate environment map lighting using Spherical Harmonics or Spherical Gaussians (SG) to approximate incoming light.
   - Run tests on datasets with highly specular/brushed materials (e.g., Shiny Blender dataset) to evaluate PSNR/SSIM improvements.
3. **Phase 3: Global Illumination (Optional)**
   - **What is IRGS?** [IRGS (Inter-Reflective Gaussian Splatting)](https://github.com/fudan-zvg/IRGS) is a SOTA inverse rendering framework built on 2DGS. It uses a custom differentiable 2D Gaussian Ray Tracer to compute **indirect illumination** (light bouncing between different surfaces in the scene, which is crucial for highly reflective/metallic materials).
   - **What does "porting" mean here?** It means taking the proposed Anisotropic GGX BRDF formulation and replacing the default isotropic GGX BRDF inside the IRGS rendering/optimization pipeline.
   - **Why do this?** This allows you to evaluate the combination of anisotropic material modeling with full global illumination. Highly anisotropic materials (like brushed metal cups or bowls) exhibit strong inter-reflections. By replacing their BRDF module, you can evaluate whether anisotropic modeling helps resolve complex indirect light transport and achieves better decomposition accuracy on real-world shiny surfaces.
