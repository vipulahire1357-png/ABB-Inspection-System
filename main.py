# -*- coding: utf-8 -*-
"""
ABB PanelSight - Local VS Code version (Abby API edition)
Abby (ABB internal AI gateway) vision extraction + local crop/coordinate
conversion/annotation/JSON export.
"""

import asyncio
import argparse
import base64
import io
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Literal

import httpx
import pandas as pd
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pydantic import create_model
from dotenv import load_dotenv
from tkinter import Tk, filedialog

# ============================================================
# 1. CONFIGURATION (all values come from .env)
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# Find the existing .env file in this project folder or any parent folder.
ENV_FILE = next(
    (folder / ".env"
     for folder in (BASE_DIR, *BASE_DIR.parents)
     if (folder / ".env").is_file()),
    None,
)
if ENV_FILE is None:
    raise RuntimeError(f"No .env file found in {BASE_DIR} or any parent folder.")

load_dotenv(dotenv_path=ENV_FILE, override=False)


def env_str(key, default=""):
    return (os.getenv(key) or default).strip()


def env_int(key, default):
    try:
        return int(env_str(key, str(default)))
    except ValueError:
        return default


def env_float(key, default):
    try:
        return float(env_str(key, str(default)))
    except ValueError:
        return default


def env_bool(key, default):
    value = env_str(key, str(default)).lower()
    return value in {"1", "true", "yes", "on"}


API_KEY = env_str("ABBY_API_KEY")
if not API_KEY:
    raise RuntimeError(f"ABBY_API_KEY not found in {ENV_FILE}.")

# Endpoint (check https://api.abby.abb.com/documentation/ for the exact path)
ABBY_BASE_URL = env_str("ABBY_BASE_URL", "https://api.abby.abb.com").rstrip("/")
ABBY_CHAT_PATH = "/" + env_str("ABBY_CHAT_PATH", "/api/v1/chat/completions").lstrip("/")
ABBY_ENDPOINT = ABBY_BASE_URL + ABBY_CHAT_PATH

# Authentication header, e.g. "Authorization" + "Bearer"  or  "x-api-key" + ""
ABBY_AUTH_HEADER = env_str("ABBY_AUTH_HEADER", "Authorization")
ABBY_AUTH_SCHEME = env_str("ABBY_AUTH_SCHEME", "Bearer")

MODEL = env_str("ABBY_MODEL", "gemini-3.8-flash")

# Performance / request tuning
MAX_CONCURRENCY = env_int("ABBY_MAX_CONCURRENCY", 3)
MAX_RETRIES = env_int("ABBY_MAX_RETRIES", 3)
MAX_OUTPUT_TOKENS = env_int("ABBY_MAX_OUTPUT_TOKENS", 4096)
TEMPERATURE = env_float("ABBY_TEMPERATURE", 0.0)
REQUEST_TIMEOUT_S = env_float("ABBY_TIMEOUT_S", 120.0)
USE_JSON_MODE = env_bool("ABBY_JSON_MODE", True)     # sends response_format=json_object
VERIFY_SSL = env_bool("ABBY_VERIFY_SSL", True)        # set false only behind SSL-inspecting proxy

# Image tuning (smaller = faster + cheaper, larger = better OCR on small labels)
MAX_LONG_SIDE = env_int("MAX_LONG_SIDE", 2048)
JPEG_QUALITY = env_int("JPEG_QUALITY", 88)
IMAGE_DETAIL = env_str("ABBY_IMAGE_DETAIL", "high")  # high | low | auto
IMAGE_FORMAT = env_str("ABBY_IMAGE_FORMAT", "abby_file")  # confirmed working; "auto" = re-detect

NATIVE_YX = True

OUTPUT_ROOT = BASE_DIR / "output"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

IMAGE_LOAD_METRICS = {}


