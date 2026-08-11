import json

with open('SVG_IR_TensoIR_Lego_Colab.ipynb', 'r', encoding='utf-8') as f:
    d = json.load(f)

source = d['cells'][2]['source']

patch_idx = -1
for i, line in enumerate(source):
    if '# Install python dependencies' in line:
        patch_idx = i
        break

if patch_idx != -1:
    patch_lines = [
        "# Patch 3: Fix Colab RAM OOM (float64 -> float32)\n",
        "utils_file = '/content/SVG-IR/scene/utils.py'\n",
        "with open(utils_file, 'r') as f:\n",
        "    t = f.read()\n",
        "t = t.replace('img = img / 255', 'img = (img / 255.0).astype(np.float32)')\n",
        "with open(utils_file, 'w') as f:\n",
        "    f.write(t)\n",
        "\n",
        "dr_file = '/content/SVG-IR/scene/dataset_readers.py'\n",
        "with open(dr_file, 'r') as f:\n",
        "    t = f.read()\n",
        "t = t.replace('np.ones_like(image[..., 0])', 'np.ones_like(image[..., 0], dtype=np.float32)')\n",
        "t = t.replace('bg = np.array([1, 1, 1]) if white_background else np.array([0, 0, 0])', 'bg = np.array([1, 1, 1], dtype=np.float32) if white_background else np.array([0, 0, 0], dtype=np.float32)')\n",
        "with open(dr_file, 'w') as f:\n",
        "    f.write(t)\n",
        "print('✓ Patched SVG-IR for float32 memory optimization')\n",
        "\n",
        "# Patch 4: Fix PyTorch 2.6+ weights_only=False error in torch.load\n",
        "import glob\n",
        "for py_file in glob.glob('/content/SVG-IR/**/*.py', recursive=True):\n",
        "    with open(py_file, 'r', encoding='utf-8', errors='ignore') as f:\n",
        "        content = f.read()\n",
        "    if 'torch.load(' in content:\n",
        "        new_content = content.replace('torch.load(checkpoint_path)', 'torch.load(checkpoint_path, weights_only=False)')\n",
        "        new_content = new_content.replace('torch.load(checkpoint)', 'torch.load(checkpoint, weights_only=False)')\n",
        "        new_content = new_content.replace('torch.load(sg_params_path)', 'torch.load(sg_params_path, weights_only=False)')\n",
        "        new_content = new_content.replace('torch.load(material_palette_path)', 'torch.load(material_palette_path, weights_only=False)')\n",
        "        if new_content != content:\n",
        "            with open(py_file, 'w', encoding='utf-8') as f:\n",
        "                f.write(new_content)\n",
        "print('✓ Patched SVG-IR for PyTorch 2.6 torch.load weights_only=False compatibility')\n",
        "\n"
    ]
    source = source[:patch_idx] + patch_lines + source[patch_idx:]
    d['cells'][2]['source'] = source

with open('SVG_IR_TensoIR_Lego_Colab.ipynb', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2)

with open('comparison_methods/SVG-IR/SVG_IR_TensoIR_Lego_Colab.ipynb', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2)

