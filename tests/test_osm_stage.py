# SPDX-License-Identifier: Apache-2.0
"""Tests for the PyPSA-Earth OSM-stage port.

The refusals and the Snakemake-compatibility shims are pure and always run. The
tests that need upstream's checkout skip themselves without it, so a host that
has not cloned PyPSA-Earth still gets a green suite.

What is pinned here is mostly the *coupling* — the three places upstream's
"callable" cores turn out not to be callable. Those are the things that will
break silently on an upstream bump, and a broken one produces a wrong grid
rather than an error, so they get assertions rather than comments.
"""

from __future__ import annotations

import pytest

from pypsa_earth_ffl.handlers import osm_stage_handlers as h
from pypsa_earth_ffl.tools._pypsa_earth_tools import upstream

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HAS_UPSTREAM = (upstream.repo_dir() / "scripts").is_dir()
needs_upstream = pytest.mark.skipif(not HAS_UPSTREAM, reason="no PyPSA-Earth checkout")


# --- the Snakemake-object shim ---------------------------------------------


def test_paths_object_answers_both_access_styles():
    """`built_network` indexes `inputs` like a dict everywhere except
    build_osm_network.py:794, where it reaches for `inputs.country_shapes`.
    Snakemake's object satisfies both; a plain dict satisfies half."""
    p = h._SnakemakePaths({"country_shapes": "/x.geojson", "lines": "/l.geojson"})
    assert p["country_shapes"] == "/x.geojson"
    assert p.country_shapes == "/x.geojson"
    assert p.lines == "/l.geojson"


def test_paths_object_names_what_was_missing():
    p = h._SnakemakePaths({"lines": "/l.geojson"})
    with pytest.raises(AttributeError, match="country_shapes"):
        _ = p.country_shapes


# --- refusals: what must not burn the retry budget --------------------------


def test_no_countries_is_permanent():
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="countries is required"):
        h.handle({"_facet_name": "pypsa.earth.DownloadOsmData", "countries": [], "out_dir": "/tmp/x"})


@needs_upstream
def test_missing_raw_inputs_point_at_the_previous_stage(tmp_path):
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="run DownloadOsmData first"):
        h.handle({
            "_facet_name": "pypsa.earth.CleanOsmData",
            "raw_dir": str(tmp_path), "out_dir": str(tmp_path / "out"),
            "extended_country_shape": str(tmp_path / "shape.geojson"),
        })


@needs_upstream
def test_the_line_filtering_shape_is_required(tmp_path):
    """clean_data filters lines by `within(extended_country_shape)`, so omitting
    it would not simplify the run — it would change which lines survive."""
    from facetwork.runtime.errors import PermanentError

    raw = tmp_path / "raw"
    raw.mkdir()
    for n in ("cables", "generators", "lines", "substations"):
        (raw / f"all_raw_{n}.geojson").write_text("")
    with pytest.raises(PermanentError, match="extended_country_shape is required"):
        h.handle({
            "_facet_name": "pypsa.earth.CleanOsmData",
            "raw_dir": str(raw), "out_dir": str(tmp_path / "out"),
        })


@needs_upstream
def test_the_empty_country_shape_is_required(tmp_path):
    from facetwork.runtime.errors import PermanentError

    clean = tmp_path / "clean"
    clean.mkdir()
    for n in ("generators", "lines", "substations"):
        (clean / f"all_clean_{n}.geojson").write_text("")
    with pytest.raises(PermanentError, match="country_shapes is required"):
        h.handle({
            "_facet_name": "pypsa.earth.BuildOsmNetwork",
            "clean_dir": str(clean), "out_dir": str(tmp_path / "out"),
        })


def test_unknown_facet_is_rejected():
    with pytest.raises(ValueError, match="Unknown facet"):
        h.handle({"_facet_name": "pypsa.earth.Nope"})


# --- upstream bridge --------------------------------------------------------


