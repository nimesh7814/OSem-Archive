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
