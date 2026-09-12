# Ticket OPS-4127: status site is down after last night's nginx change

The public status site is served by nginx on port 80 in this machine. Since a
config change last night, users report the site is down. Please get it working
again.

## How the site is supposed to work

- `GET /` serves `/var/www/app/index.html`.
- `GET /static/<file>` serves files from `/var/www/app/static/`
  (for example `/static/style.css`, including nested paths under subdirectories).
  Requests for static files that do not exist must return 404.
- `GET /api/...` is reverse-proxied to the status backend, which listens on
  `127.0.0.1:8081`. The backend answers `/api/health` with
  `{"status": "ok", "nonce": "<value chosen by the running backend>"}` and
  `/api/items` with a JSON list. Its responses, including its 404s, must reach
  the client unchanged.

The nginx site config is `/etc/nginx/sites-enabled/app.conf`. nginx logs go to
`/var/log/nginx/`.

## Constraints

- The backend at `/opt/backend/app.py` belongs to the platform team. Do not
  modify it and do not move it to another port. If it is not running, start it
  yourself for testing with `python3 /opt/backend/app.py &`.
- Keep the routing rules above; do not serve `/api/` responses from nginx
  itself.

## Done means

`nginx -t` passes and all of the routes above return the correct content.
Your fix is graded in a fresh container: the grader collects your on-disk
`/etc/nginx`, `/var/www/app`, and `/opt/backend/app.py`, starts a fresh
backend on 8081 with a secret it chooses, verifies served root and static
routes against your submitted on-disk files as well as fresh probe files
written to `/var/www/app/static/` (confirming missing static files return 404),
and starts nginx from your collected config. Anything that only lives in a
running process is lost.