def safe_write_text(path, text):
    """Write text, recreating the folder if needed. Never crashes the run."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[WARN] Could not write {path}: {exc}")
        return False
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

print(f"Abby API key loaded from: {ENV_FILE}")
print(f"Abby endpoint: {ABBY_ENDPOINT}")
print(f"Image format: {IMAGE_FORMAT}")


def auth_headers():
    value = f"{ABBY_AUTH_SCHEME} {API_KEY}".strip() if ABBY_AUTH_SCHEME else API_KEY
    return {
        ABBY_AUTH_HEADER: value,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============================================================
# 2. SELECT AND LOAD IMAGES
# ============================================================

def choose_images(paths=None):
    """Load selected images once; the test-images option bypasses the picker."""
    if paths is None:
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(
            title="Select ABB panel inspection images",
            initialdir=str(BASE_DIR / "test images"),
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()

    if not paths:
        raise RuntimeError("No images selected.")

    originals = {}
    for path in paths:
        path = Path(path)
        load_started = time.perf_counter()
        try:
            with Image.open(path) as im:
                originals[path.name] = ImageOps.exif_transpose(im).convert("RGB").copy()
            IMAGE_LOAD_METRICS[path.name] = time.perf_counter() - load_started
        except Exception as exc:
            print(f"[SKIP] {path.name}: {exc}")

    if not originals:
        raise RuntimeError("No readable images were selected.")

    return originals



def select_rois(originals):
    """
    Friendly multi-image crop interface.
    - Select each image from the left-side list.
    - Drag on the preview to draw an ROI.
    - Use Reset Crop to restore the full image.
    - Submit Crops to continue processing all images.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox
    from PIL import ImageTk

    names = list(originals.keys())
    crop_rects = {
        name: (0, 0, originals[name].width, originals[name].height)
        for name in names
    }

    root = tk.Tk()
    root.title("ABB PanelSight | Inspection Image Cropping")
    root.geometry("1180x790")
    root.minsize(900, 620)
    root.configure(bg="#eef2f7")

    # Theme
    BG = "#eef2f7"
    PANEL = "#ffffff"
    NAVY = "#14243a"
    MUTED = "#64748b"
    ACCENT = "#e32636"
    ACCENT_DARK = "#bd1828"
    BORDER = "#dbe3ed"
    GREEN = "#16865b"

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Treeview",
        background=PANEL,
        fieldbackground=PANEL,
        foreground=NAVY,
        rowheight=34,
        borderwidth=0,
        font=("Segoe UI", 10),
    )
    style.configure(
        "Treeview.Heading",
        background="#f3f6fa",
        foreground=NAVY,
        font=("Segoe UI Semibold", 9),
        relief="flat",
    )
    style.map("Treeview", background=[("selected", "#feecef")],
              foreground=[("selected", NAVY)])
    style.configure("TProgressbar", troughcolor="#e7edf4",
                    background=ACCENT, bordercolor="#e7edf4",
                    lightcolor=ACCENT, darkcolor=ACCENT)

    # Header
    header = tk.Frame(root, bg=NAVY, height=82)
    header.pack(fill="x")
    header.pack_propagate(False)

    brand = tk.Frame(header, bg=NAVY)
    brand.pack(side="left", padx=24, pady=13)
    tk.Label(
        brand, text="ABB", bg=NAVY, fg="#ff414d",
        font=("Segoe UI Black", 19)
    ).pack(anchor="w")
    tk.Label(
        brand, text="PanelSight  /  Image Preparation",
        bg=NAVY, fg="white", font=("Segoe UI Semibold", 15)
    ).pack(anchor="w")

    header_right = tk.Frame(header, bg=NAVY)
    header_right.pack(side="right", padx=24)
    tk.Label(
        header_right, text="STEP 1 OF 2",
        bg=NAVY, fg="#cbd5e1", font=("Segoe UI Semibold", 9)
    ).pack(anchor="e", pady=(18, 2))
    tk.Label(
        header_right, text="Select inspection regions",
        bg=NAVY, fg="white", font=("Segoe UI", 10)
    ).pack(anchor="e")

    # Intro strip
    intro = tk.Frame(root, bg=BG)
    intro.pack(fill="x", padx=24, pady=(17, 12))
    tk.Label(
        intro, text="Prepare your inspection images",
        bg=BG, fg=NAVY, font=("Segoe UI Semibold", 17)
    ).pack(anchor="w")
    tk.Label(
        intro,
        text="Choose an image, then drag over the panel area you want Abby to inspect. "
             "You can use the full image or define a different crop for every image.",
        bg=BG, fg=MUTED, font=("Segoe UI", 10), wraplength=1050, justify="left"
    ).pack(anchor="w", pady=(4, 0))

    # Main content
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=24, pady=(0, 14))

    left = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
    left.pack(side="left", fill="y", padx=(0, 14))
    left.configure(width=245)
    left.pack_propagate(False)

    tk.Label(
        left, text="YOUR IMAGES", bg=PANEL, fg=MUTED,
        font=("Segoe UI Semibold", 9)
    ).pack(anchor="w", padx=15, pady=(16, 8))

    image_list = tk.Listbox(
        left, activestyle="none", selectmode="browse",
        bg=PANEL, fg=NAVY, selectbackground="#feecef",
        selectforeground=NAVY, relief="flat", borderwidth=0,
        font=("Segoe UI", 10), highlightthickness=0,
        exportselection=False
    )
    image_list.pack(fill="both", expand=True, padx=9, pady=(0, 10))
    for name in names:
        image_list.insert("end", name)

    left_bottom = tk.Frame(left, bg=PANEL)
    left_bottom.pack(fill="x", padx=14, pady=(0, 14))
    progress_label = tk.Label(
        left_bottom, text=f"0 / {len(names)} images reviewed",
        bg=PANEL, fg=MUTED, font=("Segoe UI", 9)
    )
    progress_label.pack(anchor="w", pady=(0, 7))
    progress = ttk.Progressbar(left_bottom, maximum=len(names), value=0)
    progress.pack(fill="x")

    # Preview panel
    right = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
    right.pack(side="left", fill="both", expand=True)

    toolbar = tk.Frame(right, bg=PANEL)
    toolbar.pack(fill="x", padx=17, pady=(13, 8))
    selected_name = tk.Label(
        toolbar, text="", bg=PANEL, fg=NAVY,
        font=("Segoe UI Semibold", 11), anchor="w"
    )
    selected_name.pack(side="left", fill="x", expand=True)
    dimensions_label = tk.Label(
        toolbar, text="", bg=PANEL, fg=MUTED,
        font=("Segoe UI", 9)
    )
    dimensions_label.pack(side="right")

    canvas_wrap = tk.Frame(right, bg="#e8edf3")
    canvas_wrap.pack(fill="both", expand=True, padx=15, pady=(0, 10))

    canvas = tk.Canvas(
        canvas_wrap, bg="#e8edf3", highlightthickness=0,
        cursor="crosshair"
    )
    canvas.pack(fill="both", expand=True)

    hint = tk.Label(
        right,
        text="DRAG TO SELECT A CROP  •  Leave untouched to use the full image",
        bg=PANEL, fg=MUTED, font=("Segoe UI Semibold", 8)
    )
    hint.pack(anchor="w", padx=18, pady=(0, 12))

    # Footer actions
    footer = tk.Frame(root, bg=BG)
    footer.pack(fill="x", padx=24, pady=(0, 19))

    crop_status = tk.Label(
        footer, text="Crop: Full image",
        bg=BG, fg=MUTED, font=("Segoe UI", 9)
    )
    crop_status.pack(side="left", pady=9)

    button_area = tk.Frame(footer, bg=BG)
    button_area.pack(side="right")

    state = {
        "index": 0,
        "photo": None,
        "display_scale": 1.0,
        "display_x": 0,
        "display_y": 0,
        "display_w": 1,
        "display_h": 1,
        "drag_start": None,
        "rect_id": None,
        "image_id": None,
        "render_after": None,
        "completed": False,
    }

    def current_name():
        return names[state["index"]]

    def current_image():
        return originals[current_name()]

    def crop_is_full(name):
        im = originals[name]
        return crop_rects[name] == (0, 0, im.width, im.height)

    def update_progress():
        reviewed = sum(1 for name in names if name in crop_rects)
        progress.configure(value=reviewed)
        progress_label.configure(text=f"{reviewed} / {len(names)} images ready")

    def update_crop_status():
        name = current_name()
        rect = crop_rects[name]
        im = originals[name]
        if rect == (0, 0, im.width, im.height):
            crop_status.configure(text="Crop: Full image")
        else:
            x0, y0, x1, y1 = rect
            crop_status.configure(
                text=f"Crop: {x1-x0} × {y1-y0} px  |  Position: ({x0}, {y0})"
            )

    def render_preview():
        name = current_name()
        im = originals[name]
        canvas.update_idletasks()
        available_w = max(100, canvas.winfo_width() - 24)
        available_h = max(100, canvas.winfo_height() - 24)
        scale = min(available_w / im.width, available_h / im.height)
        display_w = max(1, int(im.width * scale))
        display_h = max(1, int(im.height * scale))
        preview = im.resize((display_w, display_h), Image.Resampling.LANCZOS)

        state["photo"] = ImageTk.PhotoImage(preview)
        state["display_scale"] = scale
        state["display_w"] = display_w
        state["display_h"] = display_h
        state["display_x"] = (canvas.winfo_width() - display_w) // 2
        state["display_y"] = (canvas.winfo_height() - display_h) // 2

        canvas.delete("all")
        state["image_id"] = canvas.create_image(
            state["display_x"], state["display_y"],
            image=state["photo"], anchor="nw"
        )

        x0, y0, x1, y1 = crop_rects[name]
        if not crop_is_full(name):
            sx = state["display_x"]
            sy = state["display_y"]
            scale = state["display_scale"]
            coords = (
                sx + x0 * scale, sy + y0 * scale,
                sx + x1 * scale, sy + y1 * scale
            )
            # Darken outside crop using a translucent-looking stipple overlay.
            # Draw the crop border prominently.
            state["rect_id"] = canvas.create_rectangle(
                *coords, outline=ACCENT, width=3, dash=(7, 3)
            )
            canvas.create_text(
                coords[0] + 7, coords[1] + 7,
                text="INSPECTION ROI", anchor="nw",
                fill=ACCENT, font=("Segoe UI Semibold", 9)
            )

        selected_name.configure(text=name)
        dimensions_label.configure(text=f"{im.width} × {im.height} px")
        update_crop_status()
        update_progress()

    def schedule_render(_event=None):
        if state["render_after"] is not None:
            root.after_cancel(state["render_after"])
        state["render_after"] = root.after(80, render_preview)

    def select_image(index):
        if index < 0 or index >= len(names):
            return
        state["index"] = index
        image_list.selection_clear(0, "end")
        image_list.selection_set(index)
        image_list.activate(index)
        render_preview()

    def on_list_select(_event=None):
        selection = image_list.curselection()
        if selection:
            select_image(selection[0])

    def canvas_to_image(x, y):
        scale = state["display_scale"]
        ix = (x - state["display_x"]) / scale
        iy = (y - state["display_y"]) / scale
        im = current_image()
        ix = max(0, min(im.width, ix))
        iy = max(0, min(im.height, iy))
        return ix, iy

    def on_mouse_down(event):
        x, y = canvas_to_image(event.x, event.y)
        state["drag_start"] = (x, y)
        if state["rect_id"] is not None:
            canvas.delete(state["rect_id"])
            state["rect_id"] = None
        sx = state["display_x"] + x * state["display_scale"]
        sy = state["display_y"] + y * state["display_scale"]
        state["rect_id"] = canvas.create_rectangle(
            sx, sy, sx, sy, outline=ACCENT, width=3, dash=(7, 3)
        )

    def on_mouse_drag(event):
        if state["drag_start"] is None or state["rect_id"] is None:
            return
        x, y = canvas_to_image(event.x, event.y)
        x0, y0 = state["drag_start"]
        sx0 = state["display_x"] + x0 * state["display_scale"]
        sy0 = state["display_y"] + y0 * state["display_scale"]
        sx1 = state["display_x"] + x * state["display_scale"]
        sy1 = state["display_y"] + y * state["display_scale"]
        canvas.coords(state["rect_id"], sx0, sy0, sx1, sy1)

    def on_mouse_up(event):
        if state["drag_start"] is None:
            return
        x, y = canvas_to_image(event.x, event.y)
        x0, y0 = state["drag_start"]
        state["drag_start"] = None

        left_px = max(0, min(current_image().width - 1, round(min(x0, x))))
        top_px = max(0, min(current_image().height - 1, round(min(y0, y))))
        right_px = max(left_px + 1, min(current_image().width, round(max(x0, x))))
        bottom_px = max(top_px + 1, min(current_image().height, round(max(y0, y))))

        # Ignore accidental clicks/tiny rectangles.
        if right_px - left_px < 8 or bottom_px - top_px < 8:
            render_preview()
            return

        crop_rects[current_name()] = (
            left_px, top_px, right_px, bottom_px
        )
        render_preview()

    def reset_current_crop():
        im = current_image()
        crop_rects[current_name()] = (0, 0, im.width, im.height)
        render_preview()

    def go_previous():
        select_image(max(0, state["index"] - 1))

    def go_next():
        select_image(min(len(names) - 1, state["index"] + 1))

    def submit_crops():
        # Ensure every image has a valid crop before closing.
        for name, im in originals.items():
            rect = crop_rects[name]
            x0, y0, x1, y1 = rect
            if not (0 <= x0 < x1 <= im.width and 0 <= y0 < y1 <= im.height):
                messagebox.showerror(
                    "Invalid crop",
                    f"The crop for {name} is invalid. Please reset or select it again.",
                    parent=root
                )
                select_image(names.index(name))
                return

        state["completed"] = True
        root.destroy()

    def cancel():
        if messagebox.askyesno(
            "Cancel inspection",
            "Close the crop window and stop this inspection run?",
            parent=root
        ):
            state["completed"] = False
            root.destroy()

    # Canvas interactions
    canvas.bind("<ButtonPress-1>", on_mouse_down)
    canvas.bind("<B1-Motion>", on_mouse_drag)
    canvas.bind("<ButtonRelease-1>", on_mouse_up)
    canvas.bind("<Configure>", schedule_render)
    image_list.bind("<<ListboxSelect>>", on_list_select)

    # Buttons
    def make_button(parent, text, command, bg_color, fg_color="white"):
        return tk.Button(
            parent, text=text, command=command,
            bg=bg_color, fg=fg_color,
            activebackground=ACCENT_DARK if bg_color == ACCENT else "#e2e8f0",
            activeforeground=fg_color,
            relief="flat", borderwidth=0, cursor="hand2",
            padx=16, pady=10, font=("Segoe UI Semibold", 9)
        )

    make_button(
        button_area, "← Previous", go_previous, "#e2e8f0", NAVY
    ).pack(side="left", padx=(0, 8))
    make_button(
        button_area, "Next →", go_next, "#e2e8f0", NAVY
    ).pack(side="left", padx=(0, 8))
    make_button(
        button_area, "Reset Crop", reset_current_crop, "#e2e8f0", NAVY
    ).pack(side="left", padx=(0, 8))
    make_button(
        button_area, "Submit Crops  →", submit_crops, ACCENT
    ).pack(side="left", padx=(0, 8))
    make_button(
        button_area, "Cancel", cancel, BG, MUTED
    ).pack(side="left")

    select_image(0)
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()

    if not state["completed"]:
        raise RuntimeError("Image cropping was cancelled by the user.")

    print("\nCrop selection completed:")
    for name, rect in crop_rects.items():
        print(f"  {name}: {rect}")

    return crop_rects


