#!/usr/bin/env python3
"""
uses https://github.com/dschep/ntfy
"""
import subprocess
import logging
import socket


def run_ntfy(*, job: str, backend: str) -> None:
    # TODO not sure what to do with --stdin arg here?
    # could probably use last N lines of log or something
    # TODO get last logs here?
    title = f'dron[{socket.gethostname()}]: {job} failed'
    body = title
    try:
        subprocess.check_call(['ntfy', '-b', backend, '-t', title, 'send', body])
    except Exception as e:
        logging.exception(e)
        # TODO fallback on email