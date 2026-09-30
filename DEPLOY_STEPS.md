# IntelliFleet production deployment

## Render API

Create the service from the repository using `render.yaml`, or enter these settings manually:

- Service: Web Service, `intellifleet-api`
- Region: Singapore
- Plan: Starter
- Root directory: `backend`
- Build: `pip install -r requirements.txt`
- Pre-deploy: `python -m alembic upgrade head`
- Start: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips="*"`
- Health check: `/health`
- Instances: 1; autoscaling disabled
- Secret file: mount the staff snapshot at `/etc/secrets/staff_directory.json`
- Set `STAFF_DIRECTORY_SNAPSHOT_PATH=/etc/secrets/staff_directory.json`.

Set every `sync: false` variable in `render.yaml` in Render's Environment page. Use exact frontend origins in `ALLOWED_ORIGINS`, for example `https://<pages-project>.pages.dev,https://<custom-domain>`. Never use `*`.

`backend/migrations/env.py` explicitly runs Alembic against `DATABASE_URL_DIRECT`; set that to the direct Neon connection string. Runtime queries use `DATABASE_URL`.

The service must run one worker. Do not add `--reload` in Render. Render's proxy terminates TLS; the browser connects to `wss://<api-domain>/ws/fleet`, which is forwarded to the backend WebSocket route.

## Cloudflare Pages frontend

- Framework preset: Vite
- Root directory: `artifacts/intellifleet`
- Build command: `pnpm install --frozen-lockfile && pnpm --filter @workspace/intellifleet run build`
- Output directory: `artifacts/intellifleet/dist/public` relative to the repository root, or `dist/public` when the Pages root directory is `artifacts/intellifleet`
- Node/pnpm: use the repository's pinned package manager configuration
- Build variables: `VITE_API_BASE_URL=https://<render-api-domain>`, `VITE_CARTRACK_WS_URL=wss://<render-api-domain>/ws/fleet`, and the browser-restricted `VITE_GOOGLE_MAPS_API_KEY`
- SPA fallback: `public/_redirects` contains `/*  /index.html  200`

## Vercel fallback

Import the same repository and set the project root to `artifacts/intellifleet`. Use the same build command and set the output directory to `dist/public`. `vercel.json` provides the SPA rewrite to `/index.html`.

## Verification

1. Check `GET https://<render-api-domain>/health` returns healthy and the migration revision matches the Alembic head.
2. Confirm startup logs include the exact CORS origin list, fleet cache refresh, staff source/count, and scheduler status.
3. Confirm browser login, `/app/tower`, `/app/reports`, `/app/routes`, and `/ws/fleet` over HTTPS/WSS.
4. Confirm Render has one instance and one scheduler/poller.
5. Confirm the built bundle contains no server-only `ZOHO`, `sk-`, or `postgres` secret strings.

## Repository status

The configured Git remote is:

`https://github.com/jampala-rishi-krishna/IntelliFleet-Logistics-Platform.git`

Current local branch at preparation time: `replit-agent`. Push the reviewed commit to that branch (or the team's chosen release branch) before connecting Render and Cloudflare Pages. This preparation does not claim a push was performed.
