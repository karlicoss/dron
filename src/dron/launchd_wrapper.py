#!/usr/bin/env python3
import argparse
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from typing import NoReturn

from loguru import logger

# Standard macOS log directory, also the default for platformdirs.user_log_path('dron').
# Keep the path explicit to avoid adding a dependency to this macOS-only wrapper.
LOG_DIR = Path('~/Library/Logs/dron').expanduser()


def main() -> NoReturn:
    p = argparse.ArgumentParser()
    p.add_argument('--notify', action='append')
    p.add_argument('--job', required=True)
    p.add_argument(
        '--load-profile', action='store_true', help='Source ~/.profile for the job and failure notifications'
    )
    # hmm, this doesn't work with keyword args??
    # p.add_argument('cmd', nargs=argparse.REMAINDER)
    args, rest = p.parse_known_args()

    assert rest[0] == '--', rest
    cmd = rest[1:]

    notify_cmds = [] if args.notify is None else args.notify
    job = args.job

    prefix: list[str] = []
    if args.load_profile:
        # Source .profile explicitly so .bash_profile cannot shadow it.
        prefix = [
            '/bin/bash', '--noprofile', '--norc', '-c',
            '. "$HOME/.profile" && exec "$@"',
            'dron',
        ]  # fmt: skip

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f'{job}.log'

    logger.add(log_file, rotation='100 MB')  # todo configurable? or rely on osx rotation?

    # hmm, a bit crap transforming everything to stdout? but not much we can do?
    captured_log = []
    try:
        with Popen([*prefix, *cmd], stdout=PIPE, stderr=STDOUT) as po:
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
        yield f"exit code: {rc}\n".encode()
        yield b'command: \n'
        yield (' '.join(map(shlex.quote, cmd)) + '\n').encode('utf8')
        yield f'log file: {log_file}\n'.encode()
        yield b'\n'
        yield b'output (stdout + stderr):\n\n'
        # TODO shit -- if multiple notifications, can't use generator for captured_log
        # unless we notify simultaneously?
        # Replace invalid bytes for text logs and notifications, while keeping stdout unchanged.
        for line in captured_log:
            yield line.decode('utf8', errors='replace').encode()

    for line in payload():
        logger.info(line.decode('utf8').rstrip('\n'))  # meh

    notification_input = b''.join(payload())
    for notify_cmd in notify_cmds:
        logger.info(f'notifying: {notify_cmd}')
        command = [*prefix, '/bin/sh', '-c', notify_cmd]
        try:
            with Popen(command, stdin=PIPE, stdout=PIPE, stderr=PIPE) as po:
                # Drain stdout and stderr while writing stdin so a noisy notifier cannot deadlock.
                sout, serr = po.communicate(input=notification_input)
        except Exception as e:
            logger.error(f'notification failed: {notify_cmd}')
            logger.exception(e)
            continue

        for line in sout.decode('utf8', errors='replace').splitlines():
            logger.debug(line)
        for line in serr.decode('utf8', errors='replace').splitlines():
            logger.debug(line)
        if po.returncode != 0:
            logger.error(f'notification failed: {notify_cmd} (exit code: {po.returncode})')

    sys.exit(rc)


if __name__ == '__main__':
    main()
