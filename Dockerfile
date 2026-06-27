FROM python:3.11-slim

LABEL org.opencontainers.image.title="ccc-layered-storage"
LABEL org.opencontainers.image.description="Optional dev/test image for CCC layered storage"

WORKDIR /workspace/ccc-layered-storage

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fuse-overlayfs \
        fuse3 \
        squashfs-tools \
        squashfuse \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY deploy ./deploy
COPY Makefile ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -e '.[dev]'

CMD ["sh", "-lc", "make test"]
