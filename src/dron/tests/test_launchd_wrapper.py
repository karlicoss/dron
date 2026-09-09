from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ..launchd import launchd_wrapper


def test_invalid_utf8_output(tmp_path: Path) -> None:
    output = b'hello \xe2\x82\xac\xff\ntruncated \xe2'
    job = tmp_path / 'job.py'
    job.write_text(f'import sys\nsys.stdout.buffer.write({output!r})\nsys.exit(7)\n')
    notification = tmp_path / 'notification.txt'
    notifier = tmp_path / 'notifier.py'
    notifier.write_text(
        'import sys\n'
        'from pathlib import Path\n'
        'payload = sys.stdin.buffer.read()\n'
        'payload.decode("utf8")\n'
        'Path(sys.argv[1]).write_bytes(payload)\n'
        'sys.stdout.buffer.write(b"notifier stdout: \\xff\\n")\n'
        'sys.stderr.buffer.write(b"notifier stderr: \\xfe\\n")\n'
    )
    command = launchd_wrapper(
        job='utf8-test',
        on_failure=[shlex.join([sys.executable, '-B', str(notifier), str(notification)])],
    )
    result = subprocess.run(
        [*command, sys.executable, '-B', str(job)],
        env={'HOME': str(tmp_path), 'PATH': os.defpath},
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 7, result.stderr
    assert result.stdout == output
    payload = notification.read_text()
    assert payload.startswith('exit code: 7\n')
    assert payload.endswith('hello €�\ntruncated �')
    logged = (tmp_path / 'Library/Logs/dron/utf8-test.log').read_text()
    assert 'hello €�' in logged
    assert 'truncated �' in logged
    assert 'notifier stdout: �' in logged
    assert 'notifier stderr: �' in logged
    assert b'Traceback' not in result.stderr


@pytest.mark.parametrize('notifier_exit_code', [0, 3])
def test_notification_pipes_do_not_deadlock(tmp_path: Path, *, notifier_exit_code: int) -> None:
    output = b'x' * (128 * 1024)
    job = tmp_path / 'job.py'
    job.write_text(f'import sys\nsys.stdout.buffer.write({output!r})\nsys.exit(7)\n')
    notifier = tmp_path / 'notifier.py'
    # Fill stdout and stderr before reading the large notification from stdin.
    notifier.write_text(
        'import sys\n'
        'from pathlib import Path\n'
        'sys.stdout.buffer.write(b"y" * 524288)\n'
        'sys.stdout.buffer.flush()\n'
        'sys.stderr.buffer.write(b"z" * 524288)\n'
        'sys.stderr.buffer.flush()\n'
        'Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\n'
        'sys.exit(int(sys.argv[2]))\n'
    )
    first = tmp_path / 'first.txt'
    second = tmp_path / 'second.txt'
    command = launchd_wrapper(
        job='notification-pipes-test',
        on_failure=[
            shlex.join([sys.executable, '-B', str(notifier), str(destination), str(exit_code)])
            for destination, exit_code in [(first, notifier_exit_code), (second, 0)]
        ],
    )
    with subprocess.Popen(
        [*command, sys.executable, '-B', str(job)],
        env={'HOME': str(tmp_path), 'PATH': os.defpath},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    assert process.returncode == 7, stderr
    assert stdout == output
    payload = first.read_bytes()
    assert second.read_bytes() == payload
    assert payload.startswith(b'exit code: 7\n')
    assert payload.endswith(output)
    assert (b'notification failed:' in stderr) == (notifier_exit_code != 0)
