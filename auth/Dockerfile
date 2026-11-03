# Pinned: base image digest + full lockfile. See README.md to update.
FROM python:3.11.18-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
 && pip check \
 && rm /tmp/requirements.lock

COPY auth.py .
CMD ["uvicorn", "auth:app", "--host", "0.0.0.0", "--port", "9000"]
