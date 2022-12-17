#!/usr/bin/env python3
# ugh. I just need some variable substitution; why is bash so shit?
import argparse
import os
import io
import socket
from typing import Iterator, Optional
import shlex
from subprocess import Popen, check_call, check_output, run, PIPE


def send(payload: Iterator[bytes]) -> None:
    # todo can we exec into it?
    pl = b'\n'.join(payload)
    r = run(['sendmail', '-t'], input=pl)
    r.check_returncode()


def scu(*args):
    return ['systemctl', '--user', *args]


def payload(*, to: str, unit: str) -> Iterator[bytes]:
    hostname = socket.gethostname()
    # todo From: systemd to From: dron?
    yield f'''
To: {to}
From: systemd <root@{hostname}>
Subject: {unit}
Content-Transfer-Encoding: 8bit
Content-Type: text/plain; charset=UTF-8
'''.lstrip().encode('utf8')

    cmd = scu('status', '--lines', '1000000', unit, '-o', 'cat')
    r = run(cmd, stdout=PIPE, stderr=PIPE)
    # status returns non-zero code if the unit failed, so need to ignore failures..
    # todo check return code?
    yield ' '.join(map(shlex.quote, cmd)).encode('utf8') # meh
    yield r.stdout


def main() -> None:
    from argparse import ArgumentParser
    p = ArgumentParser()
    p.add_argument('--to', required=True)
    p.add_argument('--unit', required=True)
    args = p.parse_args()

    pl = payload(to=args.to, unit=args.unit)
    send(payload=pl)


if __name__ == '__main__':
   main()

# # eh. not very atomic..
# # TODO FIXME make sure that we get some warning if the mailer itself fails?
# # systemctl --user show --value -p InvocationID systemdtab-test.service
# # TODO indicate if it failed?
# # TODO suggest how to inspect logs, include tail or link??
# # TODO ugh!
