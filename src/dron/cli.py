from __future__ import annotations

import socket
import sys
from pprint import pprint

import click

from . import common, launchd, systemd
from .api import UnitName
from .common import (
    IS_SYSTEMD,
    MonitorParams,
    Unit,
    escape,
    logger,
    print_monitor,
    set_verify_off,
)
from .dron import (
    apply,
    do_lint,
    get_entries_for_monitor,
    load_jobs,
    manage,
    managed_units,
)


# TODO test it on CI?
# TODO explicitly inject it into readme?
def _drontab_example() -> str:
    return '''
from dron.api import job

# at the moment you're expected to define jobs() function that yields jobs
# in the future I might add more mechanisms
def jobs():
    # simple job that doesn't do much
    yield job(
        'daily',
        '/home/user/scripts/run-borg /home/user',
        unit_name='borg-backup-home',
    )

    yield job(
        'daily',
        'linkchecker https://beepb00p.xyz',
        unit_name='linkchecker-beepb00p',
    )

    # drontab is simply python code!
    # so if you're annoyed by having to rememver Systemd syntax, you can use a helper function
    def every(*, mins: int) -> str:
        return f'*:0/{mins}'

    # make sure my website is alive, it will send local email on failure
    yield job(
        every(mins=10),
        'ping https://beepb00p.xyz',
        unit_name='ping-beepb00p',
    )
'''.lstrip()


def _get_epilog() -> str:
    return '''
* Why?

In short, because I want to benefit from the heavy lifting that Systemd does: timeouts, resource management, restart policies, powerful scheduling specs and logging,
while not having to manually manipulate numerous unit files and restart the daemon all over.

I elaborate on what led me to implement it and motivation [[https://beepb00p.xyz/scheduler.html#what_do_i_want][here]]. Also:

\b
- why not just use [[https://beepb00p.xyz/scheduler.html#cron][cron]]?
- why not just use [[https://beepb00p.xyz/scheduler.html#systemd][systemd]]?
'''.strip()


@click.group(
    context_settings={'show_default': True},
    help="""
dron -- simple frontend for Systemd, inspired by cron.

\b
- *d* stands for 'Systemd'
- *ron* stands for 'cron'

dron is my attempt to overcome things that make working with Systemd tedious
""".strip(),
    epilog=_get_epilog(),
)
@click.option(
    '--marker',
    required=False,
    help=f'Use custom marker instead of default `{common.MANAGED_MARKER}`. Useful for developing/testing.',
)
def cli(*, marker: str | None) -> None:
    if marker is not None:
        common.MANAGED_MARKER = marker


arg_tab_module = click.option(
    '--module',
    'tab_module',
    type=str,
    default=f'drontab.{socket.gethostname()}',
)


# specify in readme???
# would be nice to use external checker..
# https://github.com/systemd/systemd/issues/8072
# https://unix.stackexchange.com/questions/493187/systemd-under-ubuntu-18-04-1-fails-with-failed-to-create-user-slice-serv
def _set_verify_off(ctx, param, value) -> None:  # noqa: ARG001
    if value is True:
        set_verify_off()


arg_no_verify = click.option(
    '--no-verify',
    is_flag=True,
    callback=_set_verify_off,
    expose_value=False,
    help='Skip systemctl verify step',
)


@cli.command('lint')
@arg_tab_module
@arg_no_verify
def cmd_lint(*, tab_module: str) -> None:
    # FIXME how to disable verity?
    # FIXME lint command isn't very interesting now btw?
    # perhaps instead, either add dry mode to apply
    # or split into the 'diff' part and side effect apply part
    _state = do_lint(tab_module)
    logger.info('all good')


@cli.command('print')
@arg_tab_module
@click.option('--pretty', is_flag=True, help='Pretty print')
@arg_no_verify
def cmd_print(*, tab_module: str, pretty: bool) -> None:
    """Parse and print drontab"""
    jobs = list(load_jobs(tab_module=tab_module))

    if pretty:
        import tabulate

        items = [
            {
                'UNIT': job.unit_name,
                'SCHEDULE': job.when,
                'COMMAND': escape(job.command),
            }
            for job in jobs
        ]
        print(tabulate.tabulate(items, headers="keys"))
    else:
        for j in jobs:
            print(j)


# TODO --force?
@cli.command('apply')
@arg_tab_module
def cmd_apply(*, tab_module: str) -> None:
    """Apply drontab (like 'crontab' with no args)"""
    apply(tab_module)


@cli.command('debug')
def cmd_debug() -> None:
    """Print some debug info"""
    managed = managed_units(with_body=False)  # TODO not sure about body
    for x in managed:
        pprint(x, stream=sys.stderr)


@cli.command('uninstall')
def cmd_uninstall() -> None:
    """Remove all managed jobs (will ask for confirmation)"""
    click.confirm('Going to remove all dron managed jobs. Continue?', default=True, abort=True)
    manage([])


@cli.group('job')
def cli_job() -> None:
    """Actions on individual jobs"""
    pass


def _prompt_for_unit() -> UnitName:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter

    # TODO print options
    managed = list(managed_units(with_body=False))
    units = [x.unit_file.stem for x in managed]

    print('Units under dron:', file=sys.stderr)
    for u in units:
        print(f'- {u}', file=sys.stderr)

    completer = WordCompleter(units, ignore_case=True)
    session = PromptSession("Select a unit: ", completer=completer)  # type: ignore[var-annotated]
    selected = session.prompt()
    return selected


arg_unit = click.argument('unit', type=Unit, default=_prompt_for_unit)


@cli_job.command('past')
@arg_unit
def cmd_past(unit: Unit) -> None:
    if IS_SYSTEMD:
        # TODO hmm seems like this just exit with 0 if unit diesn't exist
        return systemd.cmd_past(unit)
    else:
        return launchd.cmd_past(unit)


@cli_job.command('run')
@arg_unit
@click.option('--exec', 'do_exec', is_flag=True, help='Run directly, not via systemd/launchd')
def cmd_run(*, unit: Unit, do_exec: bool) -> None:
    """Run the job right now, ignoring the timer"""
    if IS_SYSTEMD:
        return systemd.cmd_run(unit=unit, do_exec=do_exec)
    else:
        return launchd.cmd_run(unit=unit, do_exec=do_exec)


@cli.command('monitor')
@click.option('-n', type=float, default=1.0, help='refresh every n seconds')
@click.option('--once', is_flag=True, help='only call once')
@click.option('--rate', is_flag=True, help='Display success rate (unstable and potentially slow)')
@click.option('--command', is_flag=True, help='Display command')
def cmd_monitor(*, n: float, once: bool, rate: bool, command: bool) -> None:
    """Monitor services/timers managed by dron"""
    params = MonitorParams(
        with_success_rate=rate,
        with_command=command,
    )

    if once:
        # old style monitor
        # TODO think if it's worth integrating with timers?
        managed = list(managed_units(with_body=False))  # body slows down this call quite a bit
        if len(managed) == 0:
            logger.warning('no managed units!')

        logger.debug('starting monitor...')

        entries = get_entries_for_monitor(managed=managed, params=params)
        print_monitor(entries)
    else:
        from .monitor import MonitorApp

        app = MonitorApp(
            monitor_params=params,
            refresh_every=n,
            show_logger=False,
        )
        app.run()


def main() -> None:
    cli()
