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
