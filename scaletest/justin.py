"""Build and run the justin / justin-test-jobscript commands for a scenario.

The central idea is that a local test and a real submission of a scenario get
the *same* jobscript, the same input selection (--mql / --monte-carlo) and the
same set of --env NAME=VALUE pairs. The two command builders below share
``_input_args`` and ``_env_args`` so they cannot drift apart.

What differs:
  * submission adds workflow-level options (outputs, memory, scope, ...) and
    the campaign / workflow-id bookkeeping;
  * a test with ``--dev`` adds the suite's dev extras (env overrides, --bind,
    --x509-cert-dir, patched commands), which are never used for submission.

All commands are run with an argument list (no shell), so values containing
spaces or quotes in an MQL query are passed through exactly.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .config import WORKFLOW_OPTIONS, Scenario, Suite

# Defaults when no override is given: whatever is on PATH, which on a
# dunegpvm after `setup justin` is the stock justIN client and test script.
DEFAULT_JUSTIN = 'justin'
DEFAULT_TEST_JOBSCRIPT = 'justin-test-jobscript'


# ---------------------------------------------------------------------------
# Pieces shared by test and submit
# ---------------------------------------------------------------------------
def _input_args(scen: Scenario) -> list:
    """--mql QUERY or --monte-carlo N, identical for test and submit."""
    cfg = scen.config
    if cfg.get('mql'):
        return ['--mql', cfg['mql']]
    return ['--monte-carlo', str(cfg['monte_carlo'])]


def _env_args(env: dict) -> list:
    """One --env NAME=VALUE per variable, in a stable (sorted) order."""
    args = []
    for k in sorted(env):
        args += ['--env', f'{k}={env[k]}']
    return args


def justin_base(justin: str, instance: str) -> list:
    """`justin --instance INST`: every justin call names the instance
    explicitly, so a stray $JUSTIN_OPTIONS can't redirect a submission."""
    return [justin, '--instance', instance]


# ---------------------------------------------------------------------------
# Local test: justin-test-jobscript
# ---------------------------------------------------------------------------
def test_command(suite: Suite, scen: Scenario, dev: bool,
                 test_jobscript: str = None, justin: str = None):
    """Return (argv, extra_environment) for running the scenario locally.

    ``extra_environment`` is merged into os.environ by the caller. It sets
    JUSTIN_OPTIONS because justin-test-jobscript calls plain `justin`
    internally (show-replicas, get-token) and has no --instance option of its
    own; JUSTIN_OPTIONS is how the justin client picks up extra options.
    """
    d = suite.dev if dev else {}
    tjs = (test_jobscript or d.get('justin_test_jobscript')
           or DEFAULT_TEST_JOBSCRIPT)

    env = dict(scen.config['env'])
    if dev:
        env.update(d.get('env', {}))      # e.g. job_stuff=${PWD}, pfn_override

    argv = [tjs, '--jobscript', str(scen.jobscript_path)]
    argv += _input_args(scen)
    image = (scen.config.get('workflow') or {}).get('image')
    if image:
        argv += ['--image', str(image)]
    argv += _env_args(env)
    if dev:
        # These two options only exist in the locally patched
        # justin-test-jobscript, hence dev-only.
        if d.get('x509_cert_dir'):
            argv += ['--x509-cert-dir', d['x509_cert_dir']]
        for b in d.get('binds', []):
            argv += ['--bind', b]

    extra_env = {'JUSTIN_OPTIONS': f'--instance {suite.instance}'}
    # justin-test-jobscript finds `justin` on PATH. If a specific client was
    # requested (e.g. one that trusts the newer UK e-Science CA), put its
    # directory first on PATH so that's the one used.
    jcmd = justin or d.get('justin')
    if jcmd and '/' in jcmd:
        extra_env['PATH'] = (str(Path(jcmd).parent) + os.pathsep
                             + os.environ.get('PATH', ''))
    return argv, extra_env


# ---------------------------------------------------------------------------
# Real submission: justin create-campaign / simple-workflow
# ---------------------------------------------------------------------------
def create_campaign_command(suite: Suite, scen: Scenario, justin: str,
                            description: str, id_file: str) -> list:
    argv = justin_base(justin, suite.instance) + [
        'create-campaign', '--description', description,
        '--campaign-id-file', id_file]
    quota = (scen.config.get('workflow') or {}).get('quota')
    if quota:
        # The campaign's quota must match the workflows' scope quota, or
        # justIN refuses to put the workflows in it.
        argv += ['--quota', str(quota)]
    return argv


def workflow_options(scen: Scenario) -> list:
    """Turn the scenario's ``workflow:`` block into simple-workflow options,
    using the table in config.WORKFLOW_OPTIONS."""
    wf = scen.config.get('workflow') or {}
    args = []
    for key in sorted(wf):
        opt, kind = WORKFLOW_OPTIONS[key]
        val = wf[key]
        if kind == 'scalar':
            args += [opt, str(val)]
        elif kind == 'list':
            for v in (val if isinstance(val, list) else [val]):
                args += [opt, str(v)]
        elif kind == 'dict':
            for k in sorted(val):
                args += [opt, f'{k}={val[k]}']
        elif kind == 'outputs':
            # PATTERN or PATTERN:DESTINATION. DESTINATION is a Rucio dataset
            # prefix, or an https:// WebDAV directory (e.g. FNAL scratch).
            for o in val:
                spec = o['pattern']
                if o.get('destination'):
                    spec += ':' + str(o['destination'])
                args += ['--output-pattern', spec]
        # kind == 'campaign' (quota) is used by create_campaign_command only
    return args