def test_a_missing_checkout_is_permanent_not_retryable(monkeypatch, tmp_path):
    """Not having PyPSA-Earth is a deployment fact, so the task dead-letters
    rather than retrying five times on a host that will never have it."""
    from facetwork.runtime.errors import PermanentError

    monkeypatch.setenv("FW_PYPSA_EARTH_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(PermanentError, match="checkout not found"):
        h.handle({
            "_facet_name": "pypsa.earth.CleanOsmData",
            "raw_dir": str(tmp_path), "out_dir": str(tmp_path),
            "extended_country_shape": str(tmp_path / "s.geojson"),
        })


@needs_upstream
def test_upstream_cores_are_still_where_we_call_them():
    """An upstream bump that renames or re-signatures these is the failure mode
    this port cares about most: the payload is theirs, so a silent change there
    is a silent change to the model."""
    assert hasattr(upstream.load("clean_osm_data"), "clean_data")
    assert hasattr(upstream.load("build_osm_network"), "built_network")
    assert hasattr(upstream.load("download_osm_data"), "country_list_to_geofk")


@needs_upstream
def test_the_module_globals_upstream_expects_still_exist():
    """The three coupling points (README). If upstream ever makes these real
    parameters, these assertions fail and the shims can go."""
    import inspect

    clean = upstream.load("clean_osm_data")
    src = inspect.getsource(clean.load_network_data)
    assert "input_files[" in src, "load_network_data no longer reads the global"
    assert "input_files" not in inspect.signature(clean.load_network_data).parameters

    net = upstream.load("build_osm_network")
    src2 = inspect.getsource(net.add_buses_to_empty_countries)
    assert "geo_crs" in src2
    assert "geo_crs" not in inspect.signature(net.add_buses_to_empty_countries).parameters


@needs_upstream
def test_version_identifies_the_commit():
    v = upstream.version()
    assert v and v != "unknown" and len(v) == 12


# --- build_shapes -----------------------------------------------------------


def test_shapes_refuse_to_guess_about_the_sea(tmp_path):
    """No EEZ file means empty offshore shapes, which is correct for a landlocked
    country and wrong for a coastal one. That is a judgement about the caller's
    countries, so it is theirs to make explicitly."""
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="allow_no_eez"):
        h.handle({
            "_facet_name": "pypsa.earth.BuildShapes",
            "countries": ["LU"], "out_dir": str(tmp_path),
        })


def test_shapes_reject_a_missing_eez_path(tmp_path):
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="eez_gpkg does not exist"):
        h.handle({
            "_facet_name": "pypsa.earth.BuildShapes",
            "countries": ["LU"], "out_dir": str(tmp_path),
            "eez_gpkg": str(tmp_path / "nope.gpkg"),
        })


def test_shapes_need_countries(tmp_path):
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="countries is required"):
        h.handle({
            "_facet_name": "pypsa.earth.BuildShapes",
            "countries": [], "out_dir": str(tmp_path), "allow_no_eez": True,
        })


@needs_upstream
def test_the_shape_functions_are_still_where_we_call_them():
    """`gadm_shapes` is deliberately not produced by default — gadm() downloads
    WorldPop rasters and nothing in the OSM stage reads it."""
    mod = upstream.load("build_shapes")
    for fn in ("countries", "eez", "country_cover", "gadm", "save_to_geojson"):
        assert hasattr(mod, fn), fn


@needs_upstream
def test_country_cover_still_accepts_no_eez():
    """The no-EEZ path is upstream's own: eez_shapes defaults to None in their
    signature. If that ever becomes required, allow_no_eez is a lie."""
    import inspect

    sig = inspect.signature(upstream.load("build_shapes").country_cover)
    assert sig.parameters["eez_shapes"].default is None


# --- the local-planet workaround for an unreachable GADM --------------------


def test_shape_source_is_validated(tmp_path):
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="shape_source must be"):
        h.handle({"_facet_name": "pypsa.earth.BuildShapes", "countries": ["LU"],
                  "out_dir": str(tmp_path), "allow_no_eez": True, "shape_source": "naturalearth"})


def test_osm_shape_source_needs_a_local_pbf(tmp_path):
    from facetwork.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="needs local_pbf"):
        h.handle({"_facet_name": "pypsa.earth.BuildShapes", "countries": ["LU"],
                  "out_dir": str(tmp_path), "allow_no_eez": True, "shape_source": "osm"})


def test_local_shapes_iso2_extraction():
    """OSM is inconsistent about where the ISO-2 code lives, and a 3-letter or
    numeric value in ISO3166-1 must not be mistaken for one."""
    from pypsa_earth_ffl.tools._pypsa_earth_tools import local_shapes as ls

    assert ls._iso2({"ISO3166-1:alpha2": "lu"}) == "LU"
    assert ls._iso2({"ISO3166-1": "MT"}) == "MT"
    assert ls._iso2({"iso3166-1": "de"}) == "DE"
    assert ls._iso2({"ISO3166-1": "LUX"}) == "", "alpha-3 is not an ISO-2 code"
    assert ls._iso2({"ISO3166-1": "442"}) == "", "numeric is not an ISO-2 code"
    assert ls._iso2({"name": "Luxembourg"}) == ""


