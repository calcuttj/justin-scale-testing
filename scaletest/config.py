"""Load a suite YAML file and turn it into fully-resolved scenarios.

Terminology
-----------
suite     One YAML file. It has a name, a target justIN instance, a block of
          ``defaults``, an optional ``dev`` block, and a list of scenarios.
scenario  One running condition: which jobscript, which jobscript env vars,
          which workflow-level options (outputs, memory, ...), and which input
          files (an MQL query, or a Monte Carlo count).
batch     One submission of N workflows of a scenario (handled in cli.py).

How a scenario is resolved
--------------------------
Each scenario starts from ``defaults``. If it says ``extends: OTHER`` it starts
from OTHER's fully-resolved config instead (which itself started from
``defaults``). The scenario's own keys are then laid on top.

When laying one config on top of another ("merging"):
  * dicts merge key by key, recursively, so a scenario can override a single
    env var or a single workflow option and inherit everything else;
  * lists and plain values *replace* the inherited value outright. This matters
    for ``workflow.outputs``: giving outputs in a scenario replaces the whole
    inherited list rather than appending to it, which is usually what you want
    when scanning output layouts.

The ``dev`` block
-----------------
``dev`` holds local-only extras (a patched justin-test-jobscript, --bind,
--x509-cert-dir, env overrides such as job_stuff=${PWD}). It is only applied
by ``scale-test test --dev`` and is never sent to justIN, so it is kept out of
the scenario config and does not affect scenario versions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised for any problem with the suite YAML. The CLI prints the message
    and exits, so messages should say what is wrong and where."""


# ---------------------------------------------------------------------------
# Allowed keys
# ---------------------------------------------------------------------------
# Keys under a scenario's ``workflow:`` block, mapped to the `justin
# simple-workflow` option they become. The second element says how the value
# is turned into command-line arguments (see justin.py):
#   'scalar'   -> one option:            --rss-mib 2000
#   'list'     -> option repeated:       --output-rse A --output-rse B
#   'dict'     -> option repeated K=V:   --classad K1=V1 --classad K2=V2
#   'outputs'  -> special: list of {pattern, destination} -> --output-pattern
#   'campaign' -> not a workflow option; used for `create-campaign --quota`
# Rejecting unknown keys catches typos (e.g. ``rss_mb``) that would otherwise
# be silently ignored and give a test that isn't what you thought it was.
WORKFLOW_OPTIONS = {
    'scope':                 ('--scope', 'scalar'),
    'lifetime_days':         ('--lifetime-days', 'scalar'),
    'max_distance':          ('--max-distance', 'scalar'),
    'rss_mib':               ('--rss-mib', 'scalar'),
    'wall_seconds':          ('--wall-seconds', 'scalar'),
    'processors':            ('--processors', 'scalar'),
    'gpus':                  ('--gpus', 'scalar'),
    'gpus_min_mib':          ('--gpus-min-mib', 'scalar'),
    'gpus_min_runtime':      ('--gpus-min-runtime', 'scalar'),
    'gpus_min_capability':   ('--gpus-min-capability', 'scalar'),
    'gpus_max_capability':   ('--gpus-max-capability', 'scalar'),
    'site':                  ('--site', 'scalar'),
    'image':                 ('--image', 'scalar'),
    'output_rse':            ('--output-rse', 'list'),
    'output_rse_expression': ('--output-rse-expression', 'scalar'),
    'classads':              ('--classad', 'dict'),
    'outputs':               (None, 'outputs'),
    'quota':                 (None, 'campaign'),
}

# Keys a scenario entry may have. ``defaults`` may have the same keys except
# the ones that only make sense per scenario (name, extends, description).
SCENARIO_KEYS = {'name', 'extends', 'description', 'mql', 'monte_carlo',
                 'workflows_per_batch', 'jobscript', 'workflow', 'env'}
DEFAULTS_KEYS = SCENARIO_KEYS - {'name', 'extends', 'description'}
DEV_KEYS = {'justin', 'justin_test_jobscript', 'x509_cert_dir', 'binds', 'env'}
SUITE_KEYS = {'suite', 'instance', 'defaults', 'dev', 'scenarios'}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def deep_merge(base: dict, over: dict) -> dict:
    """Return a new dict: ``base`` with ``over`` laid on top.

    Nested dicts are merged recursively; anything else in ``over`` (lists,
    strings, numbers) replaces the value in ``base``. Inputs are not modified.
    """
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _check_keys(what: str, d: dict, allowed: set):
    """Fail loudly on unknown keys (almost always a typo)."""
    unknown = set(d) - allowed
    if unknown:
        raise ConfigError(f'{what}: unknown key(s) {sorted(unknown)}; '
                          f'allowed: {sorted(allowed)}')


