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

# torch/torchvision, real CPU-only wheels for the LightGlue tie-point matcher
# (src/trntest/pose_alignment.py's match_features_lightglue) -- installed from PyTorch's own CPU
# wheel index in this dedicated step, *before* the main project install below. `uv pip install`
# (the pip-compatible interface used here, not the `uv add`/`uv sync` project workflow) doesn't
# read pyproject.toml's `[tool.uv.sources]`/`[[tool.uv.index]]`, so a plain `-e '.[dev]'` install
# with `torch`/`torchvision` just listed as normal dependencies would resolve them from PyPI's
# default index instead -- which serves the CUDA-bundled build (multi-GB, several `nvidia-*`
# runtime packages, all dead weight with no GPU/NVIDIA driver present in this container). Installing
# the correct CPU wheels here first means the main install below finds them already satisfied and
# leaves them alone.
RUN uv pip install --no-cache torch torchvision --index-url https://download.pytorch.org/whl/cpu

WORKDIR /workspace
COPY . /workspace
RUN uv pip install --no-cache -e '.[dev]'
# LightGlue itself: pinned to a specific commit (its own pyproject.toml has no released version,
# `version = "0.0"`, so a git ref is the only reproducible way to pin it) rather than the plain
# PyPI index, since there is no official `lightglue` PyPI package -- the upstream-recommended
# install is `git clone` + `pip install -e .` (see docs/data-sources.md's "LightGlue tie-point
# matching" section). Deliberately `--no-deps` and deliberately *not* also listed in
# pyproject.toml's own `dependencies`: its declared `requirements.txt` (torch/torchvision/numpy/
# matplotlib/kornia/opencv-python) would reinstall a conflicting `opencv-python` alongside this
# project's own `opencv-python-headless` (both provide the `cv2` import, colliding on install) and a
# separately-pinned `kornia`/`torch`/`torchvision`. Everything `import lightglue` actually needs at
# runtime is already satisfied by this project's own pyproject.toml dependencies (torch/torchvision/
# kornia/numpy/matplotlib) plus `opencv-python-headless` for `cv2`.
RUN uv pip install --no-cache --no-deps \
        'lightglue @ git+https://github.com/cvg/LightGlue.git@eb42fee2d71449efb0aa5c10549752b5d75384d8'

# Redirects torch.hub's pretrained-weight cache (LightGlue's matcher weights, DISK's extractor
# weights via kornia.feature.DISK.from_pretrained -- both real network fetches on first use, tens of
# MB total) under this project's own shared cache/ convention (see docs/caching.md) instead of
# torch's own default `~/.cache/torch`, which would live inside the container and be re-fetched on
# every rebuild.
ENV TORCH_HOME=/workspace/cache/torch

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--NotebookApp.token=", "--NotebookApp.password="]
