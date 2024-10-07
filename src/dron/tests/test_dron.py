from __future__ import annotations

from pathlib import Path

import pytest

from ..dron import load_jobs


def test_load_jobs_basic(tmp_path: Path) -> None:
    tpath = Path(tmp_path) / 'drontab.py'
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
    loaded = list(load_jobs(tabfile=tpath, ppath=tmp_path))
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


def test_load_jobs_dupes(tmp_path: Path) -> None:
    tpath = Path(tmp_path) / 'drontab.py'
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
        _loaded = list(load_jobs(tabfile=tpath, ppath=tmp_path))


def test_jobs_auto_naming(tmp_path: Path) -> None:
    tpath = Path(tmp_path) / 'drontab.py'
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
    loaded = list(load_jobs(tabfile=tpath, ppath=tmp_path))
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
