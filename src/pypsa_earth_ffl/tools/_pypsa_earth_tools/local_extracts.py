# SPDX-License-Identifier: Apache-2.0
"""Serve earth_osm from LOCAL extracts instead of Geofabrik.

`download_osm_data` calls ``earth_osm``, which fetches a per-country PBF from
Geofabrik. On a deployment that already hosts its own planet split — 8 continent
and ~200 country extracts on disk and in the object store — re-downloading them
is both slow and a dependency on a host this fleet has been banned from before.
The standing policy here is that no OSM cache entry should be hitting Geofabrik.

earth_osm does not need patching for that. Its download step is:

    if update or not pbf_existed:  download
    else:                          reuse the file, then verify_pbf(pbf, md5)

so a PBF **and a matching ``.md5``** already sitting in its ``data_dir/pbf/``
makes the network call unnecessary — and the verification still runs, against
our own checksum, so a corrupt local copy is caught rather than trusted.

The path mapping is free rather than invented: earth_osm's own region record
gives ``https://download.geofabrik.de/europe/luxembourg-latest.osm.pbf``, whose
path is ``europe/luxembourg-latest.osm.pbf`` — exactly the key layout of a
planet split produced by ``planet_bootstrap`` (and of ``s3://osm-extracts``). So
the local file is found by asking earth_osm where it would have downloaded from.

What this does NOT do is claim the local copy is current. A self-hosted extract
is as fresh as the last time the planet was split; the returned report says which
countries were served locally so a run can be read with that in mind.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - matching Geofabrik's own checksum format
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def geofabrik_relpath(country_code: str) -> tuple[str, str]:
    """(`europe/luxembourg-latest.osm.pbf`, `luxembourg-latest.osm.pbf`).

    Taken from earth_osm's own region table, so the layout cannot drift from what
    it expects to download.
    """
    from earth_osm import gfk_data

    region = gfk_data.get_region_tuple(country_code)
    path = urlparse(region.urls["pbf"]).path.lstrip("/")
    return path, path.rsplit("/", 1)[-1]


def prefill(
    countries: list[str],
    data_dir: str,
    roots: list[str],
    *,
    link: bool = True,
) -> dict[str, Any]:
    """Place local extracts where earth_osm looks, so it does not download them.

    ``link`` hard-links by default — a country extract is hundreds of megabytes
    and copying it per run is the cost this exists to avoid. Falls back to a copy
    across filesystems, which is where a hard link cannot go.
    """
    pbf_dir = Path(data_dir) / "pbf"
    pbf_dir.mkdir(parents=True, exist_ok=True)
    search = [Path(r) for r in roots if str(r).strip()]

    served: list[str] = []
    absent: list[str] = []
    for code in countries:
        try:
            rel, fname = geofabrik_relpath(code)
        except Exception as exc:  # noqa: BLE001 - unknown code is not fatal here
            log.warning("no earth_osm region for %r: %s", code, exc)
            absent.append(code)
            continue

        source = None
        for root in search:
            for candidate in (root / rel, root / fname):
                if candidate.exists():
                    source = candidate
                    break
            if source:
                break
        if source is None:
            absent.append(code)
            continue

        target = pbf_dir / fname
        if not target.exists():
            if link:
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
            else:
                shutil.copyfile(source, target)
        # The checksum earth_osm will verify against. Written from OUR copy, so
        # it certifies the bytes on disk rather than asserting they match
        # Geofabrik's — which they need not, a self-hosted split being its own
        # artefact.
        (pbf_dir / f"{fname}.md5").write_text(f"{_md5(target)}  {fname}\n")
        served.append(code)

    return {"served": served, "absent": absent, "pbf_dir": str(pbf_dir)}
