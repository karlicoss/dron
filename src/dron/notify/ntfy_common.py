"""
uses https://github.com/dschep/ntfy
"""

import logging
import socket
import subprocess
import sys
from typing import NoReturn


# ty doesn't support NoReturn yet, see https://github.com/astral-sh/ty/issues/180
def run_ntfy(*, job: str, backend: str) -> NoReturn:  # ty: ignore[invalid-return-type]
    # TODO not sure what to do with --stdin arg here?
    # could probably use last N lines of log or something
    # TODO get last logs here?
    title = f'dron[{socket.gethostname()}]: {job} failed'
    body = title
    try:
        subprocess.check_call(['ntfy', '-b', backend, '-t', title, 'send', body])
    except Exception as e:
        logging.exception(e)
        # TODO fallback on email?
        sys.exit(1)
    sys.exit(0)
