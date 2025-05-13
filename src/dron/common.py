from __future__ import annotations

import platform
import shlex
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from loguru import logger  # noqa: F401

datetime_aware = datetime
datetime_naive = datetime


# TODO can remove this? although might be useful for tests
VERIFY_UNITS = True
# TODO ugh. verify tries using already installed unit files so if they were bad, everything would fail
# I guess could do two stages, i.e. units first, then timers
# dunno, a bit less atomic though...


def set_verify_off() -> None:
    global VERIFY_UNITS
    VERIFY_UNITS = False


@dataclass
class MonitorParams:
    with_success_rate: bool
    with_command: bool


Unit = str
Body = str
UnitFile = Path


@dataclass
class UnitState:
    unit_file: UnitFile
    body: Body | None
    cmdline: Sequence[str] | None  # can be None for timers


@dataclass
class SystemdUnitState(UnitState):
    dbus_properties: Any  # seems like keeping this around massively speeds up dbus access...


@dataclass
class LaunchdUnitState(UnitState):
    # NOTE: can legit be str (e.g. if unit was never ran before)
    last_exit_code: str | None
    pid: str | None
    schedule: str | None


State = Iterable[UnitState]


IS_SYSTEMD = platform.system() != 'Darwin'  # if not systemd it's launchd


T = TypeVar('T')


def unwrap(x: T | None) -> T:
    assert x is not None
    return x


PathIsh = str | Path

# if it's an str, assume it's already escaped
# otherwise we are responsible for escaping..
Command = PathIsh | Sequence[PathIsh]


OnCalendar = str
TimerSpec = dict[str, str]  # meh # TODO why is it a dict???
ALWAYS = 'always'
When = OnCalendar | TimerSpec


MANAGED_MARKER = '(MANAGED BY DRON)'


def is_managed(body: str) -> bool:
    # switching off it because it's unfriendly to launchd
    legacy_marker = '<MANAGED BY DRON>'
    return MANAGED_MARKER in body or legacy_marker in body


pytest_fixture: Any
under_pytest = 'pytest' in sys.modules
if under_pytest:
    import pytest

    pytest_fixture = pytest.fixture
else:
    pytest_fixture = lambda f: f  # no-op otherwise to prevent pytest import


Escaped = str


def escape(command: Command) -> Escaped:
    if isinstance(command, Escaped):
        return command
    elif isinstance(command, Path):
        return escape([command])
    else:
        return ' '.join(shlex.quote(str(part)) for part in command)


def wrap(script: PathIsh, command: Command) -> Escaped:
    return shlex.quote(str(script)) + ' ' + escape(command)


def test_wrap() -> None:
    assert wrap('/bin/bash', ['-c', 'echo whatever']) == "/bin/bash -c 'echo whatever'"
    bin_ = Path('/bin/bash')
    assert wrap(bin_, "-c 'echo whatever'") == "/bin/bash -c 'echo whatever'"
    assert wrap(bin_, ['echo', bin_]) == "/bin/bash echo /bin/bash"
    assert wrap('cat', bin_) == "cat /bin/bash"


@dataclass(order=True)
class MonitorEntry:
    unit: str
    status: str
    left: str
    next: str
    schedule: str
    command: str | None
    pid: str | None

    """
    'status' is coming from systemd/launchd, and it's a string.

    So status_ok should be used instead if you actually want to rely on something robust.
    """
    status_ok: bool


def print_monitor(entries: Iterable[MonitorEntry]) -> None:
    entries = sorted(
        entries,
        key=lambda e: (e.pid is None, e.status_ok, e),
    )

    import termcolor  # noqa: I001
    import tabulate

    tabulate.PRESERVE_WHITESPACE = True

    headers = [
        'UNIT',
        'STATUS',
        'LEFT',
        'NEXT',
        'SCHEDULE',
    ]
    with_command = any(x.command is not None for x in entries)
    if with_command:
        headers.append('COMMAND')

    items = []
    for e in entries:
        e = replace(
            e,
            status=termcolor.colored(e.status, 'green' if e.status_ok else 'red'),
        )
        if e.pid is not None:
            e = replace(
                e,
                next=termcolor.colored('running', 'yellow'),
                left='--',
            )
        items.append(list(asdict(e).values())[: len(headers)])
    print(tabulate.tabulate(items, headers=headers))