# ============================================================
# 3. SCHEMA AND PROMPT
# ============================================================

Detection = create_model(
    "Detection",
    k=(Literal["device", "device_id_label", "ferrule"], ...),
    i=(str, ...),
    b=(list[int], ...),
)
SCHEMA = create_model("InspectionDetections", detections=(list[Detection], ...))
BOX_ORDER = "ymin,xmin,ymax,xmax" if NATIVE_YX else "xmin,ymin,xmax,ymax"

SYSTEM_PROMPT = (
    "Inspect this electrical panel image. Return only JSON detections. For each visible "
    "panel device, return k=device, i='', b=its box. For each printed device-ID label, "
    "return k=device_id_label, i=the exact printed ID, b=tight box around the label. "
    "For each wire-end ferrule, return k=ferrule, i=the exact printed ID, b=box around "
    "the ferrule. Boxes are [ymin,xmin,ymax,xmax] integers 0-1000 relative to this "
    "image. Use i='' if unreadable; never guess. Do not classify terminal numbers, "
    "wire marks, logos, or printed device labels as devices. No grouping, ordering, "
    "description, or extra fields. Output exactly one JSON object of the form "
    '{"detections":[{"k":"device","i":"","b":[ymin,xmin,ymax,xmax]}]} '
    "with no markdown and no text outside the JSON."
)


