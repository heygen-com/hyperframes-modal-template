# HyperFrames on Modal

Deploy a [HyperFrames](https://hyperframes.heygen.com) video-rendering app to [Modal](https://modal.com): an in-browser preview of a bundled composition plus an API that renders it to MP4 server-side — headless Chromium + FFmpeg, spun up per render, billed per second.

Sibling templates: [Vercel](https://github.com/heygen-com/hyperframes-vercel-template) · [Cloudflare](https://github.com/heygen-com/hyperframes-cloudflare-template) · [deploy guide](https://hyperframes.heygen.com/guides/deploy)

## Architecture

```
web browser ──► Modal web endpoint (FastAPI, @modal.asgi_app)
                 ├─ GET  /               preview page (<hyperframes-player>)
                 ├─ GET  /composition/   composition HTML + seek runtime
                 ├─ POST /api/render     spawns render function → call_id
                 ├─ GET  /api/render/:id poll (202 while running)
                 └─ GET  /renders/:name  serve MP4 from Volume
                          │
                          ▼
                render_composition (spawned per render)
                 └─ hyperframes render … --workers auto --no-browser-gpu
                          │
                          ▼
                Modal Volume (hyperframes-renders)
```

- **Image**: `node:22-bookworm-slim` + Python 3.12 + FFmpeg + the `hyperframes` CLI, with chrome-headless-shell **baked in at build time** — requests never download a browser.
- **Renders run as spawned Functions**, not inside the web request: Modal web endpoints cap at 150s per request, so `POST /api/render` returns a `call_id` immediately and the client polls. Renders get up to 15 minutes (`timeout=900`).
- **Output** lands on a Modal Volume and is served back through the web endpoint.

## Deploy

```bash
uv sync              # install modal locally
source .venv/bin/activate
modal setup          # authenticate (once)
modal deploy src/app.py
```

That's it — the deploy prints your URL, e.g. `https://<workspace>--hyperframes-modal-web.modal.run`.

For local iteration with hot reload:

```bash
modal serve src/app.py
```

Smoke test (renders a tiny bundled composition end-to-end and prints the download command):

```bash
modal run src/app.py
```

## Swap in your own composition

1. Drop your composition folder into `compositions/<name>/` (an `index.html` following the [HyperFrames composition contract](https://hyperframes.heygen.com/introduction), plus any local assets).
2. Set `PREVIEW_COMPOSITION = "<name>"` in `src/app.py`.
3. If your composition isn't 1920×1080, update the `<hyperframes-player>` dimensions in `web/index.html`.
4. `modal deploy src/app.py`.

Validate before deploying:

```bash
cd compositions/<name>
npx hyperframes check
```

## Costs & performance

- First deploy builds the image (~2 min); afterwards deploys take ~2 s and renders start in ~1 s (container cold boot) — no per-request npm installs or browser downloads.
- A 12 s 1080p30 composition renders in roughly 10-90 s depending on complexity, using 3 parallel Chrome workers with `--workers auto`.
- Containers scale to zero when idle; you pay per second of render time.
- `--no-browser-gpu` matters: Modal containers have no GPU, and without the flag the hyperframes GPU probe hangs for 180 s before falling back to software rendering.

## Roadmap

- **Fan-out rendering** — chunked parallel rendering across N containers via `@hyperframes/producer/distributed` (`plan` → `renderChunk` → `assemble`) and `Function.starmap()`, for long compositions that shouldn't wait on a single machine.

## License

[Apache-2.0](LICENSE)
