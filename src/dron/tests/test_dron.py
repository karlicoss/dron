from __future__ import annotations

import plistlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ..common import UnitState
from ..dron import Add, Delete, Update, _delete_order, compute_plan, load_jobs, load_state, prepare_apply_plan
from ..launchd import _format_calendar_interval, plist


@pytest.fixture
def tmp_pythonpath(tmp_path: Path) -> Iterator[Path]:
    ps = str(tmp_path)
    assert ps not in sys.path  # just in case
    sys.path.insert(0, ps)
    try:
        yield tmp_path
    finally:
        sys.path.remove(ps)


def test_load_jobs_basic(tmp_pythonpath: Path) -> None:
    tpath = Path(tmp_pythonpath) / 'test_drontab.py'
    tpath.write_text(
        '''
from typing import Iterator

from dron.api import job, Job


def jobs() -> Iterator[Job]:
    job3 = job(
        '03:10',
        ['/path/to/command.py', 'some', 'args', '3'],
        unit_name='job3',
    )
    job1 = job(
        '01:10',
        ['/path/to/command.py', 'some', 'args', '1'],
        unit_name='job1',
    )
    yield job1
    yield job(
        '02:10',
        ['/path/to/command.py', 'some', 'args', '2'],
        unit_name='job2',
    )
    yield job3

'''
    )
    loaded = list(load_jobs(tab_module='test_drontab'))
    [job1, job2, job3] = loaded

    assert job1.when == '01:10'
    assert job1.command == ['/path/to/command.py', 'some', 'args', '1']
    assert job1.unit_name == 'job1'

    assert job2.when == '02:10'
    assert job2.command == ['/path/to/command.py', 'some', 'args', '2']
    assert job2.unit_name == 'job2'

    assert job3.when == '03:10'
    assert job3.command == ['/path/to/command.py', 'some', 'args', '3']
    assert job3.unit_name == 'job3'


def test_load_jobs_dupes(tmp_pythonpath: Path) -> None:
    tpath = Path(tmp_pythonpath) / 'test_drontab.py'
    tpath.write_text(
        '''
from typing import Iterator

from dron.api import job, Job

def jobs() -> Iterator[Job]:
    yield job('00:00', 'echo', unit_name='job3')
    yield job('00:00', 'echo', unit_name='job1')
    # whoops! duplicate job name
    yield job('00:00', 'echo', unit_name='job3')
'''
    )
    with pytest.raises(AssertionError):
        _loaded = list(load_jobs(tab_module='test_drontab'))


def test_compute_plan() -> None:
    def unit(name: str, body: str) -> UnitState:
        return UnitState(unit_file=Path('/units') / name, body=body, cmdline=None)

    # fmt: off
    unchanged_current = unit('unchanged.service', 'same')
    changed_current   = unit('changed.service'  , 'old')
    deleted_current   = unit('deleted.service'  , 'deleted')

    unchanged_pending = unit('unchanged.service', 'same')
    changed_pending   = unit('changed.service'  , 'new')
    added_pending     = unit('added.service'    , 'added')
    # fmt: on

    plan = list(
        compute_plan(
            current=[
                unchanged_current,
                changed_current,
                deleted_current,
            ],
            pending=[
                unchanged_pending,
                changed_pending,
                added_pending,
            ],
        )
    )

    assert plan == [
        Delete(unit_file=deleted_current.unit_file),
        Update(
            unit_file=unchanged_current.unit_file,
            old_body='same',
            new_body='same',
        ),
        Update(
            unit_file=changed_current.unit_file,
            old_body='old',
            new_body='new',
        ),
        Add(
            unit_file=added_pending.unit_file,
            body='added',
        ),
    ]


def test_prepare_apply_plan_materializes_pending() -> None:
    def unit(name: str, body: str) -> UnitState:
        return UnitState(unit_file=Path('/units') / name, body=body, cmdline=None)

    current = [unit('changed.service', 'old')]
    pending = (state for state in [unit('changed.service', 'new'), unit('added.service', 'added')])

    plan = prepare_apply_plan(current=current, pending=pending)

    assert plan.pending_units == {'changed.service', 'added.service'}
    assert [update.unit for update, _diff in plan.updates] == ['changed.service']
    assert [add.unit for add in plan.adds] == ['added.service']


def test_delete_order_deletes_timers_before_services() -> None:
    timer = Delete(unit_file=Path('/units/example.timer'))
    service = Delete(unit_file=Path('/units/example.service'))

    assert sorted([service, timer], key=_delete_order) == [timer, service]


def test_launchd_plist_escapes_command_arguments() -> None:
    body = plist(
        unit_name='example',
        command=['/bin/echo', 'one & two', '<three>'],
        on_failure=[],
        when='hourly',
    )

    parsed = plistlib.loads(body.encode())
    assert parsed['ProgramArguments'][-3:] == ['/bin/echo', 'one & two', '<three>']


