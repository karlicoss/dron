from typing import NamedTuple, Optional, Sequence

from .common import (
    Command,
    When, OnCalendar,
    wrap,
)


OnFailureAction = str


class Job(NamedTuple):
    when: Optional[When]
    command: Command
    unit_name: str
    on_failure: Sequence[OnFailureAction]
    kwargs: dict[str, str]


# staticmethod isn't callable directly prior to 3.10
def _email(to: str) -> str:
    return f'python3 -m dron.notify.email --job %n --to {to}'


class notify:
    @staticmethod
    def email(to: str) -> str:
        return _email(to)

    # TODO adapt to macos
    email_local = _email(to='%u')

    # TODO adapt to macos
    desktop_notification = 'python3 -m dron.notify.ntfy_linux --job %n'

    telegram = 'python3 -m dron.notify.ntfy_telegram --job %n'


def job(when: Optional[When], command: Command, *, unit_name: str, on_failure: Sequence[OnFailureAction]=(notify.email_local,), **kwargs) -> Job:
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
