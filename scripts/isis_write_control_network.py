"""Writes a real ISIS control network (`.net`) file from a CSV of control points -- the actual
binary-writing step behind `trntest.control_network.write_control_network`.

**Runs under the ISIS conda environment's own Python, not this project's `uv` venv.** `plio`
(DOI-USGS's ISIS control-network read/write library) ships bundled with the `isis` conda package
this project already installs (`docker/Dockerfile`'s `micromamba create ... isis ale`) -- confirmed
live, no separate install needed -- but it isn't, and deliberately isn't, a dependency of this
project's own `pyproject.toml`: pulling `plio` into the main venv would drag in its own real
transitive weight (protobuf, sqlalchemy, h5py, scipy, networkx) version-coupled to a specific ISIS
release, just to write one file. This script is invoked as a subprocess via
`f"{os.environ['ISISROOT']}/bin/python"`, the same "treat ISIS as an external tool called via
subprocess" pattern `isis_wac.py` already uses throughout for `campt`/`cam2map`/etc. -- not
`trntest`-importable, so it's outside `trntest-lint`'s mypy scope (`src/trntest` only) and never
needs `plio` resolvable from this project's own venv.

CSV columns expected (one row per control point -- this project only ever writes single-measure,
`Fixed`-type points, i.e. real ground control from a trusted basemap, not multi-image tie points
subject to their own adjustment): `id`, `pointType`, `referenceIndex`, `aprioriX`, `aprioriY`,
`aprioriZ`, `adjustedX`, `adjustedY`, `adjustedZ` (body-fixed rectangular km -- ISIS's own control
network convention), `serialnumber`, `measureType`, `sample`, `line`. `sample`/`line` are expected
0-based (numpy/array convention) -- `plio`'s own `IsisStore.create_points` adds `(0.5, 0.5)` itself
to reach ISIS's 1-based pixel-center convention (confirmed via direct source inspection, not
assumed); the caller (`write_control_network`) is responsible for converting from
`isis_wac.ground_to_image_pixel`'s already-1-based output before writing the CSV, not this script."""

import argparse

import pandas as pd
from plio.io.io_controlnetwork import to_isis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Input control-point CSV path")
    parser.add_argument("--out", required=True, help="Output ISIS .net file path")
    parser.add_argument("--target", required=True, help="Target body name, e.g. 'Moon'")
    parser.add_argument("--networkid", required=True, help="Control network ID/name")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    to_isis(df, args.out, targetname=args.target, networkid=args.networkid)


if __name__ == "__main__":
    main()
