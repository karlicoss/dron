#!/usr/bin/env python3
import argparse
import platform
import shlex
from subprocess import Popen, PIPE, STDOUT
import sys
from typing import Iterator


IS_SYSTEMD = platform.system() != 'Darwin'  # if not systemd it's launchd


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument('--job', required=True)
    p.add_argument('--stdin', action='store_true')
    return p


def get_stdin() -> Iterator[bytes]:
    yield from sys.stdin.buffer


def get_last_systemd_log(job: str) -> Iterator[bytes]:
    inf = '1000000'
    cmd = ['systemctl', '--user', 'status', '--no-pager', '--lines', inf, job, '-o', 'cat']
    yield ' '.join(map(shlex.quote, cmd)).encode('utf8') + b'\n\n'
    with Popen(cmd, stdout=PIPE, stderr=STDOUT) as po:
        out = po.stdout
        assert out is not None
        yield from out
    rc = po.poll()
    assert rc in {
        0,
        3,  # 3 means failure due to job exit code
    }, rc
