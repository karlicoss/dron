from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ..dron import do_lint, load_jobs


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


def test_do_lint(tmp_pythonpath: Path) -> None:
    def OK(body: str) -> None:
        tpath = Path(tmp_pythonpath) / 'test_drontab.py'
        tpath.write_text(body)
        do_lint(tab_module='test_drontab')

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
