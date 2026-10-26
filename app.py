import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import modal
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

# Constants inside the container
JAR_PATH = "/opt/midis2jam2/midis2jam2.jar"
SOUNDFONT_DIR = "/opt/soundfonts"
SOUNDFONT_PATH = f"{SOUNDFONT_DIR}/Timbres of Heaven (XGM) 4.00(G).sf2"

MIDIS2JAM2_URL = "https://github.com/wyskoj/midis2jam2/releases/download/v2.2.0/midis2jam2-linux-x64-2.2.0-release.jar"
SOUNDFONT_URL = "https://midkar.com/SoundFonts/Timbres%20of%20Heaven%20(XGM)%204.00(G).7z"

# 1. Define the Ubuntu Image and bake in midis2jam2 + SoundFont
image = (
    modal.Image.from_registry("ubuntu:22.04", add_python="3.11")
    .env(
        {
            "DEBIAN_FRONTEND": "noninteractive",
            # Crucial for OpenGL & hardware video encoding on Modal GPUs
            "NVIDIA_DRIVER_CAPABILITIES": "all",
            # Prevents OpenAL from crashing in a headless container without a soundcard
            "ALSOFT_DRIVERS": "null",
            # Help internal JVM spawns find classes
            "CLASSPATH": JAR_PATH,
        }
    )
    .apt_install(
        "curl",
        "ca-certificates",
        "p7zip-full",
        "openjdk-21-jre",
        "ffmpeg",
        "xvfb",
        "xauth",
        # OpenGL / Mesa / X11 dependencies required by LWJGL3 (JMonkeyEngine)
        "libgl1",
        "libglx-mesa0",
        "libegl1",
        "libgles2",
        "mesa-utils",
        "libxcursor1",
        "libxrandr2",
        "libxinerama1",
        "libxi6",
        "libxxf86vm1",
        "libxtst6",
        "libxrender1",
        # Audio & Font dependencies required by Java / OpenAL
        "libasound2",
        "libopenal1",
        "fontconfig",
        "fonts-dejavu-core",
    )
    .run_commands(
        # Download midis2jam2 v2.2.0 JAR
        f"mkdir -p /opt/midis2jam2 && curl -fSL '{MIDIS2JAM2_URL}' -o '{JAR_PATH}'",
        # Download and extract the Timbres of Heaven SoundFont
        f"mkdir -p '{SOUNDFONT_DIR}'",
        f"curl -fSL '{SOUNDFONT_URL}' -o /tmp/soundfont.7z",
        # '7z e' extracts the .sf2 directly into SOUNDFONT_DIR regardless of subfolder structure
        f"7z e /tmp/soundfont.7z -o'{SOUNDFONT_DIR}' '*.sf2' -r",
        "rm /tmp/soundfont.7z",
        f"test -f '{SOUNDFONT_PATH}'",  # Verify the soundfont exists at build time
    )
    .pip_install("fastapi[standard]", "python-multipart")
)

app = modal.App("midis2jam2-visualizer", image=image)
web_app = FastAPI(
    title="midis2jam2 MIDI Visualizer API",
    description="Render 3D MIDI visualizations to MP4 using midis2jam2 v2.2.0",
)

VALID_RESOLUTIONS = {"720p", "1080p", "1440p", "2160p"}
# We use string literals instead of integers for FPS and Quality options to match the expected input format.
ValidFPS = Literal["24", "25", "30", "50", "60", "120", "144", "240"]
ValidQuality = Literal["low", "medium", "high", "maximum"]


def validate_resolution(res: str) -> tuple[int, int]:
    """
    Validates the --resolution argument and returns the (width, height)
    so Xvfb can be sized appropriately.
    """
    preset_map = {
        "720p": (1280, 720),
        "1080p": (1920, 1080),
        "1440p": (2560, 1440),
        "2160p": (3840, 2160),
    }
    if res in preset_map:
        return preset_map[res]

    match = re.match(r"^(\d+)x(\d+)$", res)
    if not match:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid resolution. Must be 720p, 1080p, 1440p, 2160p, "
                "or WIDTHxHEIGHT (e.g., 1920x1080)."
            ),
        )

    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0 or width % 2 != 0 or height % 2 != 0:
        raise HTTPException(
            status_code=400,
            detail="Custom resolution width and height must be positive even integers.",
        )

    return width, height


