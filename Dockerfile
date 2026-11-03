# GPU image (ghcr.io/dancue44/nobookends-sync:latest and :cuda).
#
# CUDA 12 runtime + cuDNN 9: CTranslate2 4.x wheels are built against CUDA 12.
# CUDA 12 still supports Pascal (Quadro P2000, compute capability 6.1); do not
# move to a CUDA 13 base, which drops Pascal.
#
# Builds with the classic builder too (Unraid has no buildx): no BuildKit-only
# syntax in here.
ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# ---------------------------------------------------------------- build
FROM ${BASE_IMAGE} AS build

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall -y pip \
 && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} +

# ---------------------------------------------------------------- runtime
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    HF_HOME=/data/models/hf \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY nobookends_sync ./nobookends_sync

LABEL org.opencontainers.image.title="NoBookEnds Sync" \
      org.opencontainers.image.description="Aligns audiobook narration to EPUB text for NoBookEnds Listen & Read (CUDA)" \
      org.opencontainers.image.source="https://github.com/DanCue44/NoBookEnds-Sync"

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "nobookends_sync"]
