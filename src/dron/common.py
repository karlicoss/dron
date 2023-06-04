import argparse
from dataclasses import dataclass
from pathlib import Path
import shlex
import sys
from typing import NamedTuple, Iterable, Optional, Sequence, Union, Dict, Any


# TODO can remove this? although might be useful for tests
VERIFY_UNITS = True
# TODO ugh. verify tries using already installed unit files so if they were bad, everything would fail
# I guess could do two stages, i.e. units first, then timers
# dunno, a bit less atomic though...

class VerifyOff(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        global VERIFY_UNITS
        VERIFY_UNITS = False


class MonParams(NamedTuple):
    with_success_rate: bool
    with_command: bool


Unit = str
Body = str
UnitFile = Path


@dataclass
class UnitState:
    unit_file: UnitFile
    body: Optional[Body]


@dataclass
class LaunchdUnitState(UnitState):
    cmdline: Sequence[str]
    # NOTE: can legit be str (e.g. if unit was never ran before)
    last_exit_code: str
    pid: Optional[str]
    schedule: Optional[str]


State = Iterable[UnitState]


from .logging import LazyLogger
logger = LazyLogger('dron')


import platform
IS_SYSTEMD = platform.system() != 'Darwin'  # if not systemd it's launchd


from typing import TypeVar
T = TypeVar('T')
def unwrap(x: Optional[T]) -> T:
    assert x is not None
    return x


PathIsh = Union[str, Path]

# if it's an str, assume it's already escaped
# otherwise we are responsible for escaping..
Command = Union[PathIsh, Sequence[PathIsh]]


OnCalendar = str
TimerSpec = Dict[str, str] # meh # TODO why is it a dict???
ALWAYS = 'always'
When = Union[OnCalendar, TimerSpec]


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
    pytest_fixture = lambda f: f # no-op otherwise to prevent pytest import


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
    bin = Path('/bin/bash')
    assert wrap(bin, "-c 'echo whatever'") == "/bin/bash -c 'echo whatever'"
    assert wrap(bin, ['echo', bin]) == "/bin/bash echo /bin/bash"
    assert wrap('cat', bin) == "cat /bin/bash"



class MonitorEntry(NamedTuple):
    unit: str
    status: str
    left: str
    next: str
    schedule: str
    command: Optional[str]
    pid: Optional[str]
    status_ok: bool


def print_monitor(entries: Iterable[MonitorEntry]) -> None:
    entries = list(sorted(
        entries,
        key=lambda e: (e.pid is None, e.status_ok, e),
    ))

    import termcolor

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
        e = e._replace(
            status=termcolor.colored(e.status, 'green' if e.status_ok else 'red'),
        )
        if e.pid is not None:
            e = e._replace(
                next=termcolor.colored('running', 'yellow'),
                left='--',
            )
        items.append(e[:len(headers)])
    print(tabulate.tabulate(items, headers=headers))
