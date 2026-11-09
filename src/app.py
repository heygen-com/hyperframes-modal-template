"""HyperFrames on Modal — preview + render template.

Deploy:  modal deploy src/app.py
Dev:     modal serve src/app.py
Smoke:   modal run src/app.py            (renders compositions/smoke end-to-end)

Architecture (mirrors hyperframes-vercel-template / hyperframes-cloudflare-template):
- A web endpoint serves a preview page (<hyperframes-player>) for the bundled composition.
- POST /api/render spawns a render Function (headless Chromium + FFmpeg via the
  hyperframes CLI) and returns a call_id; the client polls GET /api/render/{call_id}.
  (Modal web requests cap at 150s, so spawn+poll is the blessed pattern.)
- Finished MP4s land on a Modal Volume and are served back via GET /renders/{name}.
"""

import pathlib

import modal

HYPERFRAMES_VERSION = "0.9.26"
HYPERFRAMES_NPM_PREFIX = "/opt/hyperframes-cli"
HYPERFRAMES_BIN = f"{HYPERFRAMES_NPM_PREFIX}/node_modules/.bin/hyperframes"
MINUTES = 60  # seconds

# The composition bundled into the preview page + default render target.
# Swap this to change what the template previews/renders.
PREVIEW_COMPOSITION = "modal-intro"

root = pathlib.Path(__file__).resolve().parent.parent

app = modal.App("hyperframes-modal")

# System libraries chrome-headless-shell needs on Debian bookworm.
CHROMIUM_DEPS = [
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libasound2",
    "libpango-1.0-0",
    "libcairo2",
    "libx11-6",
    "libxcb1",
    "libxext6",
    "fonts-liberation",
]

image = (
    modal.Image.from_registry("node:22-bookworm-slim", add_python="3.12")
    .apt_install("ffmpeg", "ca-certificates", *CHROMIUM_DEPS)
    .run_commands(
        f"npm install --prefix {HYPERFRAMES_NPM_PREFIX} hyperframes@{HYPERFRAMES_VERSION}",
        f"{HYPERFRAMES_BIN} browser ensure",
        f"{HYPERFRAMES_BIN} browser path",
    )
    .env(
        {
            "PATH": f"{HYPERFRAMES_NPM_PREFIX}/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"
        }
    )
    .uv_pip_install("fastapi[standard]==0.139.0")
    .add_local_dir(root / "compositions", remote_path="/compositions")
    .add_local_dir(root / "web", remote_path="/assets")
)

renders = modal.Volume.from_name("hyperframes-renders", create_if_missing=True)

RENDERS_DIR = "/renders"


@app.function(
    image=image,
    timeout=15 * MINUTES,
    volumes={RENDERS_DIR: renders},
)
def render_composition(composition: str = PREVIEW_COMPOSITION) -> str:
    """Render one bundled composition to MP4 and store it on the Volume.

    Returns the filename on the Volume (serve via GET /renders/{name}).
    """
    import shutil
    import subprocess
    import uuid

    src = pathlib.Path("/compositions") / composition
    if not src.is_dir():
        raise ValueError(f"unknown composition: {composition!r}")

    work = pathlib.Path("/tmp/render-job")
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work / "composition")

    out = work / "out.mp4"
    subprocess.run(
        [
            "hyperframes",
            "render",
            "composition",
            "-o",
            str(out),
            "--workers",
            "auto",
            # No GPU in Modal containers: skip the GPU probe, which otherwise
            # hangs to puppeteer's 180s protocolTimeout before falling back.
            "--no-browser-gpu",
        ],
        cwd=work,
        check=True,
    )

    name = f"{composition}-{uuid.uuid4().hex[:8]}.mp4"
    shutil.copy(out, f"{RENDERS_DIR}/{name}")
    renders.commit()
    return name


@app.function(image=image, volumes={RENDERS_DIR: renders})
@modal.asgi_app()
def web():
    import fastapi
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    api = fastapi.FastAPI(title="HyperFrames on Modal")

    @api.post("/api/render")
    def start_render() -> dict:
        call = render_composition.spawn(PREVIEW_COMPOSITION)
        return {"call_id": call.object_id}

    @api.get("/api/render/{call_id}")
    def poll_render(call_id: str):
        fc = modal.FunctionCall.from_id(call_id)
        try:
            name = fc.get(timeout=0)
        except TimeoutError:
            return JSONResponse({"status": "running"}, status_code=202)
        except Exception as exc:  # remote render failure surfaces here
            return JSONResponse(
                {"status": "failed", "error": str(exc)}, status_code=500
            )
        return {"status": "done", "url": f"/renders/{name}"}

    @api.get("/renders/{name}")
    def get_render(name: str):
        if "/" in name or ".." in name:
            raise fastapi.HTTPException(status_code=400)
        renders.reload()
        path = pathlib.Path(RENDERS_DIR) / name
        if not path.is_file():
            raise fastapi.HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    @api.get("/composition/")
    def composition_html():
        """Serve the preview composition with the seek runtime injected.

        The <hyperframes-player> drives the composition frame-by-frame via the
        hyperframe runtime; compositions don't bake it in, so inject it here
        (same trick as the Vercel template's normalizePreviewHtml).
        """
        html = pathlib.Path(
            f"/compositions/{PREVIEW_COMPOSITION}/index.html"
        ).read_text()
        runtime = (
            '<script src="https://cdn.jsdelivr.net/npm/@hyperframes/core@'
            f'{HYPERFRAMES_VERSION}/dist/hyperframe.runtime.iife.js"></script>'
        )
        html = html.replace("</head>", f"{runtime}\n</head>", 1)
        return HTMLResponse(html)

    # Route order matters: the explicit /composition/ route above wins over this
    # mount, which serves the composition's relative assets (assets/*.png, ...).
    api.mount(
        "/composition",
        StaticFiles(directory=f"/compositions/{PREVIEW_COMPOSITION}"),
        name="composition-assets",
    )
    api.mount("/", StaticFiles(directory="/assets", html=True), name="frontend")

    return api


@app.local_entrypoint()
def smoke(composition: str = "smoke"):
    """End-to-end smoke test: render the tiny bundled composition remotely."""
    print(f"rendering {composition!r} on Modal ...")
    name = render_composition.remote(composition)
    print(f"done: {name}")
    print(f"download: modal volume get hyperframes-renders {name} {name}")
