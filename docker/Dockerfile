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

# ISIS3 + ALE, for the ISIS/CSM real-WAC reprojection pipeline (src/trntest/isis_wac.py; see
# docs/data-sources.md's "ISIS3/CSM spike" section and notebooks/wac_isis.py for how this was
# derived) -- a separate conda-managed env, since ISIS/ALE are conda-only packages that can't merge
# into the uv venv below. Bootstrapped via micromamba (a single static binary) rather than a full
# miniforge/mamba install, to keep this layer lean; the channel/package args themselves are
# unchanged from the original spike's proven recipe. `micromamba clean` in this same RUN layer is
# required, not optional -- cleaning in a later layer doesn't shrink an earlier one (Docker's
# layered filesystem), and the downloaded-package cache left behind otherwise roughly doubles this
# layer's real size (measured: ~2GB env + ~6GB uncleaned pkgs cache).
ENV MAMBA_ROOT_PREFIX=/opt/conda
ENV ISISROOT=/opt/conda/envs/isis
ENV ISISDATA=/workspace/cache/isisdata
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C /usr/local bin/micromamba \
    && micromamba create -y -n isis --override-channels -c usgs-astrogeology -c conda-forge \
        --channel-priority flexible isis ale \
    && micromamba clean --all --yes
ENV PATH="${ISISROOT}/bin:${PATH}"

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
RUN uv venv "${VIRTUAL_ENV}"

WORKDIR /workspace
COPY . /workspace
RUN uv pip install --no-cache -e '.[dev]'

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--NotebookApp.token=", "--NotebookApp.password="]
