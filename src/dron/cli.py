from __future__ import annotations

import argparse
import socket
import sys
from pprint import pprint

import click

from . import launchd, systemd
from .api import UnitName
from .common import (
    IS_SYSTEMD,
    MANAGED_MARKER,
    MonitorParams,
    Unit,
    escape,
    logger,
    print_monitor,
)
from .dron import (
    apply,
    do_lint,
    get_entries_for_monitor,
    load_jobs,
    manage,
    managed_units,
)


def cmd_lint(tab_module: str) -> None:
    # FIXME lint command isn't very interesting now btw?
    # perhaps instead, either add dry mode to apply
    # or split into the 'diff' part and side effect apply part
    _state = do_lint(tab_module)
    logger.info('all good')


def cmd_print(*, tab_module: str, pretty: bool) -> None:
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


def cmd_run(*, unit: Unit, do_exec: bool) -> None:
    if IS_SYSTEMD:
        return systemd.cmd_run(unit=unit, do_exec=do_exec)
    else:
        return launchd.cmd_run(unit=unit, do_exec=do_exec)


def cmd_past(unit: Unit) -> None:
    if IS_SYSTEMD:
        return systemd.cmd_past(unit)
    else:
        return launchd.cmd_past(unit)


# TODO think if it's worth integrating with timers?
def cmd_monitor(params: MonitorParams) -> None:
    managed = list(managed_units(with_body=False))  # body slows down this call quite a bit
    if len(managed) == 0:
        logger.warning('no managed units!')

    logger.debug('starting monitor...')

    entries = get_entries_for_monitor(managed=managed, params=params)
    print_monitor(entries)


# TODO test it on CI?
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


def make_parser() -> argparse.ArgumentParser:
    from .common import VerifyOff

    def add_verify(p: argparse.ArgumentParser) -> None:
        # specify in readme???
        # would be nice to use external checker..
        # https://github.com/systemd/systemd/issues/8072
        # https://unix.stackexchange.com/questions/493187/systemd-under-ubuntu-18-04-1-fails-with-failed-to-create-user-slice-serv
        p.add_argument('--no-verify', action=VerifyOff, nargs=0, help='Skip systemctl verify step')

    p = argparse.ArgumentParser(
        prog='dron',
        description='''
dron -- simple frontend for Systemd, inspired by cron.

- *d* stands for 'Systemd'
- *ron* stands for 'cron'

dron is my attempt to overcome things that make working with Systemd tedious
'''.lstrip(),
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(prog, width=100),
    )
    # TODO ugh. when you type e.g. 'dron apply', help format is wrong..
    example = ''.join(': ' + l for l in _drontab_example().splitlines(keepends=True))
    # TODO begin_src python maybe?
    p.epilog = f'''
* What does it do?
In short, you type ~dron edit~ and edit your config file, similarly to ~crontab -e~:

{example}

After you save your changes and exit the editor, your drontab is checked for syntax and applied

- if checks have passed, your jobs are mapped onto Systemd units and started up
- if there are potential errors, you are prompted to fix them before retrying

* Why?
In short, because I want to benefit from the heavy lifting that Systemd does: timeouts, resource management, restart policies, powerful scheduling specs and logging,
while not having to manually manipulate numerous unit files and restart the daemon all over.

I elaborate on what led me to implement it and motivation [[https://beepb00p.xyz/scheduler.html#what_do_i_want][here]]. Also:

- why not just use [[https://beepb00p.xyz/scheduler.html#cron][cron]]?
- why not just use [[https://beepb00p.xyz/scheduler.html#systemd][systemd]]?
    '''

    p.add_argument(
        '--marker', required=False, help=f'Use custom marker instead of default `{MANAGED_MARKER}`. Useful for developing/testing.'
    )

    def add_tab_module_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument('tab_module', type=str, nargs='?')

    sp = p.add_subparsers(dest='mode')

    ### actions on drontab file
    apply_parser = sp.add_parser('apply', help="Apply drontab (like 'crontab' with no args)")
    add_tab_module_arg(apply_parser)
    add_verify(apply_parser)
    # TODO --force?
    lint_parser = sp.add_parser('lint', help="Check drontab (no 'crontab' alternative, sadly!)")
    add_tab_module_arg(lint_parser)
    add_verify(lint_parser)

    print_parser = sp.add_parser('print', help="Parse and print drontab")
    add_tab_module_arg(print_parser)
    print_parser.add_argument('--pretty', action='store_true')
    ###

    ### actions on managed jobs
    _debug_parser = sp.add_parser('debug', help='Print some debug info')

    uninstall_parser = sp.add_parser('uninstall', help="Uninstall all managed jobs")
    add_verify(uninstall_parser)

    run_parser = sp.add_parser('run', help='Run the job right now, ignoring the timer')
    run_parser.add_argument('unit', type=str, nargs='?')  # TODO add shell completion?
    run_parser.add_argument('--exec', action='store_true', dest='do_exec', help='Run directly, not via systemd/launchd')

    past_parser = sp.add_parser('past', help='List past job runs')
    past_parser.add_argument('unit', type=str, nargs='?')  # TODO add shell completion?
    ###

    ### misc actions
    mp = sp.add_parser('monitor', help='Monitor services/timers managed by dron')
    mp.add_argument('-n', type=int, default=1, help='refresh every n seconds')
    mp.add_argument('--once', action='store_true', help='only call once')
    mp.add_argument('--rate', action='store_true', help='Display success rate (unstable and potentially slow)')
    mp.add_argument('--command', action='store_true', help='Display command')
    ###
    return p


def main() -> None:
    from .cli import make_parser

    p = make_parser()
    args = p.parse_args()

    marker: str | None = args.marker
    if marker is not None:
        from . import common

        common.MANAGED_MARKER = marker

    mode: str = args.mode

    def get_tab_module() -> str:
        tab_module = args.tab_module
        if tab_module is not None:
            return tab_module
        # TODO document this?
        return f'drontab.{socket.gethostname()}'

    def prompt_for_unit() -> UnitName:
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

    if mode == 'apply':
        tab_module = get_tab_module()
        apply(tab_module)
    elif mode == 'lint':
        tab_module = get_tab_module()
        cmd_lint(tab_module)
    elif mode == 'print':
        tab_module = get_tab_module()
        cmd_print(tab_module=tab_module, pretty=args.pretty)
    elif mode == 'debug':
        managed = managed_units(with_body=False)  # TODO not sure about body
        for x in managed:
            pprint(x, stream=sys.stderr)
    elif mode == 'uninstall':
        click.confirm('Going to remove all dron managed jobs. Continue?', default=True, abort=True)
        manage([])
    elif mode == 'run':
        unit = args.unit if args.unit is not None else prompt_for_unit()
        do_exec = args.do_exec
        cmd_run(unit=unit, do_exec=do_exec)
    elif mode == 'past':
        unit = args.unit if args.unit is not None else prompt_for_unit()
        cmd_past(unit=unit)
    elif mode == 'monitor':
        once = args.once

        params = MonitorParams(
            with_success_rate=args.rate,
            with_command=args.command,
        )

        if once:
            # fallback on old style monitor for now?
            # this can be quite useful for grepping etc..
            cmd_monitor(params=params)
        else:
            from .monitor import MonitorApp

            app = MonitorApp(monitor_params=params, refresh_every=args.n)
            app.run()
    else:
        logger.error(f'Unknown mode: {mode}')
        p.print_usage(sys.stderr)
        sys.exit(1)
