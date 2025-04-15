from __future__ import annotations

import getpass
import inspect
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from .common import (
    IS_SYSTEMD,
    Command,
    OnCalendar,
    When,
    wrap,
)

OnFailureAction = str

UnitName = str


@dataclass
class Job:
    when: When | None
    command: Command
    unit_name: UnitName
    on_failure: Sequence[OnFailureAction]
    kwargs: dict[str, str]


# staticmethod isn't callable directly prior to 3.10
def _email(to: str) -> str:
    return f'{sys.executable} -m dron.notify.email --job %n --to {to}'


class notify:
    @staticmethod
    def email(to: str) -> str:
        return _email(to)

    email_local = _email(to='%u' if IS_SYSTEMD else getpass.getuser())

    # TODO adapt to macos
    desktop_notification = f'{sys.executable} -m dron.notify.ntfy_desktop --job %n'

    telegram = f'{sys.executable} -m dron.notify.telegram --job %n'


def job(
    when: When | None,
    command: Command,
    *,
    unit_name: str | None = None,
    on_failure: Sequence[OnFailureAction] = (notify.email_local,),
    **kwargs,
) -> Job:
    """
    when: if None, then timer won't be created (still allows running job manually)
    unit_name: if None, then will attempt to guess from source code (experimental!)
    """
    assert 'extra_email' not in kwargs, unit_name  # deprecated

    stacklevel: int = kwargs.pop('stacklevel', 1)

    def guess_name() -> str | Exception:
        stack = inspect.stack()
        frame = stack[stacklevel + 1]  # +1 for guess_name itself
        code_context_lines = frame.code_context
        # python should alway keep single line for code context? but just in case
        if code_context_lines is None or len(code_context_lines) != 1:
            return RuntimeError(f"Expected single code context line, got {code_context_lines=}")
        [code_context] = code_context_lines
        code_context = code_context.strip()
        rgx = r'(\w+)\s+='
        m = re.match(rgx, code_context)  # find assignment to variable
        if m is None:
            return RuntimeError(f"Couldn't guess from {code_context=} (regex {rgx=})")
        return m.group(1)

    if unit_name is None:
        guessed_name = guess_name()

        if isinstance(guessed_name, Exception):
            raise RuntimeError(f"{when} {command}: couldn't guess job name: {guessed_name}")

        unit_name = guessed_name

    return Job(
        when=when,
        command=command,
        unit_name=unit_name,
        on_failure=on_failure,
        kwargs=kwargs,
    )


__all__ = (
    'Command',
    'Job',  # todo maybe don't expose it?
    'OnCalendar',
    'OnFailureAction',
    'When',
    'job',
    'notify',
    'wrap',
)
