# SQLite lives in the image/container filesystem. Free-plan disks are
# ephemeral: sleep, wake (possibly a new container), restart or redeploy may
# drop enterprise data. Use the local one-click launcher for competition
# demos and anything you need to keep; the public instance is for simulated
# or demo data only. Data may survive a same-container pause, but this is
# not guaranteed across sleep/wake, restart, or redeploy.
#
# HTTPS production (SnapDeploy / Render, etc.):
#   LTZS_COOKIE_SECURE=1
#   LTZS_PUBLIC_ORIGIN=https://your-public-host
# Do not trust client-supplied X-Forwarded-Proto for CSRF origin checks.
# Do not print cookie values in logs.
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend /app/backend
COPY frontend/dist /app/frontend/dist
ENV PORT=8000
WORKDIR /app/backend
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
