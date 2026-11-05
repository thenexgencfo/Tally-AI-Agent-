# Tally Agent API. Non-root, no build tools in the final image.
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 agent \
 && useradd --system --uid 10001 --gid agent --home-dir /app --shell /usr/sbin/nologin agent

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-deps -r requirements.lock
COPY app ./app

USER agent
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "2", "--no-server-header", "--no-access-log"]