def test_local_shapes_refuse_a_missing_pbf(tmp_path):
    from pypsa_earth_ffl.tools._pypsa_earth_tools.local_shapes import (
        LocalShapesError, country_shapes_from_osm,
    )

    with pytest.raises(LocalShapesError, match="no such PBF"):
        country_shapes_from_osm(str(tmp_path / "nope.pbf"), ["LU"], str(tmp_path / "o.geojson"))


def test_local_shapes_refuse_no_countries(tmp_path):
    from pypsa_earth_ffl.tools._pypsa_earth_tools.local_shapes import (
        LocalShapesError, country_shapes_from_osm,
    )

    pbf = tmp_path / "x.osm.pbf"
    pbf.write_bytes(b"")
    with pytest.raises(LocalShapesError, match="no countries requested"):
        country_shapes_from_osm(str(pbf), [], str(tmp_path / "o.geojson"))


# --- upstream must win over a same-named file nearer sys.path ---------------


@needs_upstream
def test_a_stray_helpers_does_not_shadow_the_checkout(tmp_path, monkeypatch):
    """A stale `_helpers.py` in the working directory silently replaced the
    checkout's, and the symptom was a missing-dependency error naming a package
    the real file does not import. sys.path[0] is the running script's directory
    and beats anything appended, so upstream modules are loaded by PATH."""
    import sys

    stray = tmp_path / "_helpers.py"
    stray.write_text("raise ImportError('stale copy that must never be loaded')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("_helpers", None)
    upstream._prepare_path.cache_clear()

    mod = upstream.load("download_osm_data")
    assert hasattr(mod, "country_list_to_geofk")
    loaded = sys.modules["_helpers"].__file__
    assert str(upstream.repo_dir()) in loaded, f"_helpers came from {loaded}"


@needs_upstream
def test_scripts_are_loaded_from_the_checkout_path():
    mod = upstream.load("clean_osm_data")
    assert str(upstream.repo_dir()) in (mod.__file__ or "")


def test_a_nonexistent_script_is_named(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("FW_PYPSA_EARTH_DIR", str(tmp_path))
    upstream._prepare_path.cache_clear()
    with pytest.raises(upstream.UpstreamMissing, match="no such upstream script"):
        upstream.load("not_a_script")
    upstream._prepare_path.cache_clear()


# --- serving earth_osm from local extracts ---------------------------------


def test_local_extract_path_comes_from_earth_osms_own_table():
    """The mapping is not invented here: earth_osm's region record gives the
    Geofabrik URL, whose path is the layout a planet split already writes."""
    from pypsa_earth_ffl.tools._pypsa_earth_tools.local_extracts import geofabrik_relpath

    rel, fname = geofabrik_relpath("LU")
    assert rel == "europe/luxembourg-latest.osm.pbf"
    assert fname == "luxembourg-latest.osm.pbf"


def test_prefill_links_and_writes_a_verifiable_md5(tmp_path):
    """earth_osm reuses an existing PBF but still verifies it, so the checksum
    has to be there and has to match, or it silently re-downloads."""
    import hashlib

    from pypsa_earth_ffl.tools._pypsa_earth_tools.local_extracts import prefill

    root = tmp_path / "roots" / "europe"
    root.mkdir(parents=True)
    src = root / "luxembourg-latest.osm.pbf"
    src.write_bytes(b"not really a pbf, but bytes are bytes")

    data_dir = tmp_path / "data"
    report = prefill(["LU"], str(data_dir), [str(tmp_path / "roots")])
    assert report["served"] == ["LU"] and report["absent"] == []

    placed = data_dir / "pbf" / "luxembourg-latest.osm.pbf"
    assert placed.exists()
    assert placed.stat().st_ino == src.stat().st_ino, "should hard-link, not copy"
    md5_line = (data_dir / "pbf" / "luxembourg-latest.osm.pbf.md5").read_text()
    assert md5_line.split()[0] == hashlib.md5(src.read_bytes()).hexdigest()
    assert md5_line.split()[1] == "luxembourg-latest.osm.pbf"


def test_prefill_reports_what_it_could_not_serve(tmp_path):
    from pypsa_earth_ffl.tools._pypsa_earth_tools.local_extracts import prefill

    report = prefill(["LU", "MT"], str(tmp_path / "data"), [str(tmp_path / "empty")])
    assert report["served"] == [] and report["absent"] == ["LU", "MT"]
