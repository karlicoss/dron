"""
uses telegram-send for Telegram notifications
make sure to run "telegram-send --configure" beforehand!
"""
import asyncio
import logging
import socket
import sys

from .common import get_parser, get_last_systemd_log, get_stdin


def send(*, message: str) -> None:
    import telegram_send  # type: ignore[import-untyped]

    asyncio.run(telegram_send.send(messages=[message]))


def main() -> None:
    p = get_parser()
    args = p.parse_args()

    job: str = args.job
    stdin: bool = args.stdin

    body = f'dron[{socket.gethostname()}]: {job} failed'

    last_log = get_stdin() if stdin else get_last_systemd_log(job)
    body += '\n' + '\n'.join(l.decode('utf8') for l in last_log)

    try:
        send(message=body)
    except Exception as e:
        logging.exception(e)
        # TODO fallback on email?
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
