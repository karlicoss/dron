#!/usr/bin/env python3
import getpass
import logging
import shlex
import socket
import sys
from subprocess import PIPE, Popen, STDOUT
from typing import NoReturn, Iterator


def send(payload: Iterator[bytes]) -> None:
    # TODO switch systemd helper to popen/pipe version as well?
    with Popen(['sendmail', '-t'], stdin=PIPE) as po:
        stdin = po.stdin
        assert stdin is not None
        for line in payload:
            stdin.write(line)
        stdin.flush()
    rc = po.poll()
    assert rc == 0, rc


def main() -> NoReturn:
    unit = sys.argv[1]
    cmd = sys.argv[2:]
    # hmm, a bit crap transforming everything to stdout? but not much we can do?
    captured_log = []
    try:
        with Popen(cmd, stdout=PIPE, stderr=STDOUT) as po:
            out = po.stdout
            assert out is not None
            for line in out:
                captured_log.append(line)
                sys.stdout.buffer.write(line)
        rc = po.poll()

        if rc == 0:
            # short circuit
            sys.exit(0)
    except Exception as e:
        # Popen istelf still fail due to permission denied or something
        logging.exception(e)
        captured_log.append(str(e).encode('utf8'))
        rc = 123

    to = getpass.getuser()
    def payload() -> Iterator[bytes]:
        hostname = socket.gethostname()
        yield f'''
To: {to}
From: dron <root@{hostname}>
Subject: {unit}
Content-Transfer-Encoding: 8bit
Content-Type: text/plain; charset=UTF-8
'''.lstrip().encode('utf8')

        yield f"exit code: {rc}\n".encode('utf8')
        yield b'command: \n'
        yield (' '.join(map(shlex.quote, cmd)) + '\n').encode('utf8')
        yield b'\n'
        yield b'output (stdout + stderr):\n\n'
        yield from captured_log
    try:
        send(payload())
    except Exception as e:
        # need to keep defensive
        logging.error("FAILED TO SEND EMAIL!")
        logging.exception(e)

    sys.exit(rc)

if __name__ == '__main__':
    main()
