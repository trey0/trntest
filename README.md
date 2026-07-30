# trntest — lunar remote sensing demo

Generates a synthetic lunar satellite image, posed using the real LRO SPICE trajectory at the
time of an actual LROC WAC image, rendered with NASA's Ames Stereo Pipeline (`sat_sim`) from real
DEM/imagery pulled from the Lunaserv WMS server. See `docs/plan.md` for the full approach and
status, and `CLAUDE.md` for how the docs in this repo are organized.

## Build & run

All tooling (GDAL, ASP, SPICE) lives in a Docker container — nothing needs installing on the host
beyond Docker itself.

```sh
cd docker
docker compose build
docker compose up -d
```

Jupyter Lab then listens on the container's port 8888, mapped to `127.0.0.1:8888` on this host
only (no auth token is set, so it's intentionally not exposed on the public interface). From your
own machine:

```sh
ssh -L 8888:localhost:8888 <this-host>
```

then open `http://localhost:8888` in a browser. Open `notebooks/lunar_sat_sim_demo.ipynb`.

For one-off commands instead of the notebook server:

```sh
docker compose run --rm demo sat_sim --help
docker compose run --rm demo gdalinfo --version
```

`docker-compose.yml` mounts the repo at `/workspace` and a `docker/cache/` directory at
`/workspace/cache` — fetched WMS tiles and SPICE kernels persist there across container rebuilds
(see `docs/caching.md`).
