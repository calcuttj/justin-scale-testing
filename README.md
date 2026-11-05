Repository containing tools/scripts etc to do scale testing of justin on the integration instance

## scale-test

`./scale-test` runs justIN scale tests that are defined in a YAML **suite**:

- **suite**: one YAML file (see [suites/ramp.yaml](suites/ramp.yaml)).
- **scenario**: one running condition in a suite. It sets the jobscript, the
  jobscript env vars, the workflow options (outputs, memory, ...) and the
  inputs (`mql` or `monte_carlo`).
- **batch**: one submission of N workflows of a scenario. Each batch gets its
  own justIN campaign, so it has its own dashboard page and a CSV/JSON
  export. You can submit a scenario again as a new batch, e.g. after an
  outside problem spoiled a run.

A local test (`justin-test-jobscript`) and a real submission
(`justin simple-workflow`) of a scenario use the same jobscript, the same
`--mql` / `--monte-carlo`, and the same `--env` values. Every test and batch
is recorded in `state/<suite>.sqlite` (gitignored).

Requires Python 3.9+ and PyYAML.

### Typical use

```bash
# 1. Ship the code yourself (on a dunegpvm), then put the printed directory
#    in the suite's env.job_stuff
tar cf scale-test-code.tar -C scripts stream_bytes.py
justin-cvmfs-upload scale-test-code.tar

# 2. Check the suite and see what would run
./scale-test sync suites/ramp.yaml
./scale-test show suites/ramp.yaml smoke

# 3. Test locally
./scale-test test suites/ramp.yaml smoke --dev   # laptop: dev block (local code, --bind, --x509-cert-dir)
./scale-test test suites/ramp.yaml smoke         # dunegpvm, after `setup justin`

# 4. Submit and follow
./scale-test submit suites/ramp.yaml smoke --dry-run
./scale-test submit suites/ramp.yaml smoke --note "first try"
./scale-test status suites/ramp.yaml
./scale-test list   suites/ramp.yaml
./scale-test export suites/ramp.yaml -o ramp.json
```

### Notes

- **Overrides.** A scenario can override anything under `defaults`, and can
  `extends:` another scenario. Dicts merge key by key; lists such as
  `workflow.outputs` replace the inherited value.
- **Versions.** Any change to a scenario's resolved config creates a new
  scenario version. Earlier tests and batches stay linked to the version they
  ran.
- **Pass/fail.** `test` judges a run by the files left in the workspace,
  because `justin-test-jobscript` always exits 0. A run passes if a
  `justin-processed-{dids,pfns}.txt` list was written and every output pattern
  matched at least one file. The work directory is kept in `/tmp`; delete it
  when you're done, since outputs can be large.
- **`dev` block.** Used only by `test --dev`. It relies on the locally patched
  `dune-justin/commands/justin-test-jobscript` (`--bind`, `--x509-cert-dir`)
  and on a `justin` client that trusts the UK e-Science 2026 root.
- **Untested submits.** `submit` warns if the scenario version and jobscript
  have no passing non-dev test.
- **Env values** can't contain spaces, quotes, `$` or other shell
  characters. `justin-test-jobscript` would interpret them, justIN would not,
  and the test would no longer match the jobs.
- **Output names** start with `$scale_test_tag`, which defaults to
  `st-<scenario>`. They look like `TAG.big.TIME.JOB.STAGE.bin`,
  `TAG.small....bin` and `TAG.metrics....json`, with `.dupN` added before the
  extension for copies.
