#!/usr/bin/env python3
import shlex
import socket
from subprocess import Popen, PIPE, STDOUT
from typing import Iterator

from .common import get_parser


def send_payload(payload: Iterator[bytes]) -> None:
    with Popen(['sendmail', '-t'], stdin=PIPE) as po:
        stdin = po.stdin
        assert stdin is not None
        for line in payload:
            stdin.write(line)
        stdin.flush()
    rc = po.poll()
    assert rc == 0, rc


def get_last_log(job: str) -> Iterator[bytes]:
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


def send_email(*, to: str, job: str) -> None:
    def payload() -> Iterator[bytes]:
        hostname = socket.gethostname()
        yield f'''
To: {to}
From: dron <root@{hostname}>
Subject: {job}
Content-Transfer-Encoding: 8bit
Content-Type: text/plain; charset=UTF-8
'''.lstrip().encode('utf8')
        yield from get_last_log(job)

    send_payload(payload())


def main() -> None:
    p = get_parser()
    p.add_argument('--to', required=True)
    args = p.parse_args()
    send_email(to=args.to, job=args.job)


if __name__ == '__main__':
    main()
