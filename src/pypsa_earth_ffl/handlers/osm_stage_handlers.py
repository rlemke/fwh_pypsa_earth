# SPDX-License-Identifier: Apache-2.0
"""PyPSA-Earth's OSM stage, as three Facetwork event facets.

Ports ``download_osm_data`` -> ``clean_osm_data`` -> ``build_osm_network``.

Each handler calls **upstream's own function** with the paths Snakemake would
have resolved — ``clean_osm_data.clean_data`` and
``build_osm_network.built_network`` are already path-in/path-out, so nothing is
reimplemented. That is the same discipline ``fwh_gridbuilder`` used: keep the
payload theirs, change only the orchestrator, and the comparison stays about
orchestration rather than about two different models that happen to share a name.

Config defaults are read from the checkout's own ``config.default.yaml`` rather
than restated here, so a knob upstream changes does not silently diverge.

Blocking, heavy, and long (a country's OSM extract plus a graph build), so all
three register with ``timeout_ms=0`` and rely on the runner's global execution
timeout — same as the other OSM handlers.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from facetwork.runtime.errors import PermanentError

from ..tools._pypsa_earth_tools import upstream

log = logging.getLogger(__name__)

NAMESPACE = "pypsa.earth"

#: The four feature classes upstream's download rule asks earth_osm for
#: (scripts/download_osm_data.py). Kept as their spelling, singular, in their
#: order — this list IS the rule's payload.
FEATURES = ["substation", "line", "cable", "generator"]
NAMES = ["generator", "cable", "line", "substation"]
OUT_FORMATS = ["csv", "geojson"]


def _log(params: dict[str, Any]):
    sl = params.get("_step_log")
    return (lambda m, level="info": sl(m, level=level)) if sl else (lambda m, level="info": None)


def _config() -> dict:
    """Upstream's config.default.yaml, so defaults are theirs and not ours."""
    import yaml

    root = upstream.require_repo()
    with open(root / "config.default.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _guard(fn):
    """Turn a missing checkout into a PermanentError.

    Not having PyPSA-Earth on this runner is a deployment fact, not a transient
    fault, so the task dead-letters rather than retrying five times on a host
    that will never have it.
    """

    def wrapper(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn(params)
        except upstream.UpstreamMissing as exc:
            raise PermanentError(str(exc)) from exc

    return wrapper


# ---------------------------------------------------------------------------
# 0/3  build_shapes — the prerequisite, scoped to what the OSM stage needs
# ---------------------------------------------------------------------------


@_guard
def handle_build_shapes(params: dict[str, Any]) -> dict[str, Any]:
    """Upstream's ``build_shapes``, restricted to its three cheap outputs.

    Their rule produces six things from four independent functions. The OSM
    stage consumes exactly three — ``country_shapes``, ``offshore_shapes`` and
    ``extended_country_shape`` — and NOT ``gadm_shapes``, which is the expensive
    one: ``gadm()`` downloads WorldPop population rasters per country. So it is
    skipped unless ``include_gadm`` asks for it, and that is a scope decision
    rather than an omission.

    **The EEZ file cannot be automated, and not because of this port.** Upstream's
    own ``eez()`` reads ``data/eez/eez_v11.gpkg`` and, when it is absent, tells
    the user to *"download it from marineregions.org and copy it in"* — a
    form-gated manual download. Given one, offshore shapes are produced normally.
    Without one this returns EMPTY offshore shapes, which is exactly right for a
    landlocked country and **wrong for a coastal one**, so it must be asked for
    explicitly via ``allow_no_eez``.

    ``country_cover``'s ``eez_shapes`` argument is optional in upstream's own
    signature, so the no-EEZ path is theirs rather than something invented here —
    but the extended shape then carries no offshore buffer, and `clean_data`
    filters lines against it.
    """
    import geopandas as gpd

    mod = upstream.load("build_shapes")
    cfg = _config()
    opts = cfg["build_shape_options"]
    crs = cfg["crs"]
    say = _log(params)

    countries_list = params.get("countries") or []
    if isinstance(countries_list, str):
        countries_list = [countries_list]
    if not countries_list:
        raise PermanentError("countries is required")
    out_dir = Path(params["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    eez_gpkg = (params.get("eez_gpkg") or "").strip()
    if eez_gpkg and not Path(eez_gpkg).exists():
        raise PermanentError(f"eez_gpkg does not exist: {eez_gpkg}")
    if not eez_gpkg and not params.get("allow_no_eez"):
        raise PermanentError(
            "no eez_gpkg. Upstream's eez() needs data/eez/eez_v11.gpkg, which even "
            "upstream does not download for you (marineregions.org, form-gated). "
            "Pass it, or set allow_no_eez=true — which yields EMPTY offshore "
            "shapes: correct for a landlocked country, wrong for a coastal one."
        )

    say(f"build_shapes for {countries_list} (GADM download; gadm_shapes skipped)")
    country_shapes = mod.countries(
        countries_list,
        crs["geo_crs"],
        opts["contended_flag"],
        opts["update_file"],
        opts["out_logging"],
        tolerance=opts["simplify_tolerance"],
    )
    p_country = out_dir / "country_shapes.geojson"
    country_shapes.to_file(p_country)

    p_offshore = out_dir / "offshore_shapes.geojson"
    if eez_gpkg:
        offshore = mod.eez(
            countries_list,
            crs["geo_crs"],
            country_shapes,
            eez_gpkg,
            out_logging=opts["out_logging"],
            tolerance=opts["simplify_tolerance"],
            minarea=opts["minarea"],
            simplify_gadm=opts["simplify_gadm"],
        )
        offshore.reset_index().to_file(p_offshore)
        offshore_geom = offshore.geometry
        offshore_n = len(offshore)
    else:
        say("no EEZ: offshore shapes are EMPTY (landlocked assumption)", level="warning")
        # fiona refuses to write an empty layer, so the empty case is written as
        # literal GeoJSON rather than left absent — the next rule declares it as
        # an input and an absent file is a broken DAG, not an empty result.
        p_offshore.write_text('{"type": "FeatureCollection", "features": []}')
        offshore_geom = None
        offshore_n = 0

    extended = gpd.GeoDataFrame(
        geometry=[mod.country_cover(country_shapes, offshore_geom)],
        crs=country_shapes.crs,
    )
    p_extended = out_dir / "extended_country_shape.geojson"
    extended.reset_index().to_file(p_extended)

    gadm_path = ""
    if params.get("include_gadm"):
        say("gadm(): downloading WorldPop rasters — this is the expensive path")
        gadm_shapes = mod.gadm(
            opts["worldpop_method"], opts["gdp_method"], countries_list,
            crs["geo_crs"], opts["contended_flag"], int(params.get("mem_mb", 3000)),
            opts["gadm_layer_id"], opts["update_file"], opts["out_logging"],
            opts["year"], nprocesses=opts["nprocesses"],
            simplify_gadm=opts["simplify_gadm"], tolerance=opts["simplify_tolerance"],
            minarea=opts["minarea"],
        )
        gadm_path = str(out_dir / "gadm_shapes.geojson")
        mod.save_to_geojson(gadm_shapes, gadm_path)

    say(f"country_shapes={len(country_shapes)} offshore={offshore_n}")
    return {
        "out_dir": str(out_dir),
        "country_shapes": str(p_country),
        "offshore_shapes": str(p_offshore),
        "extended_country_shape": str(p_extended),
        "gadm_shapes": gadm_path,
        "country_count": len(country_shapes),
        "offshore_count": offshore_n,
        "upstream_commit": upstream.version(),
    }


# ---------------------------------------------------------------------------
# 1/3  download_osm_data
# ---------------------------------------------------------------------------


@_guard
def handle_download_osm_data(params: dict[str, Any]) -> dict[str, Any]:
    """Raw OSM power features for a list of countries.

    Mirrors scripts/download_osm_data.py: the same ``eo.save_osm_data`` call with
    the same feature list, then the same rename-and-fill-empties pass. The empty
    files matter — earth_osm omits a file it has no rows for, and every
    downstream rule declares it as an input, so their absence is a broken DAG
    rather than an empty result.

    ``mp`` defaults to FALSE here, unlike upstream's True: earth_osm's
    multiprocessing forks, and forking a runner that holds logging and Mongo
    locks in other threads deadlocks the children (the trap fwh_gridbuilder hit —
    a task at 0.6% CPU with 19 live processes and no output).
    """
    from earth_osm import eo

    dl = upstream.load("download_osm_data")
    countries = params.get("countries") or []
    if isinstance(countries, str):
        # A `foreach` binds one element, so the fan-out passes a bare code.
        countries = [countries] if countries else []
    if not countries:
        raise PermanentError("countries is required — nothing to download")
    out_dir = Path(params["out_dir"])
    data_dir = Path(params.get("data_dir") or (out_dir.parent / "osm_data"))
    say = _log(params)

    country_list = dl.country_list_to_geofk(countries)
    say(f"earth_osm: {len(country_list)} region(s) x {len(FEATURES)} feature(s)")
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    eo.save_osm_data(
        primary_name="power",
        region_list=country_list,
        feature_list=FEATURES,
        update=False,
        mp=bool(params.get("mp", False)),
        data_dir=str(data_dir),
        out_dir=str(out_dir),
        out_format=OUT_FORMATS,
        out_aggregate=True,
        progress_bar=False,
    )

    produced: dict[str, str] = {}
    empties: list[str] = []
    out_path = out_dir / "out"
    for name in NAMES:
        for fmt in OUT_FORMATS:
            target = out_dir / f"all_raw_{name}s.{fmt}"
            found = sorted(Path(out_path).glob(f"*{name}.{fmt}")) if out_path.exists() else []
            if not found:
                target.write_text("")
                empties.append(target.name)
            else:
                shutil.move(str(found[0]), target)
            produced[f"{name}s_{fmt}"] = str(target)

    say(f"{len(produced)} file(s); {len(empties)} empty" + (f" ({', '.join(empties)})" if empties else ""))
    return {
        "out_dir": str(out_dir),
        "cables": produced["cables_geojson"],
        "generators": produced["generators_geojson"],
        "generators_csv": produced["generators_csv"],
        "lines": produced["lines_geojson"],
        "substations": produced["substations_geojson"],
        "empty_files": empties,
        "upstream_commit": upstream.version(),
    }


# ---------------------------------------------------------------------------
# 1b/3  merge — what a per-country fan-out costs
# ---------------------------------------------------------------------------


@_guard
def handle_merge_raw_osm(params: dict[str, Any]) -> dict[str, Any]:
    """Concatenate per-country raw files into the one set `clean_osm_data` reads.

    Upstream never needs this: it hands earth_osm the whole country list at once
    with ``out_aggregate=True`` and gets one aggregated file back. Fanning the
    download out per country — the point of running this on a fleet rather than
    one machine's cores — means the aggregation has to happen afterwards, so the
    fan-out is not free and this facet is its cost, stated rather than hidden.

    The concatenation is honest because the features carry their own ``Region``,
    so a merged file is distinguishable by origin rather than anonymised. Empty
    inputs stay empty outputs: `clean_osm_data` declares all four as inputs and
    checks their SIZE (``os.path.getsize(...) > 0``), so a missing file breaks it
    and a zero-byte one is the documented "nothing here".
    """
    import json

    raw_dirs = [Path(d) for d in (params.get("raw_dirs") or [])]
    if not raw_dirs:
        raise PermanentError("raw_dirs is required — nothing to merge")
    out_dir = Path(params["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    say = _log(params)

    counts: dict[str, int] = {}
    for name in NAMES:
        merged: list[dict] = []
        header: dict[str, Any] | None = None
        for d in raw_dirs:
            src = d / f"all_raw_{name}s.geojson"
            if not src.exists() or src.stat().st_size == 0:
                continue
            try:
                doc = json.loads(src.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise PermanentError(f"{src} is not valid GeoJSON: {exc}") from exc
            if header is None:
                header = {k: v for k, v in doc.items() if k != "features"}
            merged.extend(doc.get("features") or [])
        target = out_dir / f"all_raw_{name}s.geojson"
        if merged:
            doc = dict(header or {"type": "FeatureCollection"})
            doc["features"] = merged
            target.write_text(json.dumps(doc), encoding="utf-8")
        else:
            target.write_text("")
        counts[name] = len(merged)

    say("merged " + ", ".join(f"{k}={v}" for k, v in counts.items()) + f" from {len(raw_dirs)} dir(s)")
    return {"out_dir": str(out_dir), "counts": counts, "source_dirs": len(raw_dirs)}


# ---------------------------------------------------------------------------
# 2/3  clean_osm_data
# ---------------------------------------------------------------------------


@_guard
def handle_clean_osm_data(params: dict[str, Any]) -> dict[str, Any]:
    """Upstream's ``clean_data``, called with the paths Snakemake would pass.

    ``names_by_shapes`` follows upstream's default (TRUE) and needs
    ``country_shapes`` + ``offshore_shapes`` from their ``build_shapes`` rule.
    Setting it false does not merely reduce attribution — it leaves every bus
    with ``country=NULL``, so it warns rather than passing silently.
    """
    import geopandas as gpd

    mod = upstream.load("clean_osm_data")
    cfg = _config()
    say = _log(params)

    raw = Path(params["raw_dir"])
    out_dir = Path(params["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    input_files = {
        "cables": str(raw / "all_raw_cables.geojson"),
        "generators": str(raw / "all_raw_generators.geojson"),
        "lines": str(raw / "all_raw_lines.geojson"),
        "substations": str(raw / "all_raw_substations.geojson"),
    }
    missing = [k for k, p in input_files.items() if not Path(p).exists()]
    if missing:
        raise PermanentError(
            f"raw OSM inputs missing in {raw}: {', '.join(missing)} — run DownloadOsmData first"
        )
    output_files = {
        "generators": str(out_dir / "all_clean_generators.geojson"),
        "generators_csv": str(out_dir / "all_clean_generators.csv"),
        "lines": str(out_dir / "all_clean_lines.geojson"),
        "substations": str(out_dir / "all_clean_substations.geojson"),
    }

    data_options = dict(cfg["osm"]["clean_osm_data"])
    for key in ("tag_substation", "threshold_voltage", "add_line_endings", "generator_name_method"):
        if params.get(key) is not None:
            data_options[key] = params[key]
    # Upstream's config.default.yaml sets names_by_shapes: true. Defaulting it
    # to false here (to dodge build_shapes) did not reduce the result, it
    # CORRUPTED it: without shape attribution every bus gets country=NULL, which
    # then makes add_buses_to_empty_countries treat every requested country as
    # data-less. Verified: a Luxembourg run produced 36 buses all countried NULL,
    # and a two-country run dead-lettered on the resulting length mismatch.
    # So the default follows upstream, and turning it off is a loud choice.
    names_by_shapes = params.get("names_by_shapes")
    names_by_shapes = True if names_by_shapes is None else bool(names_by_shapes)
    if not names_by_shapes:
        say(
            "names_by_shapes=false — every bus will carry country=NULL, which is "
            "NOT what upstream produces and breaks per-country logic downstream",
            level="warning",
        )
    data_options["names_by_shapes"] = names_by_shapes

    # REQUIRED, and not a formality: clean_data filters lines by
    # `geometry.boundary.within(extended_country_shape)` (clean_osm_data.py:992),
    # so this polygon decides which lines survive. Upstream produces it in
    # `build_shapes` from GADM/EEZ — outside this port's scope — so the caller
    # must supply it, and substituting a bounding box would silently change the
    # result rather than approximate it.
    if not params.get("extended_country_shape"):
        raise PermanentError(
            "extended_country_shape is required: clean_data filters lines against it, "
            "so omitting it would change which lines survive. Upstream's build_shapes "
            "rule produces it (GADM/EEZ); supply that file, or any authoritative "
            "polygon for the country set — and record which, because it is not "
            "interchangeable."
        )
    extended_country_shape = gpd.read_file(params["extended_country_shape"])["geometry"].iloc[0]

    ext_country_shapes = None
    if names_by_shapes:
        for key in ("country_shapes", "offshore_shapes"):
            if not params.get(key):
                raise PermanentError(
                    f"names_by_shapes=true needs {key} — it comes from upstream's "
                    "build_shapes rule, which is outside this port's scope"
                )
        input_files["country_shapes"] = params["country_shapes"]
        input_files["offshore_shapes"] = params["offshore_shapes"]
        country_shapes = gpd.read_file(params["country_shapes"]).set_index("name")["geometry"]
        offshore = gpd.read_file(params["offshore_shapes"])
        offshore = offshore.reindex(columns=mod.REGION_COLS).set_index("name")["geometry"]
        ext_country_shapes = mod.create_extended_country_shapes(country_shapes, offshore)

    crs = cfg["crs"]
    # ⚠️ Upstream's `clean_data` TAKES input_files as a parameter, but the
    # `load_network_data` helper it calls reads a module-level `input_files`
    # global that only their `__main__` sets (clean_osm_data.py:1124). Under
    # Snakemake the two are the same object so the coupling is invisible; called
    # as a library it is a NameError. Setting the global to the same dict is what
    # their __main__ does — not a monkeypatch of behaviour, just supplying the
    # binding the script expects. Reported upstream-side in the README.
    mod.input_files = input_files
    say(f"clean_data (upstream {upstream.version()}), names_by_shapes={names_by_shapes}")
    mod.clean_data(
        input_files,
        output_files,
        extended_country_shape,
        crs["geo_crs"],
        crs["distance_crs"],
        data_options,
        ext_country_shapes=ext_country_shapes,
        names_by_shapes=names_by_shapes,
        tag_substation=data_options["tag_substation"],
        threshold_voltage=data_options["threshold_voltage"],
        add_line_endings=data_options["add_line_endings"],
        generator_name_method=data_options.get("generator_name_method", "OSM"),
    )

    counts = {k: _feature_count(v) for k, v in output_files.items() if v.endswith(".geojson")}
    say("cleaned: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return {
        "out_dir": str(out_dir),
        "generators": output_files["generators"],
        "lines": output_files["lines"],
        "substations": output_files["substations"],
        "counts": counts,
        "upstream_commit": upstream.version(),
    }


# ---------------------------------------------------------------------------
# 3/3  build_osm_network
# ---------------------------------------------------------------------------


@_guard
def handle_build_osm_network(params: dict[str, Any]) -> dict[str, Any]:
    """Upstream's ``built_network`` — the grid topology itself."""
    mod = upstream.load("build_osm_network")
    cfg = _config()
    say = _log(params)

    clean = Path(params["clean_dir"])
    out_dir = Path(params["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = {
        "generators": str(clean / "all_clean_generators.geojson"),
        "lines": str(clean / "all_clean_lines.geojson"),
        "substations": str(clean / "all_clean_substations.geojson"),
    }
    # Required for the same reason as the clean stage's shape: upstream calls
    # add_buses_to_empty_countries(countries, inputs.country_shapes, buses), which
    # invents a bus for any requested country OSM has nothing for. Without it the
    # country set silently shrinks to whatever OSM happened to cover.
    if not params.get("country_shapes"):
        raise PermanentError(
            "country_shapes is required: built_network uses it to add buses for "
            "countries with no OSM data (build_osm_network.py:794). Upstream's "
            "build_shapes produces it."
        )
    inputs["country_shapes"] = params["country_shapes"]
    missing = [k for k, p in inputs.items() if not Path(p).exists()]
    if missing:
        raise PermanentError(
            f"clean inputs missing in {clean}: {', '.join(missing)} — run CleanOsmData first"
        )

    outputs = {
        "lines": str(out_dir / "all_lines_build_network.csv"),
        "converters": str(out_dir / "all_converters_build_network.csv"),
        "transformers": str(out_dir / "all_transformers_build_network.csv"),
        "substations": str(out_dir / "all_buses_build_network.csv"),
        "lines_geo": str(out_dir / "all_lines_build_network.geojson"),
        "converters_geo": str(out_dir / "all_converters_build_network.geojson"),
        "transformers_geo": str(out_dir / "all_transformers_build_network.geojson"),
        "substations_geo": str(out_dir / "all_buses_build_network.geojson"),
    }

    crs = cfg["crs"]
    countries = params.get("countries") or cfg.get("countries") or []
    # Same defect as the clean stage's `input_files`: built_network TAKES geo_crs
    # and distance_crs as parameters, but `add_buses_to_empty_countries` reads a
    # module-level `geo_crs` global that only their __main__ assigns
    # (build_osm_network.py:852). Mirror what __main__ sets, so the script gets
    # the bindings it was written against.
    mod.geo_crs = crs["geo_crs"]
    mod.distance_crs = crs["distance_crs"]
    say(f"built_network (upstream {upstream.version()}) for {countries}")
    mod.built_network(
        _SnakemakePaths(inputs),
        _SnakemakePaths(outputs),
        cfg.get("osm", {}).get("build_osm_network", {}),
        countries,
        crs["geo_crs"],
        crs["distance_crs"],
        force_ac=bool(params.get("force_ac", False)),
    )

    counts = {k: _row_count(v) for k, v in outputs.items() if v.endswith(".csv")}
    say("network: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return {
        "out_dir": str(out_dir),
        "buses": outputs["substations"],
        "lines": outputs["lines"],
        "converters": outputs["converters"],
        "transformers": outputs["transformers"],
        "counts": counts,
        "upstream_commit": upstream.version(),
    }


class _SnakemakePaths(dict):
    """A dict that also answers attribute access.

    Upstream's cores are *almost* decoupled from Snakemake: `clean_data` and
    `built_network` take plain ``inputs``/``outputs`` and index them like dicts —
    except ``build_osm_network.py:794``, which reaches for
    ``inputs.country_shapes``. Snakemake passes a namedtuple-like object where
    both work; a dict only satisfies half of it. Rather than fork their code,
    hand them something that satisfies both.
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(
                f"{name} — this port did not supply it; upstream's build_osm_network "
                "expects it as an attribute"
            ) from exc


def _feature_count(path: str) -> int:
    try:
        import json

        with open(path, encoding="utf-8") as fh:
            return len((json.load(fh) or {}).get("features") or [])
    except Exception:  # noqa: BLE001 - a count is diagnostics, never fatal
        return -1


def _row_count(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return max(sum(1 for _ in fh) - 1, 0)
    except OSError:
        return -1


_DISPATCH = {
    f"{NAMESPACE}.BuildShapes": handle_build_shapes,
    f"{NAMESPACE}.DownloadOsmData": handle_download_osm_data,
    f"{NAMESPACE}.MergeRawOsm": handle_merge_raw_osm,
    f"{NAMESPACE}.CleanOsmData": handle_clean_osm_data,
    f"{NAMESPACE}.BuildOsmNetwork": handle_build_osm_network,
}


def handle(payload: dict) -> dict:
    facet = payload["_facet_name"]
    fn = _DISPATCH.get(facet)
    if fn is None:
        raise ValueError(f"Unknown facet: {facet}")
    return fn(payload)


def register_handlers(runner) -> None:
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_pypsa_earth_handlers(poller) -> None:
    for facet_name, fn in _DISPATCH.items():
        poller.register(facet_name, fn)
