# SPDX-License-Identifier: Apache-2.0
"""Import PyPSA-Earth's own scripts, so the port runs THEIR code.

The whole value of a port comparison is that the payload is unchanged and only
the orchestrator differs — `fwh_gridbuilder` established that discipline by
calling the same `earth_osm` extractor upstream calls. Here it matters more,
because `clean_osm_data` and `build_osm_network` are 2,168 lines of domain
algorithm: reimplementing them would produce a *different model* and the
comparison would be meaningless, quite apart from the maintenance.

Fortunately both scripts expose a path-in/path-out core —
``clean_data(input_files, output_files, …)`` and
``built_network(inputs, outputs, …)`` — which their ``__main__`` blocks call
with paths Snakemake resolved. A handler can call exactly those.

The catch is that PyPSA-Earth's scripts are not an installable package: they
import each other as top-level modules (``from _helpers import …``) and read
config relative to a repo root. So this module puts the checkout's ``scripts``
directory on ``sys.path`` and pins ``BASE_DIR``-style expectations, rather than
pretending an import works when it does not.

Point ``FW_PYPSA_EARTH_DIR`` at a checkout::

    git clone --depth 1 https://github.com/pypsa-meets-earth/pypsa-earth
    export FW_PYPSA_EARTH_DIR=$PWD/pypsa-earth
"""

from __future__ import annotations

import os
import sys
import importlib.util
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Default checkout location, so a host that follows the convention needs no env.
DEFAULT_DIR = Path.home() / "fw_handlers" / "upstream-pypsa-earth"


class UpstreamMissing(RuntimeError):
    """PyPSA-Earth's source is not available on this host.

    A deployment fact, not a transient fault — handlers turn this into a
    ``PermanentError`` so a task dead-letters instead of retrying five times on a
    machine that will never have the checkout.
    """


def repo_dir() -> Path:
    return Path(os.environ.get("FW_PYPSA_EARTH_DIR") or DEFAULT_DIR)


def require_repo() -> Path:
    root = repo_dir()
    if not (root / "scripts").is_dir():
        raise UpstreamMissing(
            f"PyPSA-Earth checkout not found at {root} (no scripts/ dir). "
            "git clone --depth 1 https://github.com/pypsa-meets-earth/pypsa-earth "
            "and/or set FW_PYPSA_EARTH_DIR."
        )
    return root


def _from_file(name: str, path: Path) -> Any:
    """Import ``name`` from an exact file, and pin it in sys.modules.

    Loading by NAME is not good enough here. Their scripts do
    ``from _helpers import …``, which resolves through sys.path — and sys.path[0]
    is the directory of whatever script is running, which beats anything this
    module appends. A stale ``_helpers.py`` left lying in a working directory
    therefore silently replaces the checkout's, and the failure surfaces as a
    missing dependency of the WRONG file (observed: a months-old copy demanding
    `atlite`, which the current one does not import at all).

    Loading from the file and pinning the result under its own name makes the
    answer independent of where the process happens to have been started.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
        raise UpstreamMissing(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _prepare_path() -> Path:
    """Put the checkout's scripts dir on sys.path and pin its `_helpers`.

    Cached because repeated insertion would grow sys.path on every task on a
    long-lived runner, and because import side effects should happen once.
    """
    root = require_repo()
    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        # Appended, not prepended: their modules must not shadow anything the
        # runner already imported. The pinning below is what protects the other
        # direction.
        sys.path.append(str(scripts))
    helpers = scripts / "_helpers.py"
    if helpers.exists():
        _from_file("_helpers", helpers)
    return root


def load(script: str) -> Any:
    """Import one of PyPSA-Earth's scripts as a module.

    ``load("clean_osm_data").clean_data(...)`` runs upstream's cleaning, not a
    copy of it — and, thanks to `_from_file`, the copy in the checkout rather
    than any same-named file nearer the front of sys.path.
    """
    root = _prepare_path()
    path = root / "scripts" / f"{script}.py"
    if not path.exists():
        raise UpstreamMissing(f"no such upstream script: {path}")
    try:
        return _from_file(script, path)
    except ImportError as exc:  # pragma: no cover - depends on their deps
        raise UpstreamMissing(
            f"could not import PyPSA-Earth's {script}.py: {exc}. Its own "
            "dependencies (geopandas, shapely, pandas, networkx…) must be "
            "importable in this runner's environment."
        ) from exc


def version() -> str:
    """Identify WHICH upstream this ran against — a port's result is only
    interpretable against a specific commit."""
    root = repo_dir()
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            sha = (root / ".git" / ref[5:]).read_text().strip()
        else:
            sha = ref
        return sha[:12]
    except OSError:
        return "unknown"
