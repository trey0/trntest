FROM ghcr.io/astral-sh/uv:latest AS uv

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV ASP_VERSION=3.7.0
ENV ASP_DATE=2026-06-08
ENV ASP_DIR=/opt/StereoPipeline

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        python3 \
        curl \
        ca-certificates \
        bzip2 \
        libgl1 \
        libglu1-mesa \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Ames Stereo Pipeline: prebuilt binary release (no compiling from source).
RUN curl -fL -o /tmp/asp.tar.bz2 \
        "https://github.com/NeoGeographyToolkit/StereoPipeline/releases/download/${ASP_VERSION}/StereoPipeline-${ASP_VERSION}-${ASP_DATE}-x86_64-Linux.tar.bz2" \
    && mkdir -p "${ASP_DIR}" \
    && tar -xjf /tmp/asp.tar.bz2 -C "${ASP_DIR}" --strip-components=1 \
    && rm /tmp/asp.tar.bz2
ENV PATH="${ASP_DIR}/bin:${PATH}"

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
RUN uv venv "${VIRTUAL_ENV}"

WORKDIR /workspace
COPY . /workspace
RUN uv pip install --no-cache -e '.[dev]'

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--NotebookApp.token=", "--NotebookApp.password="]
