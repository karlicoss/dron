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


def _with_profile(command: list[str], *, allow_profile_failure: bool) -> list[str]:
    # Source .profile explicitly so .bash_profile cannot shadow it.
    if allow_profile_failure:
        # An EXIT trap still attempts notification if .profile calls exit or enables errexit.
        # exec replaces the shell, so a failed notifier is not retried.
        script = 'trap \'exec "$@"\' EXIT; . "$HOME/.profile"'
    else:
        # Testing the source command with && or if would suppress errexit inside .profile.
        script = (
            '. "$HOME/.profile"\n'
            'dron_profile_status=$?\n'
            '[ "$dron_profile_status" -eq 0 ] || exit "$dron_profile_status"\n'
            'exec "$@"'
        )
    return ['/bin/bash', '--noprofile', '--norc', '-c', script, 'dron', *command]


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

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f'{job}.log'

    logger.remove()
    # Job output is already forwarded to stdout, so only mirror wrapper diagnostics to stderr.
    logger.add(sys.stderr, filter=lambda record: record['extra'].get('job_output') is not True)
    # TODO add retention so old rotated files are eventually deleted.
    logger.add(log_file, rotation='100 MB')  # todo configurable? or rely on osx rotation?
    output_logger = logger.bind(job_output=True)

    # hmm, a bit crap transforming everything to stdout? but not much we can do?
    captured_log = []
    command = _with_profile(cmd, allow_profile_failure=False) if args.load_profile else cmd
    try:
        po = Popen(command, stdout=PIPE, stderr=STDOUT)
    except Exception as e:
        # Popen itself can fail, for example due to permission errors.
        logger.exception(e)
        captured_log.append(str(e).encode('utf8'))
        rc = 123
    else:
        with po:
            out = po.stdout
            assert out is not None
            for line in out:
                captured_log.append(line)
                output_logger.info(line.decode('utf8', errors='replace').removesuffix('\n'))
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        rc = po.returncode

    assert rc is not None
    if rc == 0:
        # short circuit
        sys.exit(0)

    logger.error(f'exit code: {rc}; command: {shlex.join(cmd)}')

    def payload() -> Iterator[bytes]:
        yield f"exit code: {rc}\n".encode()
        yield b'command: \n'
        yield (' '.join(map(shlex.quote, cmd)) + '\n').encode('utf8')
        yield f'log file: {log_file}\n'.encode()
        yield b'\n'
        yield b'output (stdout + stderr):\n\n'
        # TODO shit -- if multiple notifications, can't use generator for captured_log
        # unless we notify simultaneously?
        # Replace invalid bytes in the notification payload.
        for line in captured_log:
            yield line.decode('utf8', errors='replace').encode()

    notification_input = b''.join(payload())
    for notify_cmd in notify_cmds:
        logger.info(f'notifying: {notify_cmd}')
        command = ['/bin/sh', '-c', notify_cmd]
        if args.load_profile:
            # A broken profile must not prevent us from reporting the job failure it caused.
            command = _with_profile(command, allow_profile_failure=True)
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
