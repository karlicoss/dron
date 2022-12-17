from typing import NamedTuple, Optional

from .common import (
    Command,
    When, OnCalendar,
    wrap,
)


class Job(NamedTuple):
    when: Optional[When]
    command: Command
    unit_name: str
    extra_email: Optional[str]
    kwargs: dict[str, str]


# TODO think about arg names?
# TODO not sure if should give it default often?
# TODO when first? so it's more compat to crontab..
def job(when: Optional[When], command: Command, *, unit_name: Optional[str]=None, extra_email: Optional[str]=None, **kwargs) -> Job:
    """
    when: if None, then timer won't be created (still allows running job manually)

    """
    assert unit_name is not None
    # TODO later, autogenerate unit name
    # I guess warn user about non-unique names and prompt to give a more specific name?
    return Job(
        when=when,
        command=command,
        unit_name=unit_name,
        extra_email=extra_email,
        kwargs=kwargs,
    )


__all__ = (
    'Job',
    'job',
    'Command',
    'OnCalendar',
    'wrap',
)
