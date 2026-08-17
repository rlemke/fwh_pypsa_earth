# SPDX-License-Identifier: Apache-2.0
"""fwh_pypsa_earth — PyPSA-Earth's OSM stage as an FFL workflow.

The port of `download_osm_data` -> `clean_osm_data` -> `build_osm_network` from
https://github.com/pypsa-meets-earth/pypsa-earth, which turns raw OpenStreetMap
power infrastructure into a grid topology (buses, lines, converters,
transformers).

**Scope, stated up front.** PyPSA-Earth is ~60 rules in a 2,417-line Snakefile
covering sector coupling, industry demand, renewable profiles and solving. Most
of it is not OSM and needs ERA5 cutouts (gigabytes, CDS credentials) and an LP
solver. This ports the three rules that ARE OSM, and stops before
`base_network`, which needs `pypsa` itself plus GADM/EEZ shapes.

**This is not the retrieve.smk port, and the difference is the point.** There,
73 rules collapsed into one `foreach` because they were 73 near-copies of one
shape and the work list was already a table. Here the three rules are three
distinct algorithms — 2,168 lines of upstream Python — so the port is roughly
one facet per rule and there is no collapse ratio to report. What FFL adds
instead is durability, a per-country fan-out that spreads across a fleet, and
sourcing OSM from a local planet rather than a rate-limited mirror.

So the handlers **call upstream's own functions** (`clean_data`, `built_network`)
rather than reimplementing them: the payload stays theirs, only the orchestration
is ours, which is the same discipline `fwh_gridbuilder` used to make its
comparison honest.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

domain = DomainPackage(
    name="pypsa-earth",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
