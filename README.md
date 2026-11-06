# ABB PanelSight - Local VS Code Setup

## 1. Requirements
- Windows 10/11
- Python 3.11 recommended
- Internet connection
- Gemini API key

The Gemini vision inference runs in the cloud. Your laptop handles image selection,
ROI cropping, preprocessing, coordinate conversion, annotation, JSON and ZIP export.
A local NVIDIA GPU is not required by this script.

## 2. Setup

Open this folder in VS Code. In the VS Code terminal:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, use:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. API key

Copy `.env.example` to `.env`, then replace the placeholder with your Gemini API key:

```env
GEMINI_API_KEY=your_real_key
GEMINI_MODEL=gemini-3.5-flash-lite
```

Do not commit `.env` to GitHub.

## 4. Run

```powershell
python main.py
```

Select one or more JPG/PNG/WEBP images. The picker opens at `test images`. For each
image, use the crop window to drag an ROI, navigate with Previous/Next, reset a crop,
and submit or cancel the batch.

To load every supported image in the project's `test images` folder without using
the file picker, run:

```powershell
python main.py --test-images
```

## 5. Output

Results are written under `output/run_YYYYMMDD_HHMMSS/`:
- `*.inspection.json`
- `*.annotated.jpg`
- `metrics.csv`
- `batch_summary.json`

A ZIP archive is created alongside the run folder.

## 6. Important notes
- Start with MAX_CONCURRENCY=2 or 3. Increase only if your API quota permits.
- The script uses Gemini API; local GPU VRAM does not accelerate Gemini inference.
- The inspection JSON uses the stable `panelsight.image.v1` format. Each file has image
  metadata and a flat `detections` array with `type`, `text`, and pixel `bbox_xyxy_px`.
  Detection types are `device`, `device_id_label`, and `ferrule`. No image-to-image
  comparison or cross-image matching is performed.
- The model's bounding boxes and OCR must be validated against real panel images.
- `metrics.csv` includes preprocessing stages, failed-request elapsed time, retry delays,
  token usage and per-image export timings. Batch processing and crop UI time are separate.
