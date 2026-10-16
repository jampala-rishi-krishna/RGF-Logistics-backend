# IntelliFleet production deployment

## Render API

Create the service from the repository using `render.yaml`, or enter these settings manually:

- Service: Web Service, `intellifleet-api`
- Region: Singapore
- Plan: Starter
- Root directory: `backend`
- Build: `pip install -r requirements.txt`
- Pre-deploy: `python -m alembic upgrade head`
- Start: `python -m uvicorn main:asgi_app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips="*"` (`main:app` still works; `main:asgi_app` additionally guarantees CORS headers on every 500 - see "CORS, request ids and memory logging" below)
- Health check: `/health`
- Instances: 1; autoscaling disabled
- Secret file: mount the staff snapshot at `/etc/secrets/staff_directory.json`
- Set `STAFF_DIRECTORY_SNAPSHOT_PATH=/etc/secrets/staff_directory.json`.

Set every `sync: false` variable in `render.yaml` in Render's Environment page. Use exact frontend origins in `CORS_ALLOWED_ORIGINS` (or the older `ALLOWED_ORIGINS`), for example `https://<pages-project>.pages.dev,https://<custom-domain>`. Never use `*`.

### CORS, request ids and memory logging

| Variable | Required | Value |
| --- | --- | --- |
| `CORS_ALLOWED_ORIGINS` | yes (or `ALLOWED_ORIGINS`) | Exact origins, comma-separated, e.g. `https://rare-logistics-frontend.rishi-315.workers.dev,https://rare-logistics-frontend.pages.dev`. Whitespace and a trailing `/` are stripped. `*` refuses to start (credentials are allowed). |
| `ALLOWED_ORIGINS` | legacy | Read only when `CORS_ALLOWED_ORIGINS` is unset. Production still works on this name unchanged. |
| `CORS_ALLOW_ORIGIN_REGEX` | no (default: none) | Only this exact value is accepted: `^https://([a-z0-9-]+\.)?rare-logistics-frontend\.(rishi-315\.workers|pages)\.dev$` (allows Pages preview subdomains). Any other value stops startup. |
| `MEMORY_DEBUG_LOG` | no (default `0`) | `1` logs process RSS every 60 s and wherever code calls `memory_debug.log_rss(tag)`. `/health` always reports `rss_mb`. |

Every response carries `X-Request-ID` (exposed to the browser via `Access-Control-Expose-Headers`). Unhandled errors return `500 {"detail":"Server error","code":"internal_error","request_id":...}`; the traceback is logged with the same `request_id`, never sent to the browser.

If the Render dashboard has its own Start Command, it overrides `render.yaml`: change it to `main:asgi_app` there too. Leaving it on `main:app` is safe (a global exception handler adds CORS headers itself), `main:asgi_app` is the belt-and-braces option.

`/ws/fleet` accepts an optional `?token=<JWT>`; an invalid/expired token closes with code 4401, a disallowed browser Origin with 4403. Sockets without a token are still accepted (the deployed frontend sends none).

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