@web_app.get("/", response_class=HTMLResponse)
async def index():
    """Simple browser UI for uploading a MIDI file and downloading the MP4."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>midis2jam2 Visualizer</title>
        <style>
            body { font-family: system-ui, sans-serif; max-width: 500px; margin: 40px auto; padding: 0 20px; }
            label { display: block; margin-top: 15px; font-weight: bold; }
            input, select, button { width: 100%; padding: 8px; margin-top: 5px; box-sizing: border-box; }
            button { margin-top: 25px; background: #2563eb; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; padding: 12px; }
            button:hover { background: #1d4ed8; }
        </style>
    </head>
    <body>
        <h2>midis2jam2 MIDI Visualizer</h2>
        <form action="/visualize" method="post" enctype="multipart/form-data">
            <label>MIDI File (.mid / .midi)</label>
            <input type="file" name="midi_file" accept=".mid,.midi" required>

            <label>Resolution (720p, 1080p, 1440p, 2160p, or WxH)</label>
            <input type="text" name="resolution" value="1080p" required>

            <label>FPS</label>
            <select name="fps">
                <option value="24">24</option>
                <option value="25">25</option>
                <option value="30">30</option>
                <option value="50">50</option>
                <option value="60" selected>60</option>
                <option value="120">120</option>
                <option value="144">144</option>
                <option value="240">240</option>
            </select>

            <label>Quality</label>
            <select name="quality">
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high" selected>high</option>
                <option value="maximum">maximum</option>
            </select>

            <button type="submit">Render MP4</button>
        </form>
    </body>
    </html>
    """


@web_app.post("/visualize")
async def visualize_midi(
    midi_file: UploadFile = File(..., description="The .mid or .midi file to visualize"),
    resolution: str = Form(
        "1080p",
        description="720p, 1080p, 1440p, 2160p, or even WxH such as 1920x1080",
    ),
    fps: ValidFPS = Form(
        60,
        description="Frame rate: 24, 25, 30, 50, 60, 120, 144, or 240",
    ),
    quality: ValidQuality = Form(
        "high",
        description="Video quality: low, medium, high, or maximum",
    ),
):
    if not midi_file.filename or not midi_file.filename.lower().endswith((".mid", ".midi")):
        raise HTTPException(status_code=400, detail="Uploaded file must be a .mid or .midi file.")

    width, height = validate_resolution(resolution)

    # Create a temporary working directory that cleans up after the response is sent
    temp_dir = tempfile.mkdtemp(prefix="midis2jam2_")
    try:
        input_midi_path = os.path.join(temp_dir, "input.mid")
        output_mp4_path = os.path.join(temp_dir, "output.mp4")

        # Save the uploaded MIDI file
        with open(input_midi_path, "wb") as f:
            shutil.copyfileobj(midi_file.file, f)

        # Ensure the virtual X11 screen is at least as large as the target video resolution
        xvfb_w = max(width, 1920)
        xvfb_h = max(height, 1080)

        cmd = [
            "xvfb-run",
            "-a",
            "-s",
            f"-screen 0 {xvfb_w}x{xvfb_h}x24 +iglx",
            "java",
            "-cp",
            JAR_PATH,
            "-Djava.awt.headless=false",
            "org.wysko.midis2jam2.MainKt",
            input_midi_path,
            "--record",
            output_mp4_path,
            "--resolution",
            resolution,
            "--fps",
            str(fps),
            "--quality",
            quality,
            "--soundbank",
            SOUNDFONT_PATH,
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1700,
        )

        if proc.returncode != 0 or not os.path.exists(output_mp4_path):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "midis2jam2 failed to render the video.",
                    "return_code": proc.returncode,
                    "stdout": proc.stdout[-2000:],
                    "stderr": proc.stderr[-2000:],
                },
            )

        output_filename = f"{Path(midi_file.filename).stem}.mp4"

        # Return the MP4 file and delete temp_dir in the background once download finishes
        return FileResponse(
            path=output_mp4_path,
            media_type="video/mp4",
            filename=output_filename,
            background=BackgroundTask(shutil.rmtree, temp_dir, True),
        )

    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# 2. Attach the FastAPI app to Modal with an L40S GPU (fastest graphics-capable GPU)
@app.function(
    gpu="L40S",
    timeout=1800,  # 30-minute timeout for high-res / high-fps renders
    memory=16384,  # 16 GB RAM
)
@modal.asgi_app()
def fastapi_app():
    return web_app