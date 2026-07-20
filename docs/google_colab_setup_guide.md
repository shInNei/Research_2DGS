# Google Colab Setup & Dataset Training Guide

This guide explains how to push your modified repository to GitHub, set up training on Google Colab (using the free NVIDIA T4 GPU), download the datasets, and start the training process.

---

## 1. Pushing Code to Your GitHub Repository

Since Google Colab needs to clone your modified codebase, you should host it on your own GitHub account:

1. Create a new **blank repository** on GitHub (do not initialize with a README or LICENSE).
2. Open your local terminal in the `relightable2DGS` folder and run:
   ```bash
   # Remove the original git remote pointing to the official repo
   git remote remove origin
   
   # Add your own repository as the new remote
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
   
   # Rename the default branch to main and commit changes
   git branch -M main
   git add .
   git commit -m "Implement Anisotropic GGX PBR model in 2DGS"
   
   # Push your code (including commit histories)
   git push -u origin main
   ```

---

## 2. Google Colab Notebook Steps

Once your code is on GitHub, create a new Google Colab notebook, change the runtime type to **T4 GPU**, and create the following code cells.

### Cell 1: Environment Verification
Check if the GPU is active and CUDA compiler is available:
```bash
!nvidia-smi
!nvcc --version
```

### Cell 2: Clone Your Repository (Recursive)
Clone the repository, making sure to fetch the C++ submodules recursively:
```bash
# Replace with your actual GitHub repository URL
!git clone --recursive https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git /content/Research_2DGS
```

### Cell 3: Install Dependencies & Compile Submodules
Install the necessary python packages, **patch the CUB/FLT_MAX issue**, and build the custom CUDA submodules:
```bash
%cd /content/Research_2DGS
!pip install plyfile opencv-python lpips trimesh open3d tqdm mediapy

# Patch simple-knn compiling issue (FLT_MAX not found on modern compilers)
!python -c "with open('submodules/simple-knn/simple_knn.cu', 'r+') as f: c = f.read(); f.seek(0); f.write('#include <cfloat>\n' + c)"

# Compile CUDA rasterizer and KNN submodules
!pip install submodules/diff-surfel-rasterization
!pip install submodules/simple-knn
```

### Cell 4: Download Training Datasets (High-Speed GCS Mirror)
We recommend using Google's official high-speed direct download links. They are extremely stable and will not hit rate limits:
```bash
%cd /content/Research_2DGS
# Create data folder
!mkdir -p data
%cd data

# Option A: Download Shiny Blender Dataset (Ref-NeRF) - Highly Recommended for Anisotropic/Specularity
!wget https://storage.googleapis.com/gresearch/refraw360/ref.zip
!unzip -q ref.zip
!rm ref.zip

# Option B: Download Standard NeRF Synthetic Dataset (Lego, Drums, etc.)
!wget https://storage.googleapis.com/gresearch/nerf/nerf_synthetic.zip
!unzip -q nerf_synthetic.zip -d nerf_synthetic
!rm nerf_synthetic.zip

%cd /content/Research_2DGS
```

### Cell 5: Run Training
Start training on a selected scene (e.g. `helmet` from Shiny Blender, or `lego` from NeRF Synthetic):
```bash
%cd /content/Research_2DGS
# Train on Shiny Blender "helmet" scene (extracted to 'data/refnerf/helmet')
!python train.py -s data/refnerf/helmet --model_path output/shiny_blender_helmet --eval

# OR: Train on standard NeRF Synthetic "lego" scene
# !python train.py -s data/nerf_synthetic/lego --model_path output/nerf_synthetic_lego --eval
```

### Cell 6: Mount Google Drive & Backup Outputs
Run this to mount Google Drive and back up the training results (`output/` folder) to prevent data loss:
```python
# 1. Mount Google Drive
from google.colab import drive
drive.mount('/content/drive')
```
```bash
# 2. Copy the training outputs to Google Drive
!mkdir -p /content/drive/MyDrive/Research_2DGS_outputs/
!cp -r /content/Research_2DGS/output/* /content/drive/MyDrive/Research_2DGS_outputs/
```

---

## 3. Recommended Research Datasets

For testing Inverse Rendering, Relighting, and Anisotropic specular highlights, the following datasets are standard:

### A. Shiny Blender (Ref-NeRF)
*   **Best for**: Highly reflective, specular, or anisotropic surfaces (metals, glossy plastics).
*   **Scenes**: `helmet`, `car`, `coffee`, `toaster`, `ball`, `teapot`.
*   **Features**: These objects exhibit complex specular reflections and view-dependent highlights under a single light source, making them ideal to evaluate Anisotropic GGX vs. Isotropic GGX.
*   **Download Link**: [https://storage.googleapis.com/gresearch/refraw360/ref.zip](https://storage.googleapis.com/gresearch/refraw360/ref.zip)

### B. TensoIR Synthetic
*   **Best for**: Inverse rendering evaluations (extracting pure albedo, metallic, roughness under complex environment maps).
*   **Scenes**: `lego`, `ficus`, `hotdog`, `armadillo`.
*   **Features**: Includes environment maps used to light the synthetic scenes.
*   **Download Link**: Available via instructions in the [Facebook Research TensoIR repository](https://github.com/facebookresearch/TensoIR).

### C. Standard NeRF Synthetic
*   **Best for**: General 3D scene reconstruction and geometry verification.
*   **Scenes**: `lego`, `drums`, `ship` (contain glossy components), `materials` (contains standard sphere shapes with varying roughness).
*   **Download Link**: [https://storage.googleapis.com/gresearch/nerf/nerf_synthetic.zip](https://storage.googleapis.com/gresearch/nerf/nerf_synthetic.zip)