# Characters not allowed in env values. justin-test-jobscript writes each
# --env as an unquoted shell line ``export NAME=VALUE``, so the shell would
# split, expand or glob such values; justIN itself passes them through
# literally. A value containing any of these would therefore behave
# differently in a local test than in a real job, which defeats the point.
UNSAFE_ENV_CHARS = re.compile(r'''[\s'"`$\\;&|<>()*?\[\]{}!#~]''')


def check_env(what: str, env: dict):
    for k, v in env.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k):
            raise ConfigError(f'{what}: env name {k!r} is not a valid shell '
                              'variable name')
        bad = UNSAFE_ENV_CHARS.search(v)
        if bad:
            raise ConfigError(
                f'{what}: env {k}={v!r} contains {bad.group()!r}, which '
                'justin-test-jobscript would interpret as shell syntax '
                '(justIN would not), so tests and jobs would differ')


def env_str(v) -> str:
    """Convert a YAML env value to the string the jobscript will see.

    YAML turns ``true`` into a Python bool, which str() would render as
    'True'; shell scripts conventionally expect lower case.
    """
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if v is None:
        raise ConfigError('env values must not be null')
    return str(v)


# ---------------------------------------------------------------------------
# Data classes returned to the rest of the tool
# ---------------------------------------------------------------------------
@dataclass
class Scenario:
    name: str
    # The fully-resolved scenario: plain dicts/lists/strings only, so it can be
    # stored as JSON in the database and hashed to detect changes.
    config: dict
    # Absolute path of the jobscript file (config['jobscript'] keeps the path
    # as written in the YAML, relative to the YAML file's directory).
    jobscript_path: Path

    @property
    def config_sha(self) -> str:
        """Hash of the resolved config. If any setting changes, this changes,
        and the database records a new scenario *version* (see db.py)."""
        return hashlib.sha256(json.dumps(self.config, sort_keys=True)
                              .encode()).hexdigest()

    @property
    def workflows_per_batch(self) -> int:
        return int(self.config.get('workflows_per_batch', 1))


@dataclass
class Suite:
    path: Path        # absolute path of the YAML file
    name: str         # the ``suite:`` value; also names the database file
    instance: str     # justIN instance, e.g. 'int' or 'fnal'
    dev: dict         # expanded dev block (see module docstring)
    scenarios: dict = field(default_factory=dict)   # name -> Scenario

    def scenario(self, name: str) -> Scenario:
        if name not in self.scenarios:
            raise ConfigError(f'No scenario {name!r} in {self.path}; '
                              f'have {list(self.scenarios)}')
        return self.scenarios[name]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _expand_dev(value, base_dir: Path, is_path: bool):
    """Expand ``${VAR}`` and ``~`` in a dev value.

    Only dev values are expanded, because they describe *this* machine (e.g.
    ``job_stuff: ${PWD}``). Scenario values are kept literally so that what is
    recorded in the database is exactly what was sent to justIN.

    For path-like values (``is_path``) a relative path containing a '/' is made
    absolute relative to the YAML file's directory, so the suite works no
    matter which directory you run scale-test from. A bare command name such
    as ``justin`` is left alone so it is looked up on PATH.
    """
    if isinstance(value, list):
        return [_expand_dev(v, base_dir, is_path) for v in value]
    s = os.path.expanduser(os.path.expandvars(str(value)))
    if is_path and ('/' in s) and not os.path.isabs(s):
        s = str((base_dir / s).resolve())
    return s


