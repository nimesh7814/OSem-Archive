# OpenSenseMap — Frontend (Vite + React)

Short description

- Frontend for the OSeM Archive: browse sensor stations, view readings and maps, run analyses, and export data.
- Built with Vite + React, Tailwind CSS, Radix UI components, Recharts and Leaflet for maps.

Prerequisites

- Node 18+ (tested with v24.x)
- pnpm (preferred; `pnpm-lock.yaml` is included)

Quick install

```powershell
# from this folder (frontend)
pnpm install
```

Development

```powershell
# start dev server (auto-selects a free port if 5173 is in use)
pnpm dev
# open the shown Local URL (e.g. http://127.0.0.1:5173/)
```

Notes

- The dev server proxies several API routes to a backend expected at `http://localhost:8000` (see `vite.config.ts`). If you see "http proxy error: /summary ECONNREFUSED", start the backend on port 8000 or update the proxy targets.
- Vite may choose a different port if the default is busy.

Build & Preview

```powershell
# production build
pnpm build
# preview the production bundle locally
pnpm preview
```

Docker

```powershell
# build the image
docker build -t osem-archive-frontend .

# run it on port 8080
docker run --rm -p 8080:80 osem-archive-frontend
```

Or use Docker Compose:

```powershell
docker compose up --build
```

By default Compose publishes the app on port 8081 to avoid conflicts with other local services. If you want a different host port, set `HOST_PORT` before starting Compose:

```powershell
$env:HOST_PORT=8080
docker compose up --build
```

If the frontend should talk to an API on another host, set `VITE_API_BASE_URL` at build time:

```powershell
docker build --build-arg VITE_API_BASE_URL=https://api.example.com -t osem-archive-frontend .
```
