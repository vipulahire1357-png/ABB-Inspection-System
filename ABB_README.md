# ABB PanelSight - Local VS Code Setup (Abby API edition)

## 1. Requirements
- Windows 10/11
- Python 3.11 recommended
- ABB network / VPN connection (Abby API is internal)
- Abby API key (from "Abby for developers": https://api.abby.abb.com/documentation/)

Vision inference runs through the Abby API gateway. Your laptop handles image
selection, ROI cropping, preprocessing, coordinate conversion, annotation and export.
No local GPU is required.

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

## 3. Configure `.env`

The script searches for `.env` in the project folder and its parent folders.
Add these lines to your existing `.env` (only `ABBY_API_KEY` is mandatory, the rest
have defaults):

```env
# Required
ABBY_API_KEY=paste_your_abby_api_key_here

# Endpoint - verify path, header and model names in the Abby developer docs
ABBY_BASE_URL=https://api.abby.abb.com
ABBY_CHAT_PATH=ABBY_CHAT_PATH=/api/v1/chat/completions
ABBY_AUTH_HEADER=Authorization
ABBY_AUTH_SCHEME=Bearer
ABBY_MODEL=gemini-3.8-flash
```

Optional tuning:

```env
ABBY_MAX_CONCURRENCY=3      # parallel requests; raise only if your Abby quota allows
ABBY_MAX_RETRIES=3          # retries on 408/429/5xx/timeouts with exponential backoff
ABBY_MAX_OUTPUT_TOKENS=4096 # raise if JSON gets truncated on crowded panels
ABBY_TEMPERATURE=0          # deterministic output
ABBY_TIMEOUT_S=120
ABBY_JSON_MODE=true         # set false if the endpoint rejects response_format
ABBY_IMAGE_DETAIL=high      # high | low | auto
ABBY_VERIFY_SSL=true        # set false only if a corporate SSL proxy breaks HTTPS
MAX_LONG_SIDE=2048          # smaller = faster/cheaper, larger = better small-label OCR
JPEG_QUALITY=88
```

If Abby uses an API-key header instead of Bearer tokens, use for example:

```env
ABBY_AUTH_HEADER=x-api-key
ABBY_AUTH_SCHEME=
```

Never commit `.env` to Git. Make sure `.gitignore` contains `.env`.

## 4. Run

```powershell
python main.py
```

Select one or more JPG/PNG/WEBP images. The picker opens at `test images`. For each
image, drag an ROI in the crop window, navigate with Previous/Next, reset a crop,
and submit or cancel the batch.

To process every supported image in `test images` without the picker:

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

## 6. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `HTTP 401/403` | Wrong key, or wrong `ABBY_AUTH_HEADER` / `ABBY_AUTH_SCHEME` |
| `HTTP 404` | Wrong `ABBY_CHAT_PATH` - check the Abby API reference |
| `HTTP 400` mentioning model | Use an exact model ID from the Abby docs in `ABBY_MODEL` |
| `HTTP 400` mentioning response_format | Set `ABBY_JSON_MODE=false` |
| `HTTP 429` | Lower `ABBY_MAX_CONCURRENCY` |
| SSL / certificate error | Connect to VPN; as last resort `ABBY_VERIFY_SSL=false` |
| "returned no text" | Endpoint uses a different response shape - check docs |

## 7. Notes
- The inspection JSON uses the stable `panelsight.image.v1` format: image metadata plus a
  flat `detections` array with `type`, `text` and pixel `bbox_xyxy_px`. Types are
  `device`, `device_id_label` and `ferrule`.
- Malformed detections from the model are skipped instead of failing the whole image.
- Bounding boxes and OCR must be validated against real panel images.
- `metrics.csv` includes preprocessing stages, API round-trip, retries, token usage and
  export timings.