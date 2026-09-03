# SPDX-License-Identifier: Apache-2.0
"""Country shapes from a LOCAL OSM extract, when GADM cannot be reached.

Upstream's ``build_shapes`` gets country geometry from GADM
(``geodata.ucdavis.edu``). That host is unreachable from some networks — on this
fleet it answers ICMP but times out on port 443, exactly as Geofabrik does — and
then the whole OSM stage is blocked on a shape file, not on any OSM data.

OSM already contains national boundaries: relations tagged
``boundary=administrative`` + ``admin_level=2``, carrying ``ISO3166-1`` codes. A
planet or continent extract on local disk therefore answers the same question
without the network.

**This is a substitution of provenance, not an equivalent.** GADM and OSM
disagree about disputed territory, coastline generalisation and enclaves, and
upstream's ``countries()`` additionally applies a ``contended_flag`` policy and a
simplification tolerance that this does not reproduce. So the result is labelled
at the point of use and never presented as a GADM shape. Use it to get a run,
and say which source produced it when reporting numbers.

The osmium pipeline is the same two-pass shape the rest of this project uses:
filter the relations, then export assembled polygons. ``osmium export`` silently
drops boundary relations it cannot assemble into closed rings — a long tail that
bites large island-heavy countries — so the caller is told which of the requested
countries were actually produced rather than discovering a gap downstream.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Tags that mark a national boundary relation in OSM.
_FILTER = ["r/boundary=administrative", "r/admin_level=2"]

#: Where the ISO-2 code lives, in order of preference. OSM is not consistent.
_ISO_KEYS = ("ISO3166-1:alpha2", "ISO3166-1", "iso3166-1:alpha2", "iso3166-1")


class LocalShapesError(RuntimeError):
    """Raised when local country shapes cannot be produced."""


def _iso2(props: dict[str, Any]) -> str:
    for key in _ISO_KEYS:
        value = (props.get(key) or "").strip().upper()
        # ISO3166-1 sometimes carries the alpha-3 or a numeric code.
        if len(value) == 2:
            return value
    return ""


def country_shapes_from_osm(
    pbf_path: str,
    countries: list[str],
    out_path: str,
    *,
    osmium_bin: str = "osmium",
) -> dict[str, Any]:
    """Write a `country_shapes.geojson` in upstream's schema from a local PBF.

    Upstream's consumers index this on a ``name`` column holding **ISO-2 codes**
    (``build_osm_network`` matches ``country_shapes.index.isin(no_data_countries)``
    against exactly those), so ``name`` is the ISO code and never the country's
    English name — a file saying ``Luxembourg`` reads as a country with no data.
    """
    src = Path(pbf_path)
    if not src.exists():
        raise LocalShapesError(f"no such PBF: {src}")
    # ⚠️ Params before TOOLS. "no countries requested" is true whether or not
    # osmium is installed, and it is the actionable message; checking the binary
    # first meant a caller with an empty country list was told to install a tool
    # that would not have helped. Surfaced by CI, which has no osmium.
    wanted = {c.strip().upper() for c in countries if c.strip()}
    if not wanted:
        raise LocalShapesError("no countries requested")
    if not shutil.which(osmium_bin):
        raise LocalShapesError(f"osmium not found ({osmium_bin})")

    staging = Path(tempfile.mkdtemp(prefix="pe-shapes-"))
    try:
        filtered = staging / "boundaries.osm.pbf"
        subprocess.run(
            [osmium_bin, "tags-filter", "--overwrite", "-o", str(filtered), str(src), *_FILTER],
            check=True, capture_output=True, text=True,
        )
        exported = staging / "boundaries.geojsonseq"
        subprocess.run(
            [osmium_bin, "export", "-f", "geojsonseq", "--geometry-types=polygon",
             "-o", str(exported), "--overwrite", str(filtered)],
            check=True, capture_output=True, text=True,
        )

        found: dict[str, dict] = {}
        with open(exported, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip("\x1e \t\r\n")
                if not line:
                    continue
                feature = json.loads(line)
                props = feature.get("properties") or {}
                code = _iso2(props)
                if code in wanted and code not in found:
                    found[code] = {
                        "type": "Feature",
                        "geometry": feature.get("geometry"),
                        "properties": {"name": code, "source": "osm-admin-level-2"},
                    }
    except subprocess.CalledProcessError as exc:
        raise LocalShapesError(f"osmium failed: {(exc.stderr or '').strip() or exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    missing = sorted(wanted - set(found))
    if not found:
        raise LocalShapesError(
            f"no admin_level=2 boundary with an ISO-2 code for {sorted(wanted)} in {src}. "
            "Either the extract does not cover them, or osmium could not assemble the "
            "relation — it silently drops rings it cannot close."
        )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"type": "FeatureCollection",
                    "features": [found[c] for c in sorted(found)]}),
        encoding="utf-8",
    )
    return {"path": str(out), "countries": sorted(found), "missing": missing}