def simple_workflow_command(suite: Suite, scen: Scenario, justin: str,
                            description: str, campaign_id,
                            id_file: str) -> list:
    argv = justin_base(justin, suite.instance) + [
        'simple-workflow', '--description', description]
    argv += _input_args(scen)
    argv += ['--jobscript', str(scen.jobscript_path)]
    argv += workflow_options(scen)
    argv += _env_args(scen.config['env'])
    argv += ['--campaign-id', str(campaign_id), '--workflow-id-file', id_file]
    return argv


def show_workflow_command(suite: Suite, justin: str, workflow_id: int) -> list:
    return justin_base(justin, suite.instance) + [
        'show-workflows', '--workflow-id', str(workflow_id)]


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------
def fmt(argv: list) -> str:
    """Shell-quoted form of a command, for display and copy/paste."""
    return shlex.join(argv)


def run(argv: list, extra_env: dict = None, log_path: Path = None,
        echo: bool = True):
    """Run a command, streaming its merged stdout/stderr to the terminal as
    it happens (justin-test-jobscript runs can take minutes) and optionally
    copying it to ``log_path``. Returns (exit_code, full_output_text)."""
    env = dict(os.environ)
    env.update(extra_env or {})
    log = open(log_path, 'w') if log_path else None
    out = []
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, text=True,
                                bufsize=1)
        for line in proc.stdout:
            out.append(line)
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
            if log:
                log.write(line)
                log.flush()
        rc = proc.wait()
    finally:
        if log:
            log.close()
    return rc, ''.join(out)


def read_last_id(id_file: Path):
    """justin --workflow-id-file / --campaign-id-file *append* a line with the
    new ID. Return the last integer line, or None if there isn't one."""
    if not id_file.exists():
        return None
    lines = [l.strip() for l in id_file.read_text().splitlines() if l.strip()]
    try:
        return int(lines[-1]) if lines else None
    except ValueError:
        return None


def parse_workflow_state(output: str):
    """Pick the state out of `justin show-workflows --workflow-id N`.

    The service prints one line per workflow as
    "%5d %-9s %s %s %s" % (id, state, created, description, mql),
    so the state is the second whitespace-separated field.
    """
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            return parts[1]
    return None


# ---------------------------------------------------------------------------
# Judging a local test
# ---------------------------------------------------------------------------
WORKDIR_RE = re.compile(r'^Files created under (\S+)', re.M)


def judge_test(output: str, scen: Scenario):
    """Decide whether a justin-test-jobscript run passed.

    justin-test-jobscript always exits 0, whatever the jobscript did, so the
    exit code tells us nothing. Instead we apply the same tests justIN applies
    to a finished job, using the files left in the test's workspace:

      1. the jobscript wrote justin-processed-pfns.txt or
         justin-processed-dids.txt (otherwise justIN treats the input file as
         unprocessed and the job as having done nothing useful);
      2. every --output-pattern of the scenario matches at least one file
         (otherwise justIN would have nothing to upload for that pattern).

    Returns (result, workdir, detail) where result is 'pass' or 'fail' and
    detail is a list of human-readable check results.
    """
    detail = []
    m = WORKDIR_RE.search(output)
    if not m:
        return 'fail', None, ['justin-test-jobscript did not report a work '
                              'directory (it failed before creating one)']
    workdir = m.group(1)
    ws = Path(workdir) / 'home' / 'workspace'

    if '====Start of jobscript execution====' not in output:
        detail.append('FAIL jobscript never started (look for errors before '
                      '"Start of jobscript execution" in the log)')
        return 'fail', workdir, detail

    files = sorted(p.name for p in ws.iterdir()) if ws.is_dir() else []

    ok = True
    processed = [f for f in ('justin-processed-pfns.txt',
                             'justin-processed-dids.txt') if f in files]
    if processed:
        detail.append(f'ok   processed list written: {", ".join(processed)}')
    else:
        ok = False
        detail.append('FAIL no justin-processed-pfns.txt / '
                      'justin-processed-dids.txt in workspace')

    # justIN matches output patterns with Bash pattern rules; fnmatch covers
    # the same *, ? and [...] forms used here.
    for o in (scen.config.get('workflow') or {}).get('outputs') or []:
        hits = fnmatch.filter(files, o['pattern'])
        if hits:
            detail.append(f'ok   output {o["pattern"]!r}: {len(hits)} file(s)')
        else:
            ok = False
            detail.append(f'FAIL output {o["pattern"]!r}: no matching files')

    return ('pass' if ok else 'fail'), workdir, detail
