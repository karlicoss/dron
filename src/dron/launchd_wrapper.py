#!/usr/bin/env python3
import argparse
from pathlib import Path
import shlex
from subprocess import PIPE, Popen, STDOUT
import sys
from typing import NoReturn, Iterator

from loguru import logger


LOG_DIR = Path('~/Library/Logs/dron').expanduser()


def main() -> NoReturn:
    p = argparse.ArgumentParser()
    p.add_argument('--notify', action='append')
    p.add_argument('--job', required=True)
    # hmm, this doesn't work with keyword args??
    # p.add_argument('cmd', nargs=argparse.REMAINDER)
    args, rest = p.parse_known_args()

    assert rest[0] == '--', rest
    cmd = rest[1:]

    notify_cmds = [] if args.notify is None else args.notify
    job = args.job

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f'{job}.log'

    logger.add(log_file, rotation='100 MB')  # todo configurable? or rely on osx rotation?

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
        logger.exception(e)
        captured_log.append(str(e).encode('utf8'))
        rc = 123


    def payload() -> Iterator[bytes]:
        yield f"exit code: {rc}\n".encode('utf8')
        yield b'command: \n'
        yield (' '.join(map(shlex.quote, cmd)) + '\n').encode('utf8')
        yield f'log file: {log_file}\n'.encode('utf8')
        yield b'\n'
        yield b'output (stdout + stderr):\n\n'
        # TODO shit -- if multiple notifications, can't use generator for captured_log
        # unless we notify simultaneously?
        yield from captured_log

    for line in payload():
        logger.info(line.decode('utf8').rstrip('\n'))  # meh

    for notify_cmd in notify_cmds:
        try:
            with Popen(notify_cmd, shell=True, stdin=PIPE) as po:
                sin = po.stdin
                assert sin is not None
                for line in payload():
                    sin.write(line)
            assert po.poll() == 0, notify_cmd
        except Exception as e:
            logger.error(f'notificaiton failed: {notify_cmd}')
            logger.exception(e)

    sys.exit(rc)


if __name__ == '__main__':
    main()
