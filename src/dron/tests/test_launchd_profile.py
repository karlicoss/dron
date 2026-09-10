from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from ..launchd import plist


@pytest.mark.parametrize('load_profile', [False, True])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_profile_environment(tmp_path: Path, *, load_profile: bool, exit_code: int) -> None:
    user_home = tmp_path / 'user home'
    user_home.mkdir()
    (user_home / '.profile').write_text(
        'export DRON_PROFILE_VALUE="from profile"\n'
        'export PYTHONPYCACHEPREFIX="$HOME/python cache"\n'
        'export PATH="$HOME/bin:$PATH"\n'
    )
    (user_home / '.bash_profile').write_text('exit 42\n')

    probe = tmp_path / 'probe.py'
    probe.write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        'result = {"value": os.environ["DRON_PROFILE_VALUE"], "path": os.environ["PATH"],\n'
        '          "pycache": sys.pycache_prefix, "args": sys.argv[3:]}\n'
        'if "--stdin" in sys.argv:\n'
        '    result["stdin"] = sys.stdin.read()\n'
        'Path(sys.argv[1]).write_text(json.dumps(result))\n'
        'sys.exit(int(sys.argv[2]))\n'
    )
    job_result = tmp_path / 'job.json'
    notification_result = tmp_path / 'notification.json'
    arguments = ['one & two', '$(touch unexpected)', "a'quote", 'two\nlines', '']
    body = plist(
        unit_name='profile-test',
        command=[sys.executable, str(probe), str(job_result), str(exit_code), *arguments],
        on_failure=[shlex.join([sys.executable, str(probe), str(notification_result), '0'])],
        load_profile=load_profile,
    )
    launcher = plistlib.loads(body.encode())['ProgramArguments']
    result = subprocess.run(
        launcher,
        env={'HOME': str(user_home), 'PATH': os.defpath, 'DRON_PROFILE_VALUE': 'inherited'},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr
    assert not (tmp_path / 'unexpected').exists()

    def read_result(path: Path) -> dict:
        data = json.loads(path.read_text())
        assert data['value'] == ('from profile' if load_profile else 'inherited')
        assert data['path'] == (f'{user_home}/bin:{os.defpath}' if load_profile else os.defpath)
        assert data['pycache'] == (str(user_home / 'python cache') if load_profile else None)
        return data

    assert read_result(job_result)['args'] == arguments
    if exit_code != 0:
        notification = read_result(notification_result)
        assert f'exit code: {exit_code}' in notification['stdin']
    else:
        assert not notification_result.exists()


@pytest.mark.parametrize(
    ('profile', 'exit_codes'),
    [
        pytest.param(None, (1,), id='missing'),
        pytest.param('return 9\n', (9,), id='return'),
        pytest.param('exit 9\n', (9,), id='exit'),
        pytest.param('set -e\nfalse\ntrue\n', (1,), id='errexit'),
        # Bash may return 1 or 2 for this syntax error, depending on its version.
        pytest.param('if\n', (1, 2), id='syntax-error'),
    ],
)
def test_profile_failure_stops_job_but_not_notifications(
    tmp_path: Path, *, profile: str | None, exit_codes: tuple[int, ...]
) -> None:
    if profile is not None:
        (tmp_path / '.profile').write_text('export DRON_PROFILE_VALUE="before failure"\n' + profile)
    notified = tmp_path / 'notification.jsonl'
    notifier = tmp_path / 'notifier.py'
    notifier.write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        'record = {"value": os.environ["DRON_PROFILE_VALUE"], "stdin": sys.stdin.read()}\n'
        'with Path(sys.argv[1]).open("a") as output:\n'
        '    output.write(json.dumps(record) + "\\n")\n'
        'sys.exit(3)\n'
    )
    started = tmp_path / 'job-started'
    body = plist(
        unit_name='profile-failure',
        command=[sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(started)],
        on_failure=[shlex.join([sys.executable, '-B', str(notifier), str(notified)])],
        load_profile=True,
    )
    result = subprocess.run(
        plistlib.loads(body.encode())['ProgramArguments'],
        env={'HOME': str(tmp_path), 'PATH': os.defpath, 'DRON_PROFILE_VALUE': 'inherited'},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode in exit_codes, result.stderr
    assert not started.exists()
    [notification] = notified.read_text().splitlines()
    record = json.loads(notification)
    assert record['value'] == ('inherited' if profile is None else 'before failure')
    assert record['stdin'].startswith(f'exit code: {result.returncode}\n')
    assert 'notification failed:' in result.stderr
    assert '(exit code: 3)' in result.stderr
