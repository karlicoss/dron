import argparse
import platform
import shlex
import sys
from collections.abc import Iterator
from subprocess import PIPE, STDOUT, Popen, check_output

IS_SYSTEMD = platform.system() != 'Darwin'  # if not systemd it's launchd


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    p.add_argument('--stdin', action='store_true')
    return p


def get_stdin() -> Iterator[bytes]:
    yield from sys.stdin.buffer


def get_last_systemd_log(job: str) -> Iterator[bytes]:
    # output unit status
    cmd = ['systemctl', '--user', 'status', '--no-pager', job, '-o', 'cat']
    yield b'$ ' + ' '.join(map(shlex.quote, cmd)).encode('utf8') + b'\n\n'
    with Popen(cmd, stdout=PIPE, stderr=STDOUT) as po:
        out = po.stdout
        assert out is not None
        yield from out
    rc = po.poll()
    assert rc in {
        0,
        3,  # 3 means failure due to job exit code
    }, rc

    # for logs, we used to use --lines 1000000 in systemctl status
    # however, from around 2024 it stated consuming too much time
    # (as if it actually retrieved 1000000 lines and only then tooks the ones relevant to the unit??)

    cmd = ['systemctl', '--user', 'show', job, '-p', 'InvocationID', '--value']
    invocation_id = check_output(cmd, text=True)
    invocation_id = invocation_id.strip()  # for some reason dumps multiple lines?
    assert len(invocation_id) > 0  # just in case, todo maybe make defensive?

    yield b'\n'
    cmd = ['journalctl', '--no-pager', f'_SYSTEMD_INVOCATION_ID={invocation_id}']
    yield b'$ ' + ' '.join(map(shlex.quote, cmd)).encode('utf8') + b'\n\n'
    with Popen(cmd, stdout=PIPE, stderr=STDOUT) as po:
        out = po.stdout
        assert out is not None
        yield from out
    rc = po.poll()
    assert rc == 0
