# Google Search Console MCP — built from a pinned upstream commit with pinned deps.
# Replaces upstream's Dockerfile, which floats its uv base image and runs
# `uv sync` without its uv.lock (so deps resolve fresh every build). See README.md to update.
FROM python:3.13.18-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

# https://github.com/AminForou/mcp-gsc
ARG GSC_COMMIT=d49eea9e5efb5eb645b722249303179b2c150b49

WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock \
 && pip check \
 && rm /tmp/requirements.lock

ADD https://github.com/AminForou/mcp-gsc/archive/${GSC_COMMIT}.tar.gz /tmp/src.tar.gz
RUN tar -xzf /tmp/src.tar.gz -C /tmp \
 && cp /tmp/mcp-gsc-${GSC_COMMIT}/gsc_server.py /app/ \
 && rm -rf /tmp/src.tar.gz /tmp/mcp-gsc-${GSC_COMMIT}

CMD ["python", "gsc_server.py"]
