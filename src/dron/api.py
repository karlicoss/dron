from __future__ import annotations

import getpass
import sys
from typing import NamedTuple, Sequence

from .common import (
    IS_SYSTEMD,
    Command,
    OnCalendar,
    When,
    wrap,
)

OnFailureAction = str


class Job(NamedTuple):
    when: When | None
    command: Command
    unit_name: str
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


def job(when: When | None, command: Command, *, unit_name: str, on_failure: Sequence[OnFailureAction]=(notify.email_local,), **kwargs) -> Job:
    assert 'extra_email' not in kwargs, unit_name  # deprecated

    """
    when: if None, then timer won't be created (still allows running job manually)
    """
    # TODO later, autogenerate unit name
    # I guess warn user about non-unique names and prompt to give a more specific name?
    return Job(
        when=when,
        command=command,
        unit_name=unit_name,
        on_failure=on_failure,
        kwargs=kwargs,
    )


__all__ = (
    'When',
    'OnCalendar',
    'OnFailureAction',
    'Command', 'wrap',
    'job',
    'notify',

    'Job',  # todo maybe don't expose it?
)
