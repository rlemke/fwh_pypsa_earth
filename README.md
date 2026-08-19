# fwh_pypsa_earth — PyPSA-Earth's OSM stage, in FFL

The port of `download_osm_data` → `clean_osm_data` → `build_osm_network` from
[PyPSA-Earth](https://github.com/pypsa-meets-earth/pypsa-earth): raw
OpenStreetMap power infrastructure to a grid topology of buses, lines,
converters and transformers.

## Scope — three rules of about sixty

PyPSA-Earth is **2,417 lines of Snakefile, ~60 rules**, covering sector
coupling, industry demand, renewable profiles and solving. Most of it is not
OSM, and needs ERA5 cutouts (gigabytes, CDS credentials) and an LP solver.

These are the three rules that *are* OSM. The port stops before `base_network`,
which assembles a PyPSA `.nc`, and does not include `build_shapes` — see
[Prerequisites](#prerequisites-that-are-not-optional).

## This is not the retrieve.smk port, and that is the point

[`fwh_pypsa_data`](https://github.com/rlemke/fwh_pypsa_data) collapsed **73
retrieve rules into one `foreach`**, because they were 73 near-copies of a
single shape driven by a table. It is tempting to expect the same here.

You do not get it. These three rules are three distinct algorithms — **2,168
lines of upstream Python** — so the port is roughly *one facet per rule* and
there is **no collapse ratio to report**. That was predicted rather than
discovered: the [cost-of-change experiment](https://github.com/rlemke/facetwork/tree/main/docs/thesis/experiments/pypsa-retrieve-cost-of-change)
found that the rule-per-output tax is charged only on new *downloads*, and that
`script:`-shaped rules are exactly the ones a catalogue cannot express.

So the handlers **call upstream's own `clean_data` and `built_network`** rather
than reimplementing them. The payload stays theirs; only the orchestration is
ours — the discipline that made `fwh_gridbuilder`'s comparison honest.

What FFL adds here is therefore not brevity:

- the download **fans out per country across a fleet**, where Snakemake fans out
  across one machine's cores;
- each stage is a **durable step** that survives losing the machine that started
  it, and can be retried or resumed without redoing the others;
- the OSM source can be a **local planet** instead of a rate-limited mirror.

## Verified

Luxembourg, end to end through the Facetwork runtime (`fw ffl run`), on upstream
commit `d7cf8ccf5076`:

```
fw:execute:pypsa.earth.BuildOsmGrid   completed    0.6s
pypsa.earth.DownloadOsmData           completed   16.3s   4 raw files, 0 empty
pypsa.earth.CleanOsmData              completed           generators 20, lines 525, substations 1050
pypsa.earth.BuildOsmNetwork           completed           buses 36, lines 43, transformers 10, converters 0
```

Identical counts to invoking the same handlers directly, so the runtime is not
changing the result.

⚠️ **That run is plumbing, not parity.** It was made with `names_by_shapes=false`
to avoid `build_shapes`, and the consequence is not a smaller result but a wrong
one: **all 36 buses came back with `country=NULL`**. Upstream's own default is
`names_by_shapes: true`, and this port now follows it. A run with it off warns.

The two-country fan-out (LU + MT) then failed exactly there, which is how this
was found: with no country attribution `add_buses_to_empty_countries` treats
*every* requested country as data-less, so it tried to invent two buses against
a one-row shapes file and dead-lettered on `array length 2 does not match index
length 1` after 5 retries. The fan-out itself worked — two independent download
steps (310s, 184s), merge, clean — the failure was downstream and mine.

A faithful, country-attributed run therefore needs upstream's `build_shapes`
outputs, which are outside this port's scope. What is verified today is that the
three stages execute upstream's code under Facetwork orchestration and produce a
real topology; **data parity with a PyPSA-Earth run is not claimed.**

## What the port found: upstream's cores are not callable

`clean_data` and `built_network` *look* like path-in/path-out functions — they
take `input_files`/`inputs` and `outputs` as parameters. They are not usable as
a library, and this port hit three separate instances of the same defect. All
three are invisible under Snakemake, because there the parameter and the global
are the same object.

| # | Where | What |
|---|---|---|
| 1 | `clean_osm_data.py:909,914,924` | `load_network_data` reads a **module-global** `input_files` that only `__main__` assigns (line 1124) — while `clean_data` takes `input_files` as its own parameter. Calling it as a library is a `NameError`; had a global existed, it would have silently read the wrong paths. |
| 2 | `build_osm_network.py:794` | `built_network` indexes `inputs` like a dict everywhere except here, where it reaches for `inputs.country_shapes` — an **attribute**. A dict satisfies half the contract. |
| 3 | `build_osm_network.py:852` | `add_buses_to_empty_countries` reads a module-global `geo_crs`, again assigned only in `__main__`, again also a parameter of the enclosing function. |

The handlers supply exactly what `__main__` supplies (the globals, and a dict
that also answers attribute access) rather than forking the scripts. That is
worth stating plainly: **a port surfaces this, a delegation would not** — running
their Snakefile through the [Snakemake adapter](https://github.com/rlemke/facetwork/tree/main/examples/snakemake-delegate)
would have worked first time and taught nothing about how reusable the code is.

## Fan-out, and what it costs

The workflow fans the download out **per country** — each its own durable step,
claimed by whichever runner is free, retried on its own. Upstream hands the
whole country list to one `earth_osm` call, so its download is one job on one
machine.

That is not free, and the cost is a facet rather than a footnote: earth_osm's
`out_aggregate=True` produced one merged file per feature class, so fanning out
moves the aggregation downstream into **`MergeRawOsm`**. Merging is a plain
concatenation (features carry their own `Region`), and empty inputs stay
zero-byte outputs, because `clean_osm_data` checks `getsize() > 0` — a missing
file breaks it, an empty one is the documented "nothing here".

## build_shapes — ported, and blocked on a network

`BuildShapes` ports upstream's rule, **restricted to the three outputs the OSM
stage consumes**: `country_shapes`, `offshore_shapes`, `extended_country_shape`.
Their rule makes six things from four independent functions; `gadm_shapes` is
skipped unless `include_gadm`, because `gadm()` downloads WorldPop population
rasters per country and nothing downstream of here reads it.

Two dependencies are worth knowing before planning a parity run:

- **The EEZ file cannot be automated, and not because of this port.** Upstream's
  own `eez()` reads `data/eez/eez_v11.gpkg` and, when it is missing, tells you to
  *"download it from marineregions.org and copy it in"* — a form-gated manual
  download. Given one, offshore shapes are produced normally. Without one,
  `allow_no_eez` yields **empty** offshore shapes: right for a landlocked
  country, wrong for a coastal one, so it must be asked for explicitly.
  (`country_cover`'s `eez_shapes` argument is optional in upstream's signature,
  so the no-EEZ path is theirs, not invented here — but the extended shape then
  carries no offshore buffer, and `clean_data` filters lines against it.)
- **GADM is unreachable from this network.** `countries()` fetches
  `https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg/gadm41_LUX.gpkg`; the host
  answers ICMP (128.120.146.30) but **port 443 times out**, the same way
  Geofabrik is blocked for this fleet. So the facet is written, its imports
  resolve and its refusals are tested, but `countries()` has **not** been run to
  completion here. That is a network fact, not a code one, and it is why a
  country-attributed parity run is still outstanding.

### The local-planet workaround for an unreachable GADM

`shape_source="osm"` takes national boundaries from a **local extract** instead
of GADM: `boundary=administrative` + `admin_level=2`, keyed by `ISO3166-1`. It
needs `local_pbf` and nothing else — no network at all.

**It is a substitution of provenance, not an equivalent.** GADM and OSM disagree
about disputed territory and coastline generalisation, and upstream's
`contended_flag` policy is not reproduced. The choice is returned in
`shape_source`, so a result can be labelled with where its geometry came from,
and it is never presented as a GADM shape.

**The extract must CONTAIN each country.** `osmium export` silently drops a
boundary relation it cannot close, which is what happens when a country crosses
the extract's clip edge. Measured on the central-america extract asking for nine
countries: **5 assembled, 4 did not** — the island states (CU, JM, HT, DO, CR)
came out, the mainland ones whose borders run off the edge (MX, GT, BZ, PA) did
not. A missing shape would become an *invented bus* downstream rather than an
error, so this **refuses** rather than warning, and names what was missing.

The fix is simply a containing extract, demonstrated on the country that failed:

| source extract | asked | assembled | time |
|---|---|---|---|
| `central-america` (853 MB) | MX + 8 others | **MX missing** | 36.7s |
| `north-america/mexico` (598 MB) | MX | **MX** | 21.8s |

Same country, same code; the only variable is whether the extract contains it.

**Use the smallest containing extract, not the biggest.** Filtering *relations*
is far more expensive than filtering tags, because osmium must pull their
referenced members: the same query against the 20 GB `north-america` extract was
still running after **1h35m** and was abandoned in favour of the 598 MB
country extract above, which answered in 22 seconds.

#### The attributed run this unblocked

With shapes from the local planet and `names_by_shapes=true` — no GADM, no
network:

```
BuildShapes    osm-admin-level-2, 1 country, 31.1s
CleanOsmData   generators 20, lines 535, substations 1069
BuildOsmNetwork buses 37, lines 44, transformers 11, converters 0
```

and, the point of the exercise, **all 37 buses carry `country=LU`** with no
invented nodes and contiguous ids — where the earlier unattributed run produced
37 buses of `country=NULL`.

The counts differ slightly from that earlier run (lines 525→535, substations
1050→1069) and the reason is instructive: `extended_country_shape` is now
`country_cover(...)`, which **buffers by 0.02°**, rather than the raw boundary
polygon I passed by hand. More features fall inside the filter. This run follows
upstream's own composition and the earlier one did not.

Still not parity with a PyPSA-Earth run: the geometry is OSM's, not GADM's.
What is now demonstrated is that the stage runs **correctly and attributed, on
local data alone**.

### Prerequisites that are not optional

`build_shapes` outputs are required by the OSM stage, and neither is
interchangeable:

- **`extended_country_shape`** — `clean_data` filters lines by
  `geometry.boundary.within(extended_country_shape)` (`clean_osm_data.py:992`),
  so this polygon decides which lines survive. A bounding box would not
  approximate it, it would silently change the answer.
- **`country_shapes`** — `built_network` invents a bus for every requested
  country that has no OSM data (`build_osm_network.py:794`), matching on a
  `name` column holding **ISO-2 codes**. Supply a file whose `name` is
  `Luxembourg` rather than `LU` and the country reads as data-less; the run then
  fails on an array-length mismatch rather than quietly mislabelling, which is
  the better of the two outcomes but still a trap.

The verification above supplied both from **OSM admin boundaries** (via
`fwh_osm`'s `boundary_gen`, `admin_level=2`) rather than upstream's GADM/EEZ.
That is a **deviation of provenance** and is recorded as one: it exercises the
port, it does not demonstrate data parity with a PyPSA-Earth run.

## Use it

```bash
git clone --depth 1 https://github.com/pypsa-meets-earth/pypsa-earth
export FW_PYPSA_EARTH_DIR=$PWD/pypsa-earth      # or ~/fw_handlers/upstream-pypsa-earth

pip install -e .                                 # plus upstream's own deps, below
fw runner start --domain pypsa-earth --no-dashboard
fw ffl run src/pypsa_earth_ffl/ffl/pypsa_earth.ffl \
  --workflow pypsa.earth.BuildOsmGrid \
  --inputs '{"countries":["LU"],"work_dir":"/abs/work",
             "extended_country_shape":"/abs/shapes/lu.geojson",
             "country_shapes":"/abs/shapes/country_shapes.geojson"}'
```

Upstream's scripts import `_helpers`, which imports `pypsa` — and the chain does
not stop there. Running this stage pulled, in order of discovery:

| for | packages | size |
|---|---|---|
| `_helpers` | pypsa + 27 deps | 110.7 MiB |
| `_helpers` | country_converter, reverse_geocode, CurrencyConverter, fake_useragent | 4.2 MiB |
| `build_osm_network` | scikit-learn | 7.9 MiB |
| `build_shapes` | fiona | 14.1 MiB |
| `build_shapes` | numba + llvmlite | 41.2 MiB |

**~178 MiB to run three rules of sixty**, most of it for code these rules never
execute — importing their script imports the whole model's dependency tree. A
delegation would have paid the same, since it needs their environment too. A host
without the checkout raises `PermanentError`, so the task dead-letters instead of
retrying against a machine that will never have it.

## Licence

Apache-2.0. PyPSA-Earth is AGPL/MIT-mixed — see its own LICENSES; this package
calls it, does not vendor it.