# ============================================================
# 4. IMAGE PREPROCESSING AND COORDINATE HELPERS
# ============================================================

def prepare_image(name, originals, crop_rects):
    timings = {"crop_s": 0.0, "resize_s": 0.0, "jpeg_encode_s": 0.0}
    original = originals[name]
    rect = crop_rects[name]
    stage_started = time.perf_counter()
    roi = original.crop(rect)
    timings["crop_s"] = time.perf_counter() - stage_started

    if roi.width < 2 or roi.height < 2:
        raise ValueError(f"Invalid crop for {name}: {rect}")

    send_img = roi
    longest = max(roi.size)
    if longest > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / longest
        stage_started = time.perf_counter()
        send_img = roi.resize(
            (
                max(1, round(roi.width * scale)),
                max(1, round(roi.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
        timings["resize_s"] = time.perf_counter() - stage_started

    buffer = io.BytesIO()
    stage_started = time.perf_counter()
    send_img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    timings["jpeg_encode_s"] = time.perf_counter() - stage_started
    return buffer.getvalue(), rect, roi.size, send_img.size, timings


def norm_box(box):
    """Model normalized box -> [x1,y1,x2,y2] on 0..1000."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        values = [float(v) for v in box]
    except (TypeError, ValueError):
        return None

    if any(v < -50 or v > 1050 for v in values):
        return None

    values = [max(0, min(1000, int(round(v)))) for v in values]

    if NATIVE_YX:
        values = [values[1], values[0], values[3], values[2]]

    x1, y1, x2, y2 = values
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))

    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return [x1, y1, x2, y2]


def to_original(box, rect, original_size):
    """ROI-relative 0..1000 box -> original-image pixel coordinates."""
    normalized = norm_box(box)
    if normalized is None:
        return [0, 0, 0, 0]

    x0, y0, x1, y1 = rect
    original_w, original_h = original_size
    roi_w, roi_h = x1 - x0, y1 - y0

    ax1 = round(x0 + normalized[0] / 1000 * roi_w)
    ay1 = round(y0 + normalized[1] / 1000 * roi_h)
    ax2 = round(x0 + normalized[2] / 1000 * roi_w)
    ay2 = round(y0 + normalized[3] / 1000 * roi_h)

    ax1, ax2 = sorted((max(0, min(original_w, ax1)), max(0, min(original_w, ax2))))
    ay1, ay2 = sorted((max(0, min(original_h, ay1)), max(0, min(original_h, ay2))))
    return [int(ax1), int(ay1), int(ax2), int(ay2)]


def to_image_detections(data, name, rect, originals):
    original = originals[name]
    detections = []
    for detection in data.get("detections", []):
        bbox = to_original(detection.get("b"), rect, original.size)
        detections.append({
            "type": detection["k"],
            "text": detection["i"] if detection["k"] != "device" else None,
            "bbox_xyxy_px": None if bbox == [0, 0, 0, 0] else bbox,
        })
    return {
        "schema_version": "panelsight.image.v1",
        "image": {
            "file_name": name,
            "width_px": original.width,
            "height_px": original.height,
            "roi_xyxy_px": list(rect),
        },
        "detections": detections,
    }


# ============================================================
# 5. ABBY API PROCESSING
# ============================================================

JSON_MODE_STATE = {"enabled": True}
IMAGE_FORMAT_STATE = {"name": IMAGE_FORMAT}
MIME = "image/jpeg"
FILENAME = "image.jpg"


def _meta():
    return {"mime_type": MIME, "media_type": MIME, "filename": FILENAME}


def _part_variants():
    """Image content-part shapes: name -> fn(b64, data_url) -> part dict."""
    variants = {
        # Most common public formats first
        "openai_url": lambda b, u: {"type": "image_url", "image_url": {"url": u}},
        "openai_file": lambda b, u: {"type": "file", "file": {"filename": FILENAME, "file_data": u}},
        "anthropic": lambda b, u: {"type": "image", "source": {"type": "base64", "media_type": MIME, "data": b}},
        "input_image": lambda b, u: {"type": "input_image", "image_url": u},
        "input_file": lambda b, u: {"type": "input_file", "filename": FILENAME, "file_data": u},
    }
    types = ["file", "image", "input_file", "input_image", "image_url", "base64", "image_base64", "document"]
    keys = ["data", "file_data", "base64", "content", "bytes", "url", "image_url"]
    for t in types:
        for k in keys:
            for val in ("b64", "url"):
                pick = (lambda b, u, val=val: b if val == "b64" else u)
                variants[f"flat/{t}/{k}/{val}"] = (
                    lambda b, u, t=t, k=k, pick=pick: {"type": t, k: pick(b, u), **_meta()})
                variants[f"nested_type/{t}/{k}/{val}"] = (
                    lambda b, u, t=t, k=k, pick=pick: {"type": t, t: {k: pick(b, u), **_meta()}})
                variants[f"nested_file/{t}/{k}/{val}"] = (
                    lambda b, u, t=t, k=k, pick=pick: {"type": t, "file": {k: pick(b, u), **_meta()}})
    return variants


PART_VARIANTS = _part_variants()
# Confirmed on api.abby.abb.com (2026-11-06): file part with raw base64 in "url".
PART_VARIANTS["abby_file"] = lambda b, u: {
    "type": "file", "url": b, "mime_type": MIME, "media_type": MIME, "filename": FILENAME,
}

# Attachments outside the content list (top-level or message-level fields)
ATTACH_VARIANTS = {}
for _where in ("top", "message"):
    for _field in ("files", "attachments", "images", "documents"):
        for _shape in ("obj_b64", "obj_url", "raw_b64", "raw_url"):
            ATTACH_VARIANTS[f"{_where}/{_field}/{_shape}"] = (_where, _field, _shape)


def _attachment(shape, b64, url):
    if shape == "raw_b64":
        return b64
    if shape == "raw_url":
        return url
    return {"data": b64 if shape == "obj_b64" else url, "content": b64, "base64": b64, **_meta()}


IMAGE_FORMATS = ["abby_file"] + [f for f in PART_VARIANTS if f != "abby_file"] + list(ATTACH_VARIANTS)


def build_payload(image_b64, fmt=None, prompt="Inspect this panel image and return the JSON.",
                  system=True, json_mode=None, model=None):
    fmt = fmt or IMAGE_FORMAT_STATE["name"]
    data_url = f"data:{MIME};base64,{image_b64}"
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] if system else []
    payload = {"model": model or MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_OUTPUT_TOKENS}

    if fmt in PART_VARIANTS:
        user = {"role": "user", "content": [
            {"type": "text", "text": prompt}, PART_VARIANTS[fmt](image_b64, data_url)]}
    elif fmt in ATTACH_VARIANTS:
        where, field, shape = ATTACH_VARIANTS[fmt]
        user = {"role": "user", "content": prompt}
        item = [_attachment(shape, image_b64, data_url)]
        if where == "top":
            payload[field] = item
        else:
            user[field] = item
    else:
        raise ValueError(f"Unknown ABBY_IMAGE_FORMAT: {fmt}")

    messages.append(user)
    payload["messages"] = messages
    use_json = (USE_JSON_MODE and JSON_MODE_STATE["enabled"]) if json_mode is None else json_mode
    if use_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def error_message(response):
    """Readable error text (ignores Abby's misleading 'param' field)."""
    try:
        data = response.json()
    except Exception:
        return response.text
    err = data.get("error") or data.get("detail") or data
    if isinstance(err, dict):
        return " ".join(f"{err.get('message', '')} {err.get('details', '')}".split())
    return " ".join(str(err).split())


def _probe_image_b64():
    probe = Image.new("RGB", (360, 140), "white")
    ImageDraw.Draw(probe).text((30, 40), "X7Q", fill="black", font=get_font(64))
    buf = io.BytesIO()
    probe.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def detect_image_format(http):
    """Find the image format Abby accepts AND passes to the model. Logs everything."""
    log_path = OUTPUT_ROOT / "abby_image_probe.log"
    log_lines = []

    def log(line):
        print(line)
        log_lines.append(line)

    b64 = _probe_image_b64()
    question = "What text is written in this image? Reply with the text only."

    async def post(payload):
        try:
            r = await http.post(ABBY_ENDPOINT, json=payload)
        except Exception as exc:
            return None, f"ERR {type(exc).__name__}: {exc}"
        if r.status_code != 200:
            return r.status_code, error_message(r)[:160]
        return 200, (extract_text(r.json()) or "").strip()[:80]

    log(f"Detecting Abby image format for model {MODEL} (one-time check)...")

    # Baselines: tells us whether list-style content is accepted at all
    for name, content in (("text as string", "Reply OK"),
                          ("text as list", [{"type": "text", "text": "Reply OK"}])):
        status, info = await post({"model": MODEL, "max_tokens": 10,
                                   "messages": [{"role": "user", "content": content}]})
        log(f"  baseline {name:15} -> {status}: {info}")

    async def try_fmt(fmt, model=None):
        payload = build_payload(b64, fmt=fmt, prompt=question, system=False,
                                json_mode=False, model=model)
        status, info = await post(payload)
        seen = status == 200 and "X7Q" in info.upper().replace(" ", "")
        return fmt, status, info, seen

    found = None
    batch = 6
    for i in range(0, len(IMAGE_FORMATS), batch):
        results = await asyncio.gather(*[try_fmt(f) for f in IMAGE_FORMATS[i:i + batch]])
        for fmt, status, info, seen in results:
            if status != 400 or "FileInputBase64Content" not in info:
                log(f"  {fmt:40} -> {status}: {info}{'   <-- image read OK' if seen else ''}")
            if seen and not found:
                found = fmt
        if found:
            break
    tried = min(len(IMAGE_FORMATS), i + batch)
    log(f"  ({tried} formats tried; identical FileInputBase64Content errors hidden)")

    if not found:
        log("\nNo format worked with this model. Testing other models with standard formats...")
        try:
            r = await http.get(ABBY_BASE_URL + "/api/v1/models")
            models = [m["id"] for m in r.json().get("data", []) if m.get("id") != MODEL]
        except Exception:
            models = []
        for model in models[:8]:
            for fmt in ("openai_url", "openai_file", "anthropic"):
                f, status, info, seen = await try_fmt(fmt, model=model)
                log(f"  {model:28} {fmt:12} -> {status}: {info}{'   <-- WORKS' if seen else ''}")
                if seen:
                    log(f"\n[TIP] Model {model} accepts images. Set in .env:\n"
                        f"      ABBY_MODEL={model}\n      ABBY_IMAGE_FORMAT={fmt}")
                    break

    safe_write_text(log_path, "\n".join(log_lines))
    if found:
        log(f"\n[TIP] Add to .env to skip detection next time:  ABBY_IMAGE_FORMAT={found}\n")
        return found
    raise RuntimeError(
        f"Abby rejected every image format for {MODEL}. Full log saved to {log_path}. "
        "Share that log (or the image example from the Abby API docs)."
    )


def extract_text(data):
    """Read the answer text from common response shapes (OpenAI / Abby variants)."""
    if isinstance(data, dict):
        choices = data.get("choices")
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content:
                return content
            if choices[0].get("text"):
                return choices[0]["text"]
        for key in ("output_text", "answer", "response", "content", "text", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                nested = extract_text(value)
                if nested:
                    return nested
    return None


def extract_usage(data):
    usage = (data or {}).get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "thought_tokens": details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_detections(text):
    """Tolerant JSON parsing: strips code fences, drops malformed detections."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError:
        match = JSON_BLOCK.search(cleaned)
        if not match:
            raise ValueError(f"Abby response is not JSON: {text[:200]!r}")
        raw = json.loads(match.group(0))

    if isinstance(raw, list):
        raw = {"detections": raw}

    valid = []
    for item in raw.get("detections", []) or []:
        try:
            valid.append(Detection.model_validate({
                "k": item.get("k"),
                "i": item.get("i") or "",
                "b": [int(round(float(v))) for v in item.get("b", [])],
            }).model_dump())
        except Exception:
            continue
    return {"detections": valid}


async def generate(http, image_b64):
    """Single Abby request. Returns (text, usage, elapsed)."""
    started = time.perf_counter()
    response = await http.post(ABBY_ENDPOINT, json=build_payload(image_b64))
    # Fallback only if the error message itself is about JSON mode.
    if response.status_code in (400, 422) and JSON_MODE_STATE["enabled"] and USE_JSON_MODE:
        msg = error_message(response).lower()
        if "response_format" in msg or "json_object" in msg or "json mode" in msg:
            JSON_MODE_STATE["enabled"] = False
            print("[INFO] Model does not accept response_format; continuing without JSON mode.")
            response = await http.post(ABBY_ENDPOINT, json=build_payload(image_b64))
    if response.status_code >= 400:
        raise AbbyHTTPError(response.status_code, error_message(response)[:500])
    data = response.json()
    return extract_text(data), extract_usage(data), time.perf_counter() - started


class AbbyHTTPError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status


def is_retryable(exc):
    if isinstance(exc, AbbyHTTPError):
        return exc.status in RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def process_one(http, name, originals, crop_rects, semaphore, batch_start):
    preprocess_start = time.perf_counter()
    image_bytes, rect, crop_size, sent_size, preprocess_timings = await asyncio.to_thread(
        prepare_image, name, originals, crop_rects
    )
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    preprocess_sec = time.perf_counter() - preprocess_start

    queue_start = time.perf_counter()
    async with semaphore:
        queue_wait = time.perf_counter() - queue_start
        api_rtt = 0.0
        retries = 0
        retry_delay = 0.0
        response_text = None
        usage = {}

        for attempt in range(MAX_RETRIES + 1):
            request_started = time.perf_counter()
            try:
                response_text, usage, _ = await generate(http, image_b64)
                api_rtt += time.perf_counter() - request_started
                break
            except Exception as exc:
                api_rtt += time.perf_counter() - request_started
                if attempt >= MAX_RETRIES or not is_retryable(exc):
                    exc.panelsight_metrics = {
                        "image": name, "model": MODEL,
                        "image_load_s": IMAGE_LOAD_METRICS.get(name),
                        **{key: round(value, 6) for key, value in preprocess_timings.items()},
                        "preprocess_s": round(preprocess_sec, 6),
                        "queue_wait_s": round(queue_wait, 6),
                        "api_rtt_s": round(api_rtt, 6), "retries": retries,
                        "retry_delay_s": round(retry_delay, 6),
                        "status": f"ERROR: {type(exc).__name__}: {exc}",
                    }
                    raise
                retries += 1
                delay = min(20.0, 1.5 * (2 ** attempt))
                retry_delay += delay
                print(f"[RETRY {retries}] {name}: {exc} -> waiting {delay:.1f}s")
                await asyncio.sleep(delay)

    if not response_text:
        raise ValueError("Abby returned no text. Check model name / endpoint / response format.")

    parse_started = time.perf_counter()
    parsed = parse_detections(response_text)
    json_parse_sec = time.perf_counter() - parse_started
    coordinate_started = time.perf_counter()
    image_data = to_image_detections(parsed, name, rect, originals)
    coordinate_sec = time.perf_counter() - coordinate_started
    json_ready = time.perf_counter()

    return {
        "name": name,
        "ok": True,
        "image_data": image_data,
        "metrics": {
            "image": name,
            "model": MODEL,
            "image_load_s": round(IMAGE_LOAD_METRICS.get(name, 0), 6),
            "api_rtt_s": round(api_rtt, 3),
            "retry_delay_s": round(retry_delay, 6),
            "json_ready_s": round(json_ready - batch_start, 3),
            "preprocess_s": round(preprocess_sec, 3),
            **{key: round(value, 6) for key, value in preprocess_timings.items()},
            "queue_wait_s": round(queue_wait, 3),
            "json_parse_s": round(json_parse_sec, 6),
            "coordinate_s": round(coordinate_sec, 6),
            **usage,
            "retries": retries,
            "original_size": f"{originals[name].width}x{originals[name].height}",
            "crop_size": f"{crop_size[0]}x{crop_size[1]}",
            "sent_size": f"{sent_size[0]}x{sent_size[1]}",
            "status": "OK",
        },
    }


async def run_batch(originals, crop_rects):
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    batch_start = time.perf_counter()

    # One shared HTTP client = connection reuse (TLS handshake only once).
    limits = httpx.Limits(max_connections=MAX_CONCURRENCY, max_keepalive_connections=MAX_CONCURRENCY)
    async with httpx.AsyncClient(
        headers=auth_headers(),
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=15.0),
        limits=limits,
        verify=VERIFY_SSL,
        http2=False,
    ) as http:
        if IMAGE_FORMAT_STATE["name"] == "auto":
            IMAGE_FORMAT_STATE["name"] = await detect_image_format(http)
        results = await asyncio.gather(
            *[
                process_one(http, name, originals, crop_rects, semaphore, batch_start)
                for name in originals
            ],
            return_exceptions=True,
        )

    wall_time = time.perf_counter() - batch_start
    cleaned = []

    for name, result in zip(originals, results):
        if isinstance(result, Exception):
            cleaned.append(
                {
                    "name": name,
                    "ok": False,
                    "error": f"{type(result).__name__}: {result}",
                    "metrics": {
                        "image": name,
                        "model": MODEL,
                        **getattr(result, "panelsight_metrics", {}),
                        "status": f"ERROR: {type(result).__name__}: {result}",
                    },
                }
            )
        else:
            cleaned.append(result)

    return cleaned, wall_time


# ============================================================
# 6. ANNOTATION AND EXPORT
# ============================================================

FONT_CACHE = {}


def get_font(size):
    if size not in FONT_CACHE:
        try:
            FONT_CACHE[size] = ImageFont.load_default(size=size)
        except TypeError:
            FONT_CACHE[size] = ImageFont.load_default()
    return FONT_CACHE[size]


def annotate_original(name, compact, originals):
    image = originals[name].copy()
    draw = ImageDraw.Draw(image)
    line_width = max(2, image.width // 600)
    font = get_font(max(12, image.width // 110))

    def add_box(box, color, label=None):
        if not box or box == [0, 0, 0, 0]:
            return
        x1, y1, x2, y2 = box
        draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
        if label:
            label_y = max(0, y1 - getattr(font, "size", 12) - 2)
            draw.text(
                (x1, label_y),
                label,
                fill=color,
                font=font,
                stroke_width=1,
                stroke_fill=(0, 0, 0),
            )

    colors = {
        "device": (0, 210, 255),
        "device_id_label": (180, 100, 255),
        "ferrule": (255, 165, 0),
    }
    for detection in compact["detections"]:
        label = detection["type"]
        if detection["text"]:
            label += f': {detection["text"]}'
        add_box(
            detection["bbox_xyxy_px"],
            colors[detection["type"]],
            label,
        )

    return image


def export_results(results, originals, wall_time, crop_ui_sec=0.0):
    all_metrics = []

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = OUTPUT_ROOT / time.strftime("run_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    for result in results:
        if not result["ok"]:
            all_metrics.append(result["metrics"])
            print(f"\n[ERROR] {result['name']}: {result['error']}")
            continue

        name = result["name"]
        image_data = result["image_data"]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)

        export_started = time.perf_counter()
        raw_json = json.dumps(image_data, ensure_ascii=False, separators=(",", ":"))

        json_path = run_dir / f"{stem}.inspection.json"
        annotated_path = run_dir / f"{stem}.annotated.jpg"

        json_path.write_text(raw_json, encoding="utf-8")

        annotation_start = time.perf_counter()
        annotate_original(name, image_data, originals).save(
            annotated_path, quality=92
        )
        annotation_sec = time.perf_counter() - annotation_start

        metrics = result["metrics"]
        metrics.update(
            {
                "annotation_s": round(annotation_sec, 3),
                "export_s": round(time.perf_counter() - export_started, 6),
                "json_bytes": len(raw_json.encode("utf-8")),
                "device_detections": sum(d["type"] == "device" for d in image_data["detections"]),
                "device_id_labels": sum(d["type"] == "device_id_label" for d in image_data["detections"]),
                "ferrules": sum(d["type"] == "ferrule" for d in image_data["detections"]),
                "json_file": json_path.name,
                "annotated_file": annotated_path.name,
            }
        )
        all_metrics.append(metrics)

        print(f"\n[OK] {name}")
        print(f"  Detections: {len(image_data['detections'])}")
        print(f"  Devices: {metrics['device_detections']}")
        print(f"  Device ID labels: {metrics['device_id_labels']}")
        print(f"  Ferrules: {metrics['ferrules']}")

    metrics_df = pd.DataFrame(all_metrics)
    metrics_df.to_csv(run_dir / "metrics.csv", index=False)

    successful = sum(1 for r in results if r["ok"])
    print("\n" + "=" * 65)
    print("PROCESSING SUMMARY")
    print("=" * 65)
    print(f"Model: {MODEL}")
    print(f"Images successful: {successful}/{len(results)}")
    print(f"Batch processing time: {wall_time:.2f} seconds")
    print(f"Crop UI time (user interaction): {crop_ui_sec:.2f} seconds")
    print(f"Output folder: {run_dir}")

    if successful:
        ok_df = metrics_df[metrics_df["status"] == "OK"]
        if not ok_df.empty:
            print(f"Mean API RTT: {ok_df['api_rtt_s'].mean():.2f}s")
            if "input_tokens" in ok_df:
                print(f"Input tokens: {int(pd.to_numeric(ok_df['input_tokens'], errors='coerce').fillna(0).sum())}")
                print(f"Output tokens: {int(pd.to_numeric(ok_df['output_tokens'], errors='coerce').fillna(0).sum())}")
                print(f"Thought tokens: {int(pd.to_numeric(ok_df['thought_tokens'], errors='coerce').fillna(0).sum())}")

    print("\nMetrics:")
    print(metrics_df.to_string(index=False))
    return run_dir


# ============================================================
# 7. MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="ABB PanelSight inspection runner")
    parser.add_argument(
        "--test-images", action="store_true",
        help="Load every supported image from the project's test images folder.",
    )
    args = parser.parse_args()
    print("=" * 65)
    print("ABB PanelSight - Local Inspection Runner")
    print("=" * 65)
    print(f"Model: {MODEL}")
    print(f"Concurrency: {MAX_CONCURRENCY}")
    print(f"Output: {OUTPUT_ROOT}")

    paths = None
    if args.test_images:
        paths = sorted(
            path for path in (BASE_DIR / "test images").iterdir()
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        )
        if not paths:
            raise RuntimeError(f"No supported images found in {BASE_DIR / 'test images'}")
    originals = choose_images(paths)
    print(f"\nLoaded {len(originals)} image(s).")

    crop_ui_started = time.perf_counter()
    crop_rects = select_rois(originals)
    crop_ui_sec = time.perf_counter() - crop_ui_started

    print("\nStarting Abby processing...")
    started = time.perf_counter()
    results, wall_time = asyncio.run(run_batch(originals, crop_rects))
    batch_to_json_sec = time.perf_counter() - started
    run_dir = export_results(results, originals, wall_time, crop_ui_sec)
    end_to_end = time.perf_counter() - started
    metrics_path = run_dir / "metrics.csv"
    if metrics_path.exists():
        frame = pd.read_csv(metrics_path)
        frame["batch_processing_s"] = round(batch_to_json_sec, 6)
        frame["batch_end_to_end_s"] = round(end_to_end, 6)
        frame["crop_ui_s"] = round(crop_ui_sec, 6)
        frame.to_csv(metrics_path, index=False)
    (run_dir / "batch_summary.json").write_text(
        json.dumps({
            "batch_processing_s": round(batch_to_json_sec, 6),
            "batch_end_to_end_s": round(end_to_end, 6),
            "crop_ui_s": round(crop_ui_sec, 6),
            "images": len(originals),
        }, separators=(",", ":")),
        encoding="utf-8",
    )
    zip_path = shutil.make_archive(str(run_dir), "zip", root_dir=run_dir)
    print(f"ZIP archive: {zip_path}")
    print(f"\nEnd-to-end batch time (including export, excluding crop selection): {end_to_end:.2f}s")


if __name__ == "__main__":
    main()