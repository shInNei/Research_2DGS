# Implementation Plan: HDR Exposure Adjustment & Floater Speck Removal

Fix overexposure in HDR environment map relighting and eliminate floater specks/artifacts in 360° trajectory material videos.

## User Review Required

> [!IMPORTANT]
> - **Exposure Tone Mapping:** High-dynamic HDR maps (like `city.hdr` with direct sunlight > 50.0) cause overexposure. We will implement automatic Reinhard tone mapping and dynamic luminance scaling (`--exposure`, default `0.25`) in `utils/light_utils.py` so relighted images match realistic PBR exposure levels.
> - **Floater Speck Removal:** Material trajectory videos (`normal`, `roughness`, `metallic`) will apply alpha thresholding (`opacity > 0.05` & `rend_alpha > 0.1`) in `utils/mesh_utils.py` to mask out background noise and low-opacity lơ lửng specks completely.

## Proposed Changes

---

### Component 1: HDR Exposure Scaling & Tone Mapping

#### [MODIFY] [light_utils.py](file:///e:/Learning_material/relightable2DGS/utils/light_utils.py)
- Update `load_hdr_as_sg(hdr_path, exposure=0.25, num_sg=128)`:
  - Add auto-luminance normalization using 95th percentile clipping and Reinhard tone mapping scaling:
    $$C_{scaled} = \frac{C \cdot \text{scale}}{1.0 + C \cdot \text{scale}}$$
  - Prevents overexposure/whiteout under bright HDR environment maps like `city.hdr`.

#### [MODIFY] [render_relight.py](file:///e:/Learning_material/relightable2DGS/render_relight.py)
- Add `--exposure` command line parameter (default `0.25`).
- Pass exposure parameter to `load_hdr_as_sg`.

---

### Component 2: Floater Speck Removal & Background Alpha Masking

#### [MODIFY] [mesh_utils.py](file:///e:/Learning_material/relightable2DGS/utils/mesh_utils.py)
- Update `GaussianExtractor.reconstruction()`:
  - Apply alpha masking using `rend_alpha > 0.05` to `normal`, `roughness`, `metallic` maps.
  - Zero out low-opacity floater Gaussians ($\text{opacity} < 0.02$) during material map extraction.
  - Clean background pixels to produce crisp, artifact-free material videos in `render_traj_combined_grid.mp4`.

---

## Verification Plan

### Manual Verification
1. Run `python render.py -m output/tensoir_lego --light_type colocated` to verify that `render_traj_combined_grid.mp4` shows clean, artifact-free material maps with zero floater specks.
2. Run `python render_relight.py -m output/tensoir_lego --hdr_path data/eval_lights/city.hdr --exposure 0.25` to verify that relighted images under `city.hdr` have rich, non-overexposed color balance.
