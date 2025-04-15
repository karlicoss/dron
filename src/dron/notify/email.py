# noqa: A005
import socket
from collections.abc import Iterator
from subprocess import PIPE, Popen

from .common import get_last_systemd_log, get_parser, get_stdin


def send_payload(payload: Iterator[bytes]) -> None:
    with Popen(['sendmail', '-t'], stdin=PIPE) as po:
        stdin = po.stdin
        assert stdin is not None
        for line in payload:
            stdin.write(line)
        stdin.flush()
    rc = po.poll()
    assert rc == 0, rc


def send_email(*, to: str, job: str, stdin: bool) -> None:
    def payload() -> Iterator[bytes]:
        hostname = socket.gethostname()
        yield f'''
To: {to}
From: dron <root@{hostname}>
Subject: {job}
Content-Transfer-Encoding: 8bit
Content-Type: text/plain; charset=UTF-8
'''.lstrip().encode('utf8')
        last_log = get_stdin() if stdin else get_last_systemd_log(job)
        yield from last_log

    send_payload(payload())


def main() -> None:
    p = get_parser()
    p.add_argument('--to', required=True)
    args = p.parse_args()
    send_email(to=args.to, job=args.job, stdin=args.stdin)


if __name__ == '__main__':
    main()
