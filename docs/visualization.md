# Visualizing scale tests

Notes on what a dashboard for the scale tests would involve. Nothing here is
designed or built yet. Getting the data out of justIN and into one place is
most of the work; once the data is joined, the plots are straightforward.

## What it should answer

From the scale-testing plan:

- Failure rate per scenario and batch, broken down by failure mode, site and
  RSE. The rare (about 1 in 1000) failures only show up when aggregated.
- How long each step takes: queueing, getting a file, reading input,
  uploading outputs, and justIN/Rucio/MetaCat calls.
- How things degrade as the ramp goes from 1 to 10k to 100k jobs: throughput,
  slot filling, database strain.
- Whether a rerun batch behaved differently from the original.

## Data sources

### Local `scale-test` database

`state/<suite>.sqlite` maps scenario version → batch → justIN campaign →
workflow IDs, and stores the config each batch ran. Every other source below
is keyed by workflow or job ID, so this database is what joins them together.

### justIN dashboard exports

The justIN *API* only covers sites, storages and distances
([api.info.md](https://github.com/DUNE/dune-justin/blob/main/docs/api.info.md)).
Data about jobs and files comes from the dashboard's CSV/JSON downloads:

| Dashboard method | Contents |
|---|---|
| `download-campaign-workflows` | Per-workflow state and file counts for a campaign, i.e. a batch |
| `download-events` | Typed, timestamped events. Filterable by `workflow_id`, `stage_id`, `jobsub_id`, `jobscript_exit`, `file_did` |
| `download-file-states` | Per-file states for a workflow and stage |
| `download-input-file-stats` / `download-output-file-stats` | Input and output file statistics, including RSEs |

The events are the richest source. Each job goes through
`JOB_SUBMITTED → JOB_STARTED → JOB_PROCESSING → JOB_OUTPUTTING → JOB_FINISHED`,
and failures have specific types: `JOB_SCRIPT_ERROR`, `JOB_OUTPUTTING_FAILED`,
`JOB_STALLED_HEARTBEAT`, `JOB_STALLED_HTCONDOR`, `JOB_ABORT_*`, and so on
([event_types.md](https://github.com/DUNE/dune-justin/blob/main/docs/event_types.md)).
The time spent in each step and a failure classification come almost directly
from these.

### Per-job metrics from the jobscript

`stream_bytes.py` writes `TAG.metrics.TIME.JOB.STAGE.json`, which holds each
chunk's size and read time, plus total read time and wall time. Today it is
only printed into the job log, and fetching logs one job at a time doesn't
scale to 10k+ jobs. To use it, add an output pattern for `*.metrics.*.json`
(to a small Rucio dataset or FNAL scratch) and pull the files in bulk. The job
ID is in the filename, so the metrics join to the events.

### External service health

The justIN database, MetaCat and Rucio, via Wenlong's Rucio monitoring page and
any FNAL Grafana dashboards. The simplest approach is probably to link to these
and line them up by time window, rather than pulling their data in.

## Collection step

This would be something like a future `scale-test collect` command.

- Pull the exports for each recorded batch and workflow, and store them as new
  tables (jobs, events, file states, stream metrics) in the same SQLite file.
- Fetch only data that is new since the last run, so the step can be rerun
  safely and can resume after an interruption.
- The events export caps how many rows it returns, so large workflows need
  paging or splitting by `jobsub_id`.
- Size is manageable: 100k jobs at roughly 6–10 events each is about 1M rows,
  which SQLite handles fine.
- Collect promptly. The integration instance's database is wiped at each
  minor-version change.

## Views

| View | Shows |
|---|---|
| Ramp overview | One row per scenario and batch: jobs, success %, files processed, median and 95th-percentile job time |
| Timeline | Jobs running and finishing over time per batch, with failures overlaid. This is where database trouble or slot saturation shows up |
| Failure breakdown | Counts by event type × site × RSE, linking to example jobs on the justIN dashboard |
| Phase durations | Time spent queueing, processing and outputting, by site |
| Streaming | Per-chunk read time by RSE, site and protocol (also covers the XRootD vs WebDAV comparison) |
| Batch comparison | An original batch vs a rerun of the same scenario version |

## Ways to display it

From least to most infrastructure:

1. A **static HTML report** generated from the SQLite file (plotly or
   matplotlib) after each collection. Cheapest, and easy to share.
2. A **notebook or Streamlit/Panel app** (see below): interactive, but runs
   locally.
3. **Grafana** on SQLite or Postgres: live, good for time series, and sits
   next to existing FNAL monitoring. Needs a server and someone to maintain it.

Starting with a static report and moving to Grafana only if live monitoring
is needed during the 10k+ runs is the low-risk path.

### Jupyter notebook

Cells of code and plots, e.g. `pandas.read_sql(...)` on the SQLite file, then
plotly or matplotlib. `ipywidgets` adds dropdowns and sliders.

- **Good for:** exploring the data while working out which views matter, and
  one-off investigations ("why did batch 3 fail at one site?").
- **Weak points:**
  - Cells can be run out of order, so a notebook's state can differ from what
    its code shows.
  - `.ipynb` files make messy git diffs (`jupytext` or `nbstripout` help).
  - Sharing means the other person runs it, or you export a static HTML
    snapshot (`jupyter nbconvert --to html`), which loses the widgets.
- **Voilà** serves a notebook as an app with the code hidden, a halfway step
  toward the options below.

### Streamlit

A plain `.py` script. Every widget change reruns the whole script from top to
bottom, with expensive steps (database reads) cached.

```python
import sqlite3, pandas as pd, plotly.express as px, streamlit as st

@st.cache_data
def load(db):
    con = sqlite3.connect(db)
    return pd.read_sql(
        "SELECT b.id batch, v.name scenario, w.workflow_id, w.last_state "
        "FROM workflows w JOIN batches b ON w.batch_id=b.id "
        "JOIN scenario_versions v ON b.scenario_version_id=v.id", con)

df = load("state/ramp-2026-11.sqlite")
scen = st.selectbox("Scenario", sorted(df.scenario.unique()))
sel = df[df.scenario == scen]
st.metric("Workflows", len(sel))
st.plotly_chart(px.histogram(sel, x="last_state", color="batch"))
```

Run it with `streamlit run app.py` and open `localhost:8501`.

- **Good for:** the fastest route to a usable multi-page dashboard. It's very
  little code, the widgets look decent with no styling, and it's an ordinary
  script that diffs cleanly in git.
- **Weak points:**
  - The rerun-everything model gets awkward for linked interactions (e.g.
    click a bar in one plot to filter another).
  - Large raw datasets need care: aggregate in SQL rather than plotting a
    million points.
  - Streamlit's free cloud hosting needs public code and data, so it isn't
    suitable for internal results.

### Panel (HoloViz)

Reactive: functions are bound to widgets (`pn.bind`), and only the parts that
depend on a changed widget recompute. The same code runs inside a Jupyter
notebook and as a served app (`panel serve app.py`).

- **Good for:**
  - Linked, cross-filtering views.
  - Very large data: it pairs with hvPlot, HoloViews and Datashader, which can
    render millions of points (e.g. every chunk read time from 100k jobs)
    without pre-aggregating.
  - Developing in a notebook and then serving the same code as an app.
  - Sharing without a server: `panel convert` can produce a page that runs
    Python in the browser, though not every library works that way.
- **Weak points:** a steeper learning curve and a more complex API than
  Streamlit. Smaller community, so fewer examples.

### Practicalities (notebook, Streamlit, Panel)

- **Where it runs:** the database lives wherever `scale-test` runs. dunegpvm
  ports aren't reachable from outside, so use an SSH tunnel
  (`ssh -L 8501:localhost:8501 USER@dunegpvmXX.fnal.gov`) or copy the
  `.sqlite` file to a laptop.
- **Data freshness:** the app only reads the database. A separate collection
  step refreshes it, and the app shows whatever is there.
- **Sharing:** none of these is a hosted dashboard by default. Colleagues need
  the Python environment and a copy of the database, or static exports. If
  several people need to watch live during large runs, Grafana or a hosted
  page becomes worth the setup cost.
- **Dependencies:** local environment only (`streamlit` or `panel` plus
  `hvplot`, `plotly`, `pandas`). Nothing touches the jobs.

**Suggested path:** use a notebook to work out which plots are useful once
real batch data exists. Then build a Streamlit app from them as the
dashboard to reopen after each batch. Panel is worth its extra complexity
only for linked cross-filtering, or for plotting per-chunk streaming data at
100k-job scale without aggregating it first.

## Open questions

- **Authentication for the exports.** They are dashboard pages behind the
  dashboard login. It isn't yet known whether the `justin` CLI session or a
  token can fetch them from a script. If not, it may need an API addition
  upstream.
- **The events export row cap:** its value, and whether paging covers large
  workflows.
- **Which failures count as ours.** Separating failures caused by justIN or
  the scale test from site or infrastructure noise shapes the failure
  breakdown.
- **Clock alignment** when correlating with external monitoring.
