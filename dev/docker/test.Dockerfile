FROM python:3.11-slim

LABEL org.opencontainers.image.title="ccc-storage"
LABEL org.opencontainers.image.description="Optional dev/test image for CCC layered storage"

WORKDIR /workspace/ccc-storage

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        fuse-overlayfs \
        fuse3 \
        gcc \
        libfuse3-dev \
        make \
        pkg-config \
        squashfs-tools \
        squashfuse \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY .github ./.github
COPY src ./src
COPY tests ./tests
COPY deploy ./deploy
COPY dev ./dev
COPY Makefile ./

RUN python -m pip install --upgrade pip \
    && python -m pip install -e '.[dev,manifest,s3,fuse]'

CMD ["sh", "-lc", "make test"]