@pytest.mark.parametrize(
    ('when', 'expected'),
    [
        ('daily'   , {'Hour': 0,  'Minute': 0 }),
        ('hourly'  , {            'Minute': 0 }),
        ('minutely', {                        }),
        ('12:34'   , {'Hour': 12, 'Minute': 34}),
        ('*:0/10'  , [{           'Minute': minute} for minute in range(0, 60, 10)]),
    ],
)  # fmt: skip
def test_launchd_plist_calendar_schedule(when: str, expected: object) -> None:
    body = plist(
        unit_name='example',
        command='/bin/true',
        on_failure=[],
        when=when,
    )

    parsed = plistlib.loads(body.encode())
    assert parsed['StartCalendarInterval'] == expected
    assert 'StartInterval' not in parsed
    assert 'RunAtLoad' not in parsed


@pytest.mark.parametrize(
    ('interval', 'expected'),
    [
        ({'Hour': 0, 'Minute': 0}, '00:00'),
        ({           'Minute': 0}, '*:00' ),
        ({                      }, '*:*'  ),
        ([{'Minute': minute} for minute in range(0, 60, 10)], '*:00,10,20,30,40,50'),
    ],
)  # fmt: skip
def test_format_launchd_calendar_interval(interval: object, expected: str) -> None:
    assert _format_calendar_interval(interval) == expected


@pytest.mark.parametrize(
    ('seconds', 'minutes'),
    [
        (30, 1),
        (61, 2),
        (90, 2),
    ],
)
def test_launchd_plist_rounds_second_schedule_to_minutes(seconds: int, minutes: int) -> None:
    body = plist(
        unit_name='example',
        command='/bin/true',
        on_failure=[],
        when=f'*:*:0/{seconds}',
    )

    parsed = plistlib.loads(body.encode())
    assert parsed['StartCalendarInterval'] == [{'Minute': minute} for minute in range(0, 60, minutes)]
    assert 'StartInterval' not in parsed
    assert 'RunAtLoad' not in parsed


def test_launchd_plist_rejects_second_schedule_over_one_hour() -> None:
    with pytest.raises(AssertionError):
        plist(
            unit_name='example',
            command='/bin/true',
            on_failure=[],
            when='*:*:0/3601',
        )


def test_launchd_plist_always_uses_keepalive() -> None:
    body = plist(
        unit_name='example',
        command='/bin/true',
        on_failure=[],
        when='always',
    )

    parsed = plistlib.loads(body.encode())
    assert parsed['KeepAlive'] is True


def test_launchd_plist_manual_job_has_no_automatic_start() -> None:
    body = plist(
        unit_name='example',
        command='/bin/true',
        on_failure=[],
        when=None,
    )

    parsed = plistlib.loads(body.encode())
    automatic_start_keys = {
        'KeepAlive',
        'RunAtLoad',
        'StartCalendarInterval',
        'StartInterval',
    }
    assert automatic_start_keys.isdisjoint(parsed)


def test_jobs_auto_naming(tmp_pythonpath: Path) -> None:
    tpath = Path(tmp_pythonpath) / 'test_drontab.py'
    tpath.write_text(
        '''
from typing import Iterator

from dron.api import job, Job


job2 = job(
    '00:02',
    'echo',
)


def job_maker(when) -> Job:
    return job(when, 'echo job maker', stacklevel=2)


def jobs() -> Iterator[Job]:
    job_1 = job('00:01',
        'echo',
    )
    yield job2
    yield job('00:00', 'echo', unit_name='job_named')
    yield job_1
    job4 = \
       job('00:04', 'echo')
    job5     = job_maker('00:05')
    yield job5
    yield job4
'''
    )
    loaded = list(load_jobs(tab_module='test_drontab'))
    (job2, job_named, job_1, job5, job4) = loaded
    assert job_1.unit_name == 'job_1'
    assert job_1.when == '00:01'
    assert job2.unit_name == 'job2'
    assert job2.when == '00:02'
    assert job_named.unit_name == 'job_named'
    assert job_named.when == '00:00'
    assert job4.unit_name == 'job4'
    assert job4.when == '00:04'
    assert job5.unit_name == 'job5'
    assert job5.when == '00:05'


def test_load_state(tmp_pythonpath: Path) -> None:
    def OK(body: str) -> None:
        tpath = Path(tmp_pythonpath) / 'test_drontab.py'
        tpath.write_text(body)
        load_state(tab_module='test_drontab')

    def FAILS(body: str) -> None:
        with pytest.raises(Exception):
            OK(body)

    FAILS(
        body='''
    None.whatever
    '''
    )

    # no jobs
    FAILS(
        body='''
    '''
    )

    OK(
        body='''
def jobs():
    yield from []
'''
    )

    OK(
        body='''
from dron.api import job
def jobs():
    yield job(
        'hourly',
        ['/bin/echo', '123'],
        unit_name='unit_test',
    )
'''
    )

    from ..systemd import _is_missing_systemd

    if not _is_missing_systemd():
        from ..cli import _drontab_example

        # this test doesn't work without systemd yet, because launchd adapter doesn't support unquoted commands, at least yet..
        example = _drontab_example()
        # ugh. some hackery to make it find the executable..
        echo = " '/bin/echo"
        example = (
            example.replace(" 'linkchecker", echo)
            .replace(" '/home/user/scripts/run-borg", echo)
            .replace(" 'ping", " '/bin/ping")
        )
        OK(body=example)
