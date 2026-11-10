# SQLite lives in the image/container filesystem. On Render free plan this
# disk is ephemeral: sleep or redeploy drops enterprise data. Use local
# demo or an external database for anything you need to keep.
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY backend /app/backend
COPY frontend/dist /app/frontend/dist
ENV PORT=8000
WORKDIR /app/backend
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
