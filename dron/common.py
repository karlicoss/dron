from pathlib import Path
from typing import NamedTuple, Iterable, Tuple, Optional, Sequence, Union, Dict
from dataclasses import dataclass


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
    pid: str # optional?
    schedule: str # optional?


State = Iterable[UnitState]


try:
    from kython.klogging2 import LazyLogger # type: ignore
except ImportError:
    import logging
    logger = logging.getLogger('dron')
else:
    logger = LazyLogger('dron', level='info')



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
When = Union[OnCalendar, TimerSpec]
