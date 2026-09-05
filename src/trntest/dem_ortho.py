"""Fetch DEM + ortho imagery for the ground footprint computed by `camera.build_camera`, and prep both
for `sat_sim`: the DEM as elevation (not raw radius) and hole-filled, the ortho despeckled and blended
with a sun-lit hillshade (`hapke.despeckle_and_shade_ortho`). Live defaults: Astropedia's GLD100 DEM
(`dem_gld100.fetch_dem_astropedia`) and WAC_EMP's PDS4 reflectance ortho
(`ortho_wac_emp.fetch_wac_emp_reflectance`); Lunaserv WMS (`lunaserv_wms.fetch_dem_native`,
`ortho_source="lunaserv_wms"`) is a deprecated fallback kept for comparison. See
docs/data-sources/astropedia-gld100.md, docs/data-sources/wac-emp-pds4.md,
docs/data-sources/lunaserv-wms.md, and docs/caching.md.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import rasterio

from trntest import cache
from trntest.config import MOON_RADIUS_M, TrntestConfig, load_config
from trntest.dem_gld100 import fetch_dem_astropedia, reproject_astropedia_elevation_to_local_grid
from trntest.geo_utils import footprint_bbox_local_m, pad_bbox, pixel_dims_for_gsd, union_bbox
from trntest.hapke import (
    DEFAULT_ALONG_TRACK_CORRECTION,
    DEFAULT_HAPKE_SHADING,
    DEFAULT_REAL_HAPKE_PARAMS,
    despeckle_and_shade_ortho,
)
from trntest.ortho_wac_emp import fetch_wac_emp_reflectance, reproject_wac_emp_reflectance_to_local_grid
from trntest.product_registry import atomic_publish_prefix, writes_product
from trntest.subprocess_utils import run_quiet

if TYPE_CHECKING:
    from trntest.camera import Camera

# `fetch_dem_and_ortho`'s ortho-texture source. "wac_emp_pds" (live default) fetches WAC_EMP's own
# reflectance directly from its PDS4 archive (`ortho_wac_emp.fetch_wac_emp_reflectance`/
# `reproject_wac_emp_reflectance_to_local_grid`) -- physical reflectance, no embedded display stretch.
# "lunaserv_wms" is the deprecated fallback (the original `luna_wac_normalized_reflectance` WMS
# layer), kept reachable for comparison but carrying an uncorrected affine display stretch, not raw
# reflectance -- see docs/data-sources/lunaserv-wms.md.
DEFAULT_ORTHO_SOURCE = "wac_emp_pds"
ORTHO_SOURCES = ("wac_emp_pds", "lunaserv_wms")


def hole_fill_dem(dem_path, filled_path):
    """Hole-fill `dem_path` via ASP's `dem_mosaic`, writing to `filled_path`.

    :param dem_path: Input DEM GeoTIFF.
    :param filled_path: Output path; must end in exactly `-tile-0.tif` (`dem_mosaic`'s own `-o
        <prefix>` convention appends that suffix to whatever prefix it's given).
    """
    # `atomic_publish_prefix` builds a temp prefix the same way, so this is atomic despite the
    # prefix-based (not exact-path) tool convention that `atomic_publish_path`'s own contract doesn't
    # directly fit -- see that helper's own docstring.
    filled_path = Path(filled_path)
    with atomic_publish_prefix(filled_path, "-tile-0.tif") as tmp_prefix:
        run_quiet(["dem_mosaic", str(dem_path), "--hole-fill-length", "50", "-o", str(tmp_prefix)])


def ortho_shaded_filename(
    hapke: bool,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> str:
    """The `output_dir`-relative filename `hapke.despeckle_and_shade_ortho` writes its shaded ortho to.

    :param hapke: Whether Hapke shading (vs. plain Lambertian) was used.
    :param along_track_correction: Whether the along-track view-direction correction was applied.
        Only affects the filename when `hapke=True`.
    :param real_hapke_params: Whether calibrated (vs. placeholder) Hapke parameters were used. Only
        affects the filename when `hapke=True`.
    :param ortho_source: Which ortho/texture source was fetched (see `ORTHO_SOURCES`).
    :returns: The filename `hapke.despeckle_and_shade_ortho` writes to for this combination.
    """
    # Factored out so `trn_dataset.TrnTestEntry.dem_ortho_result`'s resumption check can ask for
    # exactly the file a matching `fetch_dem_and_ortho` call would produce, without duplicating this
    # naming logic. Each parameter that changes shading behavior gets its own suffix, deliberately:
    # this prevents a cached file written under an old default from silently being resumed as if it
    # matched a newer one. `_normaltilt` is always appended when `hapke=True`, independent of any
    # parameter -- kept as a permanent marker even though the correction it names is now unconditional,
    # since older cached files without it already exist on disk under other suffix combinations and
    # must not be resumed as if they matched. `_wacemp` is appended whenever
    # `ortho_source="wac_emp_pds"`, independent of `hapke`, since the input texture's numeric convention
    # (reflectance, not WMS DN) changes regardless of which shading mode blends it;
    # `ortho_source="lunaserv_wms"` keeps the original, suffix-less filenames.
    wacemp_suffix = "_wacemp" if ortho_source == "wac_emp_pds" else ""
    if not hapke:
        return f"ortho_shaded{wacemp_suffix}.tif"
    suffix = ("_atc" if along_track_correction else "") + ("_realparams" if real_hapke_params else "") + "_normaltilt"
    return f"ortho_shaded_hapke{suffix}{wacemp_suffix}.tif"


@dataclasses.dataclass(frozen=True)
class DemOrthoResult:
    """DEM/ortho tiles fetched for a `Camera`'s footprint, as returned by `fetch_dem_and_ortho`.

    :ivar ortho: Path to the shaded ortho GeoTIFF.
    :ivar dem: Path to the hole-filled DEM GeoTIFF.
    :ivar bbox: `(minx, miny, maxx, maxy)`, meters, in the per-camera local Orthographic CRS
        (`config.lunaserv_srs_template`) both tiles were fetched in -- not lon/lat degrees.
    :ivar width: Raster width, pixels.
    :ivar height: Raster height, pixels.
    """

    # Each `DemOrthoResult`'s tiles have their own independent local CRS, centered on that camera's
    # own footprint.

    ortho: Path
    dem: Path
    bbox: tuple
    width: int
    height: int


def result_from_files(ortho_path: Path, dem_path: Path) -> DemOrthoResult:
    """Reconstruct a `DemOrthoResult` from an already-generated ortho/DEM pair on disk -- pure IO, no
    fetching.

    :param ortho_path: Shaded ortho GeoTIFF path.
    :param dem_path: Hole-filled DEM GeoTIFF path.
    :returns: A `DemOrthoResult` with `bbox`/`width`/`height` read back from `ortho_path`'s own
        embedded georeferencing.
    """
    # So `trn_dataset.TrnTestEntry.dem_ortho_result` can resume from a prior `generate()` run's output
    # instead of re-fetching from Lunaserv/Astropedia. `bbox`/`width`/`height` are read back rather than
    # recomputed or stored separately: `geo_utils.reproject_raster_to_local_grid` (via
    # `hapke.despeckle_and_shade_ortho`, which carries the fetched ortho's own `profile` through
    # unchanged) writes `dst_transform`/`dst_crs` from exactly this same `bbox`/`width`/`height` at
    # fetch time, so reading them back from the file is an exact round-trip -- the same "raster's own
    # georeferencing is authoritative" pattern `isis_wac._orthographic_map_pvl` relies on elsewhere.
    with rasterio.open(ortho_path) as src:
        width, height = src.width, src.height
        bbox = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
    return DemOrthoResult(ortho=Path(ortho_path), dem=Path(dem_path), bbox=bbox, width=width, height=height)


@dataclasses.dataclass(frozen=True)
class DemFetchResult:
    """The entry's one DEM, as returned by `fetch_dem`.

    :ivar dem: Path to the hole-filled DEM GeoTIFF.
    :ivar bbox: `(minx, miny, maxx, maxy)`, meters, the padded local-CRS working grid it was fetched
        onto -- `fetch_and_shade_ortho` must reuse this exactly (never re-derive) for its own ortho
        fetch, so the two can't disagree about the AOI.
    :ivar width: Raster width, pixels.
    :ivar height: Raster height, pixels.
    """

    dem: Path
    bbox: tuple
    width: int
    height: int


@writes_product("dem_filled")
def fetch_dem(
    camera: Camera, config: TrntestConfig | None = None, extra_footprint_lonlat_deg: dict | None = None
) -> DemFetchResult:
    """The entry's one DEM fetch.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config; `load_config()` if not given.
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :returns: A `DemFetchResult` for the fetched, hole-filled DEM.
    """
    # Split out of the old combined `fetch_dem_and_ortho` so `product_registry` has exactly one
    # legible, checkable writer for the `"dem_filled"` label (principle 2), decoupled from the
    # ortho-shading concern (`fetch_and_shade_ortho`, an intentional variant family -- multiple valid
    # shaded orthos by design, principle 1) that used to be fused into the same function.
    #
    # Still takes `extra_footprint_lonlat_deg` as a caller-suppliable parameter -- principle 1's "no
    # caller-supplied parameter should be able to change identity" isn't fully closed by this split.
    # `dem_filled_path`'s own filename still doesn't encode this parameter (unlike
    # `ortho_shaded_filename`'s suffix discipline for its own parameters), so two calls against the
    # same output directory with different footprints can still silently disagree about "the" DEM --
    # see `docs/proposed-tasks/open-items.md` for what a full fix would need. Not solved here: this phase only
    # makes the current single writer legible/auditable (`writes_product`) and its file write atomic
    # (`atomic_publish`, in `dem_gld100.reproject_astropedia_elevation_to_local_grid`), not the
    # filename-collision gap itself -- flagged rather than silently assumed fixed.
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    unpadded_bbox = footprint_bbox_local_m(camera.footprint_lonlat_deg, center_lon, center_lat, MOON_RADIUS_M)
    if extra_footprint_lonlat_deg is not None:
        unpadded_bbox = union_bbox(
            unpadded_bbox,
            footprint_bbox_local_m(extra_footprint_lonlat_deg, center_lon, center_lat, MOON_RADIUS_M),
        )
    bbox = pad_bbox(unpadded_bbox, config.dem_padding_fraction)
    width, height = pixel_dims_for_gsd(bbox, config.dem_target_gsd_m)
    print(f"ROI center (lon,lat deg): {center}, bbox (local m): {bbox}")
    print(f"ROI size {width}x{height} px (~{config.dem_target_gsd_m} m/px)")

    # Live default DEM source: USGS Astropedia's flat-file GLD100, not Lunaserv's WMS -- see
    # docs/data-sources/astropedia-gld100.md. `fetch_dem_astropedia` ensures the whole ~10GB file is
    # downloaded/cached locally once (raises if this camera's footprint needs data outside the file's
    # +-79 deg latitude coverage -- no silent fallback to the deprecated Lunaserv-native path), then
    # `reproject_astropedia_elevation_to_local_grid` reads just this AOI from the local file and
    # reprojects it onto this same local-CRS grid -- already elevation (not planetocentric radius), so
    # `lunaserv_wms.radius_to_elevation` is skipped.
    astropedia_path, astropedia_deg_bbox = fetch_dem_astropedia(bbox, center_lon, center_lat, config)
    dem_elevation_path = config.output_dir / "dem_elevation.tif"
    reproject_astropedia_elevation_to_local_grid(
        astropedia_path,
        astropedia_deg_bbox,
        bbox,
        width,
        height,
        center_lon,
        center_lat,
        MOON_RADIUS_M,
        dem_elevation_path,
    )

    dem_filled_path = config.output_dir / "dem_filled-tile-0.tif"
    hole_fill_dem(dem_elevation_path, dem_filled_path)
    return DemFetchResult(dem=dem_filled_path, bbox=bbox, width=width, height=height)


@writes_product("ortho_shaded")
def fetch_and_shade_ortho(
    camera: Camera,
    dem: DemFetchResult,
    config: TrntestConfig | None = None,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> DemOrthoResult:
    """The ortho-shading half of the old combined `fetch_dem_and_ortho`, split out alongside
    `fetch_dem` -- see that function's own docstring for why.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param dem: `fetch_dem`'s output; its `bbox`/`width`/`height` are reused exactly, never re-derived.
    :param config: Project config; `load_config()` if not given.
    :param hapke: Use ISIS `photomet`'s Hapke model (the default, via
        `hapke.despeckle_and_shade_ortho`'s `hapke` passthrough); `hapke=False` falls back to the
        plain Lambertian `hapke.shade_ortho` blend.
    :param along_track_correction: Passed through to `hapke.hapke_shade_ortho`. On by default.
    :param real_hapke_params: Passed through to `hapke.hapke_shade_ortho`. On by default.
    :param ortho_source: Which ortho/texture source to fetch before shading (`ORTHO_SOURCES`):
        `"wac_emp_pds"` (live default) fetches WAC_EMP's own reflectance directly from its PDS4
        archive -- physical reflectance, no embedded display stretch. `"lunaserv_wms"` is the
        deprecated fallback (the original Lunaserv WMS layer), which carries an uncorrected affine
        display stretch -- see docs/data-sources/lunaserv-wms.md.
    :returns: A `DemOrthoResult` for the fetched, shaded ortho (paired with `dem`).
    :raises ValueError: If `ortho_source` isn't one of `ORTHO_SOURCES`, or (for `"wac_emp_pds"`) if the
        camera's footprint needs latitude beyond WAC_EMP's own equirect-tile coverage or straddles a
        tile boundary (`ortho_wac_emp.wac_emp_tile_id_for_bbox`) -- no silent fallback to
        `"lunaserv_wms"` in that case; a caller that wants the fallback has to ask for it explicitly.
    """
    # Taking `dem` (`fetch_dem`'s output) as an input and always reusing its `bbox`/`width`/`height`
    # exactly closes the entanglement `fetch_dem`'s docstring describes for the DEM/ortho pairing
    # specifically: the two can no longer fetch against two different bboxes. The DEM's own
    # filename-collision gap against a different `fetch_dem` call is still open, as noted there.
    #
    # `ortho_source="lunaserv_wms"` is only numerically coherent with `hapke=False`:
    # `hapke.hapke_shade_ortho` assumes its `ortho` input is already reflectance (see its own
    # docstring), which `"lunaserv_wms"`'s raw WMS DN is not (DN under an unknown, non-trivial affine
    # stretch). `hapke.shade_ortho`'s plain-Lambertian fallback is the one that still speaks
    # `"lunaserv_wms"`'s own DN convention unchanged. No code-level guard against this combination --
    # just don't request it.
    #
    # `hapke._terrain_photometric_angles`'s own curvature-aware surface normal is unconditionally
    # applied (not a parameter here at all, see that function's docstring). Each
    # `hapke`/`along_track_correction`/`real_hapke_params` combination writes to its own filename
    # (`ortho_shaded_filename`) rather than a single shared one, so any combination can be fetched for
    # the same camera and compared directly (e.g. `notebooks/hapke_hillshade.ipynb`/
    # `notebooks/along_track_correction.ipynb`/`notebooks/real_hapke_params.ipynb`), and so
    # `trn_dataset.TrnTestEntry.dem_ortho_result`'s resumption check can never mistake one mode's
    # cached file for another's.
    #
    # `bbox`/`width`/`height` (the fetch AOI, already unioned with whatever footprint `fetch_dem` was
    # given -- e.g. `tie_points.crop_footprint_corners_for_camera`'s WAC crop footprint, which isn't
    # always the same size/shape as the synthetic camera's own FOV) all come from `dem`, not
    # recomputed here -- see `fetch_dem`'s own docstring for that computation and its remaining
    # caveats (the ray-traced-estimate-vs-crop margin, the still-open filename-collision gap).
    if ortho_source not in ORTHO_SOURCES:
        raise ValueError(f"ortho_source={ortho_source!r} is not one of {ORTHO_SOURCES!r}")
    config = config or load_config()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    bbox, width, height = dem.bbox, dem.width, dem.height

    center = camera.footprint_lonlat_deg["center"]
    assert center is not None, "camera's nadir footprint center must be a real ground point"
    center_lon, center_lat = center
    # A per-camera local Orthographic CRS (Lunaserv's `IAU2000:30166`, parametrized by this
    # footprint's own center) rather than Lunaserv's native unprojected geographic grid
    # (`IAU2000:30100`) -- the geographic grid's degree-pixels are anisotropic away from the equator
    # (a degree of longitude covers less ground distance than a degree of latitude), and ASP's
    # `mapproject --ref-map` (see `render.run_mapproject`) doesn't preserve that anisotropy: it copies
    # the reference grid's x-resolution onto the y-axis too, silently stretching any `--ref-map`'d
    # output vertically by up to `1/cos(lat)`. A local Orthographic projection has square meter pixels
    # everywhere, so that mismatch can't arise in the first place. `IAU2000:30166` reports the Moon's
    # 1,737,400 m radius (unlike the generic OGC `AUTO:42003` Orthographic code, which is hardcoded to
    # Earth's WGS84 ellipsoid) -- see docs/data-sources/lunaserv-wms.md.
    srs = config.lunaserv_srs_template.format(c_lon=center_lon, c_lat=center_lat)

    if ortho_source == "wac_emp_pds":
        # Live default: WAC_EMP's own reflectance, fetched directly from its PDS4 archive rather than
        # through Lunaserv's WMS render -- the WMS layer's DN carries an uncorrected affine display
        # stretch, not raw reflectance. `fetch_wac_emp_reflectance` raises if this footprint needs a
        # tile beyond the archive's own equirect coverage (see its own docstring) -- no silent
        # fallback to the deprecated Lunaserv path below.
        wac_emp_path, wac_emp_product_id = fetch_wac_emp_reflectance(bbox, center_lon, center_lat, config)
        print(f"WAC_EMP tile: {wac_emp_product_id}")
        ortho_path = config.output_dir / "ortho_wac_emp.tif"
        reproject_wac_emp_reflectance_to_local_grid(
            wac_emp_path, bbox, width, height, center_lon, center_lat, MOON_RADIUS_M, ortho_path
        )
    else:
        ortho_path = cache.fetch_lunaserv_getmap(
            config.lunaserv_ortho_layer,
            bbox,
            width,
            height,
            cache_root=config.cache_root,
            srs=srs,
            base_url=config.lunaserv_base_url,
            fmt="image/tiff",
        )
    ortho_shaded_path = config.output_dir / ortho_shaded_filename(
        hapke, along_track_correction, real_hapke_params, ortho_source
    )
    despeckle_and_shade_ortho(
        ortho_path,
        dem.dem,
        camera,
        ortho_shaded_path,
        config,
        bbox,
        hapke=hapke,
        along_track_correction=along_track_correction,
        real_hapke_params=real_hapke_params,
        ortho_source=ortho_source,
    )

    return DemOrthoResult(
        ortho=ortho_shaded_path,
        dem=dem.dem,
        bbox=bbox,
        width=width,
        height=height,
    )


def fetch_dem_and_ortho(
    camera: Camera,
    config: TrntestConfig | None = None,
    extra_footprint_lonlat_deg: dict | None = None,
    hapke: bool = DEFAULT_HAPKE_SHADING,
    along_track_correction: bool = DEFAULT_ALONG_TRACK_CORRECTION,
    real_hapke_params: bool = DEFAULT_REAL_HAPKE_PARAMS,
    ortho_source: str = DEFAULT_ORTHO_SOURCE,
) -> DemOrthoResult:
    """Compose `fetch_dem` + `fetch_and_shade_ortho`.

    :param camera: Camera whose footprint determines the fetch AOI.
    :param config: Project config; `load_config()` if not given.
    :param extra_footprint_lonlat_deg: Extra corners to union into the AOI before padding, if given.
    :param hapke: Passed through to `fetch_and_shade_ortho`.
    :param along_track_correction: Passed through to `fetch_and_shade_ortho`.
    :param real_hapke_params: Passed through to `fetch_and_shade_ortho`.
    :param ortho_source: Passed through to `fetch_and_shade_ortho`.
    :returns: A `DemOrthoResult` for the fetched DEM/ortho pair.
    """
    # See `fetch_dem`/`fetch_and_shade_ortho`'s own docstrings for what's now individually
    # `product_registry`-decorated, and for the DEM filename-collision gap that split doesn't itself
    # close.
    dem = fetch_dem(camera, config, extra_footprint_lonlat_deg)
    return fetch_and_shade_ortho(
        camera,
        dem,
        config,
        hapke=hapke,
        along_track_correction=along_track_correction,
        real_hapke_params=real_hapke_params,
        ortho_source=ortho_source,
    )
