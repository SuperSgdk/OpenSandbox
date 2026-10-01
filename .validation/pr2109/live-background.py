"""Real Docker sandbox checks for the submitted CLI; no mocked SDK/HTTP calls.

Run inside the isolated Linux validation container after starting the server.
The baseline CLI copy changes only its command.py to the upstream base version.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import yaml
from opensandbox.config import ConnectionConfigSync
from opensandbox.sync.sandbox import SandboxSync

ROOT = Path(os.environ.get('OSB_VALIDATION_ROOT', '/work'))
BASELINE_SOURCE = Path(os.environ.get('OSB_BASELINE_SOURCE', '/project/validation/baseline-command.py'))
REPORT = ROOT / 'live-background-results.json'
CONFIG = ROOT / 'pr2109-client.toml'
CONFIG.write_text('')
BASELINE = ROOT / 'baseline-cli'
shutil.copytree(ROOT / 'cli/src/opensandbox_cli', BASELINE / 'opensandbox_cli', dirs_exist_ok=True)
shutil.copyfile(BASELINE_SOURCE, BASELINE / 'opensandbox_cli/commands/command.py')
results = []
report = {
    'source_commit': '889a5e6be15ce6259332e8e04549d805ecf27a2b',
    'baseline_commit': 'c7dc78a4090e5de2b9119e9bd93952cae24f87bd',
    'environment': os.environ.get('OSB_VALIDATION_ENV', 'Local Docker Desktop Linux, real source server/SDK/CLI, published execd v1.1.0'),
    'cases': results,
    'sandbox_destroyed': False,
}


def invoke(sandbox_id, proxy, baseline, fmt, extra, payload, background=True):
    args = [str(ROOT / 'cli/.venv/bin/osb'), '--config', str(CONFIG),
            '--domain', '127.0.0.1:18080', '--api-key', 'pr2109-local-test-only',
            '--protocol', 'http', '--request-timeout', '20', '--no-color',
            '--use-server-proxy' if proxy else '--no-use-server-proxy',
            'command', 'run', sandbox_id]
    if background:
        args.append('--background')
    args.extend(['-o', fmt, *extra, '--', *payload])
    env = os.environ.copy()
    if baseline:
        env['PYTHONPATH'] = str(BASELINE)
    else:
        env.pop('PYTHONPATH', None)
    started = time.monotonic()
    p = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                       env=env, timeout=30)
    return p, round(time.monotonic() - started, 3)


def execution_id(stdout, fmt):
    if fmt == 'json':
        return json.loads(stdout)['execution_id']
    if fmt == 'yaml':
        return yaml.safe_load(stdout)['execution_id']
    ids = re.findall(r'\b[0-9a-f]{32}\b', stdout)
    assert len(ids) == 1, stdout
    return ids[0]


def record(case):
    results.append(case)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(case, ensure_ascii=False), flush=True)


cfg = ConnectionConfigSync(domain='127.0.0.1:18080', protocol='http',
    api_key='pr2109-local-test-only', use_server_proxy=True,
    request_timeout=timedelta(seconds=30))
sandbox = SandboxSync.create('python:3.11-slim-bookworm', connection_config=cfg,
    entrypoint=['sleep', 'infinity'], timeout=timedelta(minutes=10),
    ready_timeout=timedelta(seconds=90), resource={'cpu': '1', 'memory': '256Mi'},
    metadata={'tag': 'pr2109-live-validation'})
report['sandbox_id'] = sandbox.id
print('Created real sandbox ' + sandbox.id, flush=True)
try:
    for proxy in [False, True]:
        route = 'server-proxy' if proxy else 'direct'
        query_cfg = ConnectionConfigSync(domain='127.0.0.1:18080', protocol='http',
            api_key='pr2109-local-test-only', use_server_proxy=proxy)
        query = SandboxSync.connect(sandbox.id, connection_config=query_cfg)
        try:
            for fmt in ['table', 'json', 'yaml']:
                failures = [
                    ('early-missing-cwd', ['--workdir', '/pr2109-directory-does-not-exist'], ['echo', 'hello']),
                    ('missing-executable', ['--argv'], ['/pr2109-executable-does-not-exist']),
                    ('non-executable-file', ['--argv'], ['/etc/hostname']),
                ]
                for name, extra, payload in failures:
                    for baseline in [True, False]:
                        p, duration = invoke(sandbox.id, proxy, baseline, fmt, extra, payload)
                        case = {'route': route, 'format': fmt, 'scenario': name,
                            'implementation': 'baseline' if baseline else 'fixed',
                            'exit_code': p.returncode, 'seconds': duration,
                            'stdout': p.stdout, 'stderr': p.stderr}
                        record(case)
                        if name == 'early-missing-cwd':
                            # Execd validates cwd before emitting init. Both implementations
                            # must preserve this existing API rejection, without success output.
                            assert p.returncode == 1 and not p.stdout, case
                            assert 'working directory does not exist' in p.stderr, case
                        elif baseline:
                            assert p.returncode == 0, case
                            assert execution_id(p.stdout, fmt), case
                            case['baseline_false_success_reproduced'] = True
                        else:
                            assert p.returncode == 1, case
                            assert not p.stdout, case
                            assert 'before background command startup was confirmed' in p.stderr, case
                        case['passed'] = True
                for eventual_exit in [0, 7]:
                    marker = f'pr2109-{route}-{fmt}-{eventual_exit}'
                    payload = ['sh', '-c', f'sleep 2; printf {marker}; exit {eventual_exit}']
                    p, duration = invoke(sandbox.id, proxy, False, fmt, [], payload)
                    case = {'route': route, 'format': fmt, 'scenario': 'started-later-exit-' + str(eventual_exit),
                        'implementation': 'fixed', 'exit_code': p.returncode,
                        'seconds': duration, 'stdout': p.stdout, 'stderr': p.stderr}
                    record(case)
                    assert p.returncode == 0 and not p.stderr, case
                    ident = execution_id(p.stdout, fmt)
                    state = query.commands.get_command_status(ident)
                    case['running_when_cli_returned'] = state.running
                    assert state.running is True, state
                    deadline = time.monotonic() + 10
                    while state.running and time.monotonic() < deadline:
                        time.sleep(0.1)
                        state = query.commands.get_command_status(ident)
                    assert state.running is False and state.exit_code == eventual_exit, state
                    logs = query.commands.get_background_command_logs(ident)
                    assert marker in logs.content, logs
                    case['eventual_exit_code'] = state.exit_code
                    case['log_marker_observed'] = True
                    case['passed'] = True
            for eventual_exit in [0, 7]:
                p, duration = invoke(sandbox.id, proxy, False, 'raw', [],
                    ['sh', '-c', f'printf pr2109-foreground; exit {eventual_exit}'], background=False)
                case = {'route': route, 'scenario': 'foreground-exit-' + str(eventual_exit),
                    'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
                record(case)
                assert p.returncode == (1 if eventual_exit else 0), case
                assert 'pr2109-foreground' in p.stdout, case
                case['passed'] = True
        finally:
            query.close()
    report['passed'] = True
finally:
    try:
        sandbox.destroy()
        report['sandbox_destroyed'] = True
    finally:
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
print('Live validation completed: ' + str(len(results)) + ' cases', flush=True)