def load_suite(path) -> Suite:
    """Read the YAML file at ``path`` and return a Suite with every scenario
    fully resolved and validated. Raises ConfigError on any problem."""
    path = Path(path).resolve()
    base_dir = path.parent
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f'{path}: {e}')

    _check_keys('suite', raw, SUITE_KEYS)
    if 'suite' not in raw:
        raise ConfigError(f'{path}: missing "suite" name')
    defaults = raw.get('defaults') or {}
    _check_keys('defaults', defaults, DEFAULTS_KEYS)

    # --- dev block: local-only extras, with paths expanded -----------------
    dev_raw = raw.get('dev') or {}
    _check_keys('dev', dev_raw, DEV_KEYS)
    dev = {}
    for k in ('justin', 'justin_test_jobscript', 'x509_cert_dir'):
        if dev_raw.get(k):
            dev[k] = _expand_dev(dev_raw[k], base_dir, is_path=True)
    dev['binds'] = _expand_dev(dev_raw.get('binds') or [], base_dir,
                               is_path=True)
    dev['env'] = {k: _expand_dev(v, base_dir, is_path=False)
                  for k, v in (dev_raw.get('env') or {}).items()}
    check_env('dev', dev['env'])

    suite = Suite(path=path, name=str(raw['suite']),
                  instance=str(raw.get('instance', 'int')), dev=dev)

    # --- index scenarios by name so ``extends`` can find its parent --------
    by_name = {}
    for s in raw.get('scenarios') or []:
        if 'name' not in s:
            raise ConfigError(f'{path}: scenario without a name: {s}')
        _check_keys(f'scenario {s["name"]}', s, SCENARIO_KEYS)
        if s['name'] in by_name:
            raise ConfigError(f'duplicate scenario name {s["name"]!r}')
        by_name[s['name']] = s

    def resolve(name, chain=()):
        """Resolve one scenario, following ``extends`` recursively.

        ``chain`` is the list of scenarios we came through, used to detect
        cycles such as a extends b, b extends a.
        """
        if name in chain:
            raise ConfigError(f'extends cycle: {" -> ".join(chain + (name,))}')
        if name not in by_name:
            raise ConfigError(f'scenario {chain[-1]!r} extends unknown {name!r}')
        s = by_name[name]
        own = {k: v for k, v in s.items() if k not in ('name', 'extends')}
        if 'extends' in s:
            parent = resolve(s['extends'], chain + (name,))
            # A description belongs to the scenario that wrote it.
            parent.pop('description', None)
            # mql and monte_carlo are alternatives: if the child picks either,
            # drop both from the parent so we don't end up with both set.
            if 'mql' in own or 'monte_carlo' in own:
                parent.pop('mql', None)
                parent.pop('monte_carlo', None)
            return deep_merge(parent, own)
        return deep_merge(defaults, own)

    for name in by_name:
        cfg = {'name': name, **resolve(name)}
        _validate(cfg)

        # Env values become strings exactly as the jobscript will see them.
        cfg['env'] = {k: env_str(v) for k, v in (cfg.get('env') or {}).items()}
        # Output filenames are prefixed with this tag by the jobscript, which
        # makes a scenario's outputs easy to find in Rucio/scratch. It is
        # visible in `scale-test show` and can be overridden in the YAML.
        cfg['env'].setdefault('scale_test_tag', f'st-{name}')
        check_env(f'scenario {name}', cfg['env'])

        if not cfg.get('jobscript'):
            raise ConfigError(f'scenario {name}: no jobscript given')
        js_path = (base_dir / cfg['jobscript']).resolve()
        suite.scenarios[name] = Scenario(name=name, config=cfg,
                                         jobscript_path=js_path)
    return suite


def _validate(cfg: dict):
    """Checks that apply to a fully-resolved scenario."""
    name = cfg['name']

    # Inputs: either real files via MQL, or N Monte Carlo counter "files".
    has_mql = bool(cfg.get('mql'))
    has_mc = cfg.get('monte_carlo') is not None
    if has_mql == has_mc:
        raise ConfigError(f'scenario {name}: give exactly one of '
                          'mql / monte_carlo')

    wf = cfg.get('workflow') or {}
    _check_keys(f'scenario {name} workflow', wf, set(WORKFLOW_OPTIONS))

    for o in wf.get('outputs') or []:
        if not isinstance(o, dict) or 'pattern' not in o:
            raise ConfigError(f'scenario {name}: each output needs a '
                              f'pattern: {o}')
        _check_keys(f'scenario {name} output', o, {'pattern', 'destination'})

    # justIN requires --lifetime-days whenever outputs go to Rucio datasets
    # (anything not https:// scratch). Catch it here rather than at submit.
    rucio_outputs = [o for o in wf.get('outputs') or []
                     if not str(o.get('destination', '')).startswith('https://')]
    if rucio_outputs and not wf.get('lifetime_days'):
        raise ConfigError(f'scenario {name}: lifetime_days is required for '
                          'Rucio-dataset outputs')

    n = cfg.get('workflows_per_batch', 1)
    if not isinstance(n, int) or n < 1:
        raise ConfigError(f'scenario {name}: workflows_per_batch must be '
                          'an integer >= 1')
