"""Command-line interface: `scale-test <subcommand> SUITE.yaml ...`.

Every subcommand starts by loading the suite YAML and syncing it into the
database (see db.DB.sync_scenario), so test runs and batches are always linked
to the scenario version that was actually in the YAML when they ran.

Subcommands
-----------
sync     validate the YAML and record new scenario versions
list     overview of scenarios, their tests and batches
show     resolved config and the exact commands test/submit would run
test     run a scenario locally with justin-test-jobscript (--dev for local
         extras) and record pass/fail
submit   create a batch: one justIN campaign + N workflows of a scenario
status   refresh the justIN state of recorded workflows
export   dump the database as JSON for later analysis
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import yaml

from . import justin as J
from .config import ConfigError, Suite, load_suite
from .db import DB

# Dashboard base URLs, used only to print helpful links.
DASHBOARDS = {
    'int':  'https://justin-int.dune.hep.ac.uk/dashboard/',
    'fnal': 'https://dunejustin.fnal.gov/dashboard/',
}


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------
def default_state_dir() -> Path:
    """state/ next to the scale-test entry point (i.e. in the scale-testing
    checkout), unless $SCALE_TEST_STATE says otherwise."""
    env = os.environ.get('SCALE_TEST_STATE')
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / 'state'


def open_suite(args):
    """Load the YAML, open its database and sync every scenario.

    Returns (suite, db, versions) where versions maps scenario name to its
    current scenario_versions row.
    """
    suite = load_suite(args.suite)
    db = DB(Path(args.state_dir) / f'{suite.name}.sqlite')
    versions, created = {}, []
    for name, scen in suite.scenarios.items():
        row, new = db.sync_scenario(suite.name, name, scen.config,
                                    scen.config_sha)
        versions[name] = row
        if new:
            created.append(f'{name} v{row["version"]}')
    if created and args.command != 'sync':
        print(f'(recorded new scenario versions: {", ".join(created)})')
    return suite, db, versions


def jobscript_text(scen) -> str:
    if not scen.jobscript_path.is_file():
        raise ConfigError(f'jobscript not found: {scen.jobscript_path}')
    return scen.jobscript_path.read_text()


def dashboard_link(suite: Suite, what: str, ident: int):
    base = DASHBOARDS.get(suite.instance)
    if not base:
        return None
    if what == 'campaign':
        return f'{base}?method=show-campaign&campaign_id={ident}'
    return f'{base}?method=show-workflow&workflow_id={ident}'


def table(rows: list, headers: list):
    """Print a simple left-aligned text table."""
    rows = [[('' if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    line = '  '.join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print('  '.join('-' * w for w in widths))
    for r in rows:
        print('  '.join(c.ljust(w) for c, w in zip(r, widths)))


def inputs_summary(cfg: dict) -> str:
    if cfg.get('mql'):
        return f'mql: {cfg["mql"]}'
    return f'monte_carlo: {cfg["monte_carlo"]}'


# ---------------------------------------------------------------------------
# sync / list / show
# ---------------------------------------------------------------------------
def cmd_sync(args):
    suite = load_suite(args.suite)
    db = DB(Path(args.state_dir) / f'{suite.name}.sqlite')
    rows = []
    for name, scen in suite.scenarios.items():
        row, new = db.sync_scenario(suite.name, name, scen.config,
                                    scen.config_sha)
        rows.append([name, row['version'], 'NEW' if new else 'unchanged',
                     scen.workflows_per_batch, inputs_summary(scen.config)])
    print(f'suite {suite.name}  instance {suite.instance}  db {db.path}\n')
    table(rows, ['scenario', 'version', 'status', 'wf/batch', 'inputs'])


def cmd_list(args):
    suite, db, versions = open_suite(args)
    rows = []
    for name in suite.scenarios:
        vid = versions[name]['id']
        # Most recent test of the current version, dev and non-dev separately.
        last = {}
        for dev in (0, 1):
            r = db.query('SELECT result FROM test_runs WHERE '
                         'scenario_version_id=? AND dev=? '
                         'ORDER BY id DESC LIMIT 1', (vid, dev))
            last[dev] = r[0]['result'] if r else '-'
        nb = db.query('SELECT COUNT(*) n FROM batches b JOIN scenario_versions'
                      ' v ON b.scenario_version_id=v.id WHERE v.suite=? AND '
                      'v.name=?', (suite.name, name))[0]['n']
        nw = db.query('SELECT COUNT(*) n FROM workflows w JOIN batches b ON '
                      'w.batch_id=b.id JOIN scenario_versions v ON '
                      'b.scenario_version_id=v.id WHERE v.suite=? AND v.name=?',
                      (suite.name, name))[0]['n']
        rows.append([name, versions[name]['version'], last[0], last[1], nb, nw])
    table(rows, ['scenario', 'version', 'last test', 'last dev test',
                 'batches (all versions)', 'workflows'])


def cmd_show(args):
    suite, db, versions = open_suite(args)
    scen = suite.scenario(args.scenario)
    v = versions[scen.name]
    print(f'# scenario {scen.name}  version {v["version"]}  '
          f'config sha {scen.config_sha[:12]}')
    print(f'# jobscript {scen.jobscript_path}')
    print(yaml.safe_dump(scen.config, sort_keys=False).rstrip())

    argv, extra_env = J.test_command(suite, scen, args.dev,
                                     args.justin_test_jobscript, args.justin)
    print(f'\n# test{" (--dev)" if args.dev else ""}:')
    for k, val in extra_env.items():
        print(f'#   with {k}={val if k != "PATH" else val.split(os.pathsep)[0] + ":..."}')
    print(J.fmt(argv))

    justin = args.justin or J.DEFAULT_JUSTIN
    print(f'\n# submit (one batch = 1 campaign + {scen.workflows_per_batch} '
          'workflow(s)):')
    print(J.fmt(J.create_campaign_command(suite, scen, justin,
                                          '<description>', '<id-file>')))
    print(J.fmt(J.simple_workflow_command(suite, scen, justin,
                                          '<description>', '<CAMPAIGN_ID>',
                                          '<id-file>')))


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------
def cmd_test(args):
    suite, db, versions = open_suite(args)
    scen = suite.scenario(args.scenario)
    version = versions[scen.name]
    text = jobscript_text(scen)

    argv, extra_env = J.test_command(suite, scen, args.dev,
                                     args.justin_test_jobscript, args.justin)
    print(f'# {suite.name}/{scen.name} v{version["version"]} '
          f'{"(dev) " if args.dev else ""}on instance {suite.instance}')
    print(J.fmt(argv))
    if args.dry_run:
        return 0

    sha = db.store_blob(text)
    run_id = db.start_test_run(version['id'], args.dev, argv, sha)

    # The run id makes the log name unique even for runs started in the same
    # second; the timestamp makes logs easy to sort and find by eye.
    stamp = datetime.datetime.now(datetime.timezone.utc) \
        .strftime('%Y%m%dT%H%M%SZ')
    log_dir = Path(args.state_dir) / 'logs' / suite.name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (f'test{run_id}-{scen.name}-v{version["version"]}'
                          f'{"-dev" if args.dev else ""}-{stamp}.log')
    db.set_test_log(run_id, str(log_path))
    try:
        rc, output = J.run(argv, extra_env, log_path)
    except FileNotFoundError as e:
        db.finish_test_run(run_id, 'fail', [f'could not run: {e}'], None)
        raise ConfigError(f'could not run {argv[0]}: {e}')
    except KeyboardInterrupt:
        db.finish_test_run(run_id, 'fail', ['interrupted'], None)
        print('\ninterrupted; recorded as fail')
        return 130

    result, workdir, detail = J.judge_test(output, scen)
    if rc != 0:
        detail.insert(0, f'note: {argv[0]} exited {rc}')
    db.finish_test_run(run_id, result, detail, workdir)

    print(f'\n===== test run {run_id}: {result.upper()} =====')
    for d in detail:
        print(f'  {d}')
    print(f'  log:     {log_path}')
    if workdir:
        print(f'  workdir: {workdir}  (delete when done; outputs can be large)')
    return 0 if result == 'pass' else 1


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------
def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print('stdin is not a terminal; use --yes to submit non-interactively')
        return False
    return input(f'{prompt} [y/N] ').strip().lower() in ('y', 'yes')


def cmd_submit(args):
    suite, db, versions = open_suite(args)
    scen = suite.scenario(args.scenario)
    version = versions[scen.name]
    text = jobscript_text(scen)
    justin = args.justin or J.DEFAULT_JUSTIN
    n = args.n or scen.workflows_per_batch
    tag = f'{suite.name}/{scen.name}/v{version["version"]}'

    # ---- summary, warnings, confirmation --------------------------------
    print(f'Submit batch of {scen.name} v{version["version"]} '
          f'to instance {suite.instance}')
    print(f'  workflows:  {n}')
    print(f'  {inputs_summary(scen.config)}')
    for o in (scen.config.get('workflow') or {}).get('outputs') or []:
        print(f'  output:     {o["pattern"]} -> {o.get("destination", "(default)")}')
    print(f'  jobscript:  {scen.jobscript_path}')
    if args.note:
        print(f'  note:       {args.note}')

    sha = db.store_blob(text)
    if not db.has_passing_test(version['id'], sha):
        print('\n  WARNING: this scenario version + jobscript has no passing '
              'non-dev test.\n           Run `scale-test test` first '
              'unless you know why not.')

    if args.dry_run:
        print('\n# dry run: would run')
        print(J.fmt(J.create_campaign_command(suite, scen, justin,
                                              f'{tag}/bN', '<id-file>')))
        print(J.fmt(J.simple_workflow_command(
            suite, scen, justin, f'{tag}/bN/1of{n}', '<CAMPAIGN_ID>',
            '<id-file>')))
        if n > 1:
            print(f'# ... x{n} (descriptions .../2of{n} etc.)')
        return 0

    if not args.yes and not confirm('\nProceed?'):
        print('aborted')
        return 1

    # ---- create the batch and its justIN campaign ----------------------
    batch_id = db.start_batch(version['id'], suite.instance, sha, n, args.note)
    ids_dir = Path(args.state_dir) / 'ids' / suite.name
    ids_dir.mkdir(parents=True, exist_ok=True)
    camp_file = ids_dir / f'batch{batch_id}-campaign.txt'
    wf_file = ids_dir / f'batch{batch_id}-workflows.txt'

    argv = J.create_campaign_command(suite, scen, justin,
                                     f'{tag}/b{batch_id}', str(camp_file))
    rc, _ = J.run(argv)
    campaign_id = J.read_last_id(camp_file)
    if rc != 0 or campaign_id is None:
        print(f'create-campaign failed (exit {rc}); batch {batch_id} '
              'recorded with no workflows')
        return 1
    db.set_batch_campaign(batch_id, campaign_id)
    print(f'batch {batch_id}: justIN campaign {campaign_id}')

    # ---- create the workflows, recording each one as it's made ---------
    made = 0
    for i in range(1, n + 1):
        argv = J.simple_workflow_command(suite, scen, justin,
                                         f'{tag}/b{batch_id}/{i}of{n}',
                                         campaign_id, str(wf_file))
        before = J.read_last_id(wf_file)
        rc, _ = J.run(argv)
        wf_id = J.read_last_id(wf_file)
        if rc != 0 or wf_id is None or wf_id == before:
            print(f'simple-workflow {i}/{n} failed (exit {rc}); stopping. '
                  f'{made} workflow(s) were created and are recorded.')
            return 1
        db.add_workflow(batch_id, wf_id, argv)
        made += 1
        print(f'  workflow {wf_id} ({i}/{n})')

    print(f'\nbatch {batch_id} submitted: {made} workflow(s) in campaign '
          f'{campaign_id}')
    link = dashboard_link(suite, 'campaign', campaign_id)
    if link:
        print(f'  {link}')
    return 0


# ---------------------------------------------------------------------------
# status / export
# ---------------------------------------------------------------------------
def cmd_status(args):
    suite, db, versions = open_suite(args)
    justin = args.justin or J.DEFAULT_JUSTIN

    sql = ('SELECT w.id, w.workflow_id, w.last_state, b.id AS batch_id, '
           'b.justin_campaign_id, b.instance, v.name, v.version '
           'FROM workflows w JOIN batches b ON w.batch_id=b.id '
           'JOIN scenario_versions v ON b.scenario_version_id=v.id '
           'WHERE v.suite=?')
    params = [suite.name]
    if args.scenario:
        sql += ' AND v.name=?'
        params.append(args.scenario)
    if args.batch:
        sql += ' AND b.id=?'
        params.append(args.batch)
    rows = db.query(sql + ' ORDER BY b.id, w.workflow_id', tuple(params))
    if not rows:
        print('no workflows recorded')
        return 0

    out = []
    for r in rows:
        state = r['last_state']
        if not args.no_refresh:
            # Batches remember which instance they went to, in case the
            # suite's instance has been changed since.
            argv = J.justin_base(justin, r['instance']) + [
                'show-workflows', '--workflow-id', str(r['workflow_id'])]
            rc, output = J.run(argv, echo=False)
            new = J.parse_workflow_state(output) if rc == 0 else None
            if new:
                db.update_workflow_state(r['id'], new)
                state = new
            else:
                state = f'{state or "?"} (refresh failed)'
        out.append([r['batch_id'], r['justin_campaign_id'],
                    f'{r["name"]} v{r["version"]}', r['workflow_id'], state])
    table(out, ['batch', 'campaign', 'scenario', 'workflow', 'state'])


def cmd_export(args):
    suite = load_suite(args.suite)
    db = DB(Path(args.state_dir) / f'{suite.name}.sqlite')
    data = db.dump()
    if not args.blobs:
        data.pop('blobs')   # jobscript texts; large and rarely needed
    text = json.dumps(data, indent=2)
    if args.output:
        Path(args.output).write_text(text)
        print(f'wrote {args.output}')
    else:
        print(text)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog='scale-test',
        description='Define justIN scale tests in a suite YAML, run them '
                    'locally with justin-test-jobscript, submit batches of '
                    'workflows, and record it all in a local database.')
    p.add_argument('--state-dir', default=str(default_state_dir()),
                   help='where databases, logs and ID files live '
                        '(default: %(default)s; or $SCALE_TEST_STATE)')
    p.add_argument('--justin', default=None,
                   help='justin command to use (default: `justin` on PATH, '
                        'or dev.justin with test --dev)')
    p.add_argument('--justin-test-jobscript', default=None,
                   help='justin-test-jobscript command to use (default: on '
                        'PATH, or dev.justin_test_jobscript with --dev)')
    sub = p.add_subparsers(dest='command', required=True)

    def add(name, func, help_, scenario=None):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument('suite', help='suite YAML file')
        if scenario == 'required':
            sp.add_argument('scenario')
        elif scenario == 'optional':
            sp.add_argument('scenario', nargs='?')
        sp.set_defaults(func=func)
        return sp

    add('sync', cmd_sync, 'validate the suite and record scenario versions')
    add('list', cmd_list, 'overview of scenarios, tests and batches')

    sp = add('show', cmd_show, 'show resolved config and commands',
             'required')
    sp.add_argument('--dev', action='store_true',
                    help='show the test command with dev extras')

    sp = add('test', cmd_test,
             'run a scenario locally with justin-test-jobscript', 'required')
    sp.add_argument('--dev', action='store_true',
                    help='apply the suite\'s dev block (local only)')
    sp.add_argument('--dry-run', action='store_true',
                    help='print the command without running it')

    sp = add('submit', cmd_submit,
             'submit a batch (1 justIN campaign + N workflows)', 'required')
    sp.add_argument('-n', type=int, default=None,
                    help='number of workflows (default: workflows_per_batch)')
    sp.add_argument('--note', default=None,
                    help='free text stored with the batch, e.g. why it was '
                         'rerun')
    sp.add_argument('--dry-run', action='store_true',
                    help='print the commands without running them')
    sp.add_argument('--yes', action='store_true',
                    help='do not ask for confirmation')

    sp = add('status', cmd_status, 'refresh and show workflow states',
             'optional')
    sp.add_argument('--batch', type=int, default=None,
                    help='only this batch id')
    sp.add_argument('--no-refresh', action='store_true',
                    help='show recorded states without asking justIN')

    sp = add('export', cmd_export, 'dump the database as JSON')
    sp.add_argument('-o', '--output', help='write to this file instead of '
                                           'stdout')
    sp.add_argument('--blobs', action='store_true',
                    help='include the stored jobscript texts')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, 'n', None) is not None and args.n < 1:
        print('error: -n must be >= 1', file=sys.stderr)
        return 2
    try:
        return args.func(args) or 0
    except ConfigError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
