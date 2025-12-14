from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime
from typing import Any, ClassVar, override

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, RichLog, Static
from textual.widgets.data_table import RowKey

from .common import MonitorEntry, MonitorParams
from .dron import get_entries_for_monitor, managed_units

MonitorEntries = dict[RowKey, MonitorEntry]


def get_entries(params: MonitorParams, *, mock: bool = False) -> MonitorEntries:
    # mock is useful for testing without overhead etc from systemd
    if mock:
        entries = []
        statuses = ['active', 'inactive', 'failed', 'exit-code: 1', 'exit-code: 127']

        for i in range(200):
            unit_name = f"mock-service-{i:03d}.timer"
            status = statuses[i % len(statuses)]
            status_ok = status in ['active', 'inactive']
            command = f"/usr/bin/mock-command-{i % 10}" if params.with_command else None

            entry = MonitorEntry(
                unit=unit_name,
                status=status,
                left='5min',
                next='2025-12-14 12:00:00',
                schedule='*:0/5',
                command=command,
                pid=None,
                status_ok=status_ok,
            )
            entries.append(entry)
    else:
        managed = list(managed_units(with_body=False))  # body slows down this call quite a bit
        entries = get_entries_for_monitor(managed=managed, params=params)
    return {RowKey(e.unit): e for e in entries}


class Clock(Static):
    """
    Displays current time with millisecond precision. Useful for debugging.
    """

    def update_time(self, dt: datetime) -> None:
        self.update(f"refreshed at: {dt.isoformat()}")


class UnitsTable(DataTable):
    BINDINGS: ClassVar = [
        Binding("j", "cursor_down"   , "Down"         , show=False),
        Binding("k", "cursor_up"     , "Up"           , show=False),
        Binding("h", "cursor_left"   , "Left"         , show=False),
        Binding("l", "cursor_right"  , "Right"        , show=False),
        Binding("g", "scroll_top"    , "Top"          , show=False),
        Binding("G", "scroll_bottom" , "Bottom"       , show=False),
        Binding("^", "scroll_home"   , "Start of line", show=False),
        Binding("$", "scroll_end"    , "End of line"  , show=False),
    ]  # fmt: skip
    # TODO would be nice to display log on enter press or something?

    def __init__(self, params: MonitorParams) -> None:
        super().__init__(
            cursor_type='row',
            zebra_stripes=True,  # alternating colours
        )
        self.params = params

        # todo how to check it statically? MonitorEntry.pid isn't giving anything?
        excluded = {
            'pid',
            'status_ok',
        }
        if not self.params.with_command:
            excluded.add('command')
        # hmm a bit nasty that if we name it self.columns we might mess with base class
        # maybe not ideal to use inheritance here..
        self.display_columns = [f.name for f in fields(MonitorEntry) if f.name not in excluded]

    @override
    def on_mount(self) -> None:
        for col in self.display_columns:
            self.add_column(label=col, key=col)

    def as_row(self, entry: MonitorEntry) -> dict[str, Any]:
        res = {k: v for k, v in asdict(entry).items() if k in self.display_columns}

        style = 'green' if entry.status_ok else 'red bold'
        res['status'] = Text(res['status'], style=style)

        # meh. workaround for next/left being max datetime
        if res['next'].startswith('9999-'):
            res['left'] = '--'
            res['next'] = Text('never', style='yellow')

        if entry.pid is not None:
            res['left'] = '--'
            res['next'] = Text('running', style='yellow')
        return res

    def update_entries(self, entries: MonitorEntries) -> None:
        current_rows: set[RowKey] = set(self.rows.keys())

        to_remove: set[RowKey] = {key for key in current_rows if key not in entries}
        for key in to_remove:
            self.remove_row(row_key=key)

        for key, entry in entries.items():
            new_row = self.as_row(entry)
            if key not in current_rows:
                self.add_row(*new_row.values(), key=key.value)
            else:
                for col, new_value in new_row.items():
                    curr_value = self.get_cell(row_key=key, column_key=col)
                    # hmm seems like DataTable is a bit dumb and even if value is the same, it does costly UI updates...
                    # this is quite noticeable optimization
                    if curr_value != new_value:
                        self.update_cell(row_key=key, column_key=col, value=new_value, update_width=True)

        def sort_key(row: list[str]):
            # kinda annoying to do that because interlally DataTable keeps row as dict[ColKey, str]...
            # but before using the key it's converted to a sequence.. ugh
            is_running = 'running' in row[self.get_column_index('next')]
            failed = 'exit-code' in row[self.get_column_index('status')]
            return (not is_running, not failed, row[self.get_column_index('unit')])

        # TODO hmm kinda annoying, doesn't look like it preserves cursor position
        #  if the item pops on top of the list when a service is running?
        #  but I guess not a huge deal now
        self.sort(key=sort_key)


class MonitorApp(App):
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        # Disable default ctrl+q, conflicting with OS/terminal bindings
        Binding("ctrl+q", "pass", show=False, priority=True),
    ]

    CSS = """
    Clock {
        dock: top;
        height: 1;
        background: $primary;
        color: $text;
    }
    UnitsTable {
        height: 1fr;
    }
    RichLog {
        height: 5;
        dock: bottom;
        border-top: solid $secondary;
    }
    """

    def __init__(
        self,
        *,
        # annoying to have default args here, but it's convenient for interactive testing with 'textual run ...'
        monitor_params: MonitorParams = MonitorParams(with_success_rate=False, with_command=False),  # noqa: B008
        refresh_every: float = 2.0,
        show_logger: bool = True,
    ) -> None:
        super().__init__()
        self.monitor_params = monitor_params
        self.refresh_every = refresh_every
        self.show_logger = show_logger

    @override
    def compose(self) -> ComposeResult:
        yield Clock()

        # TODO input field to filter out jobs?

        yield UnitsTable(params=self.monitor_params)

        # useful for debugging
        yield RichLog()

    @property
    def clock(self) -> Clock:
        return self.query_one(Clock)

    @property
    def units_table(self) -> UnitsTable:
        return self.query_one(UnitsTable)

    @property
    def rich_log(self) -> RichLog:
        return self.query_one(RichLog)

    # @override  # TODO weird.. type checker complains it's not present in base class?
    def on_mount(self) -> None:
        if not self.show_logger:
            self.rich_log.display = False

        self.units_table.focus()

        self._update_entries()

        # Hmm tried using set_interval..
        # But I think if refresh interval is low enough, it just cancels previous requests
        # , so ends up never rendering anything??
        # self.set_interval(interval=self.refresh_every, callback=self._update_entries)
        # Instead relying on set_timer and tail call in update_entries_ui

    # exclusive cancels previous call if it happens still to run
    @work(exclusive=True, thread=True)
    def _update_entries(self) -> None:
        # NOTE: this only goes into dev console
        # need to run via
        # - TEXTUAL_DEBUG=1 uu run --with=textual-dev textual run --dev ...
        # - also need to run in another tab uu tool run --from textual-dev textual console
        # self.log("UPDATING")

        entries = get_entries(params=self.monitor_params)
        # TODO hmm it likely still spending some time in CPU, so not sure how much thread would help
        self.call_from_thread(self.update_entries_ui, entries)

    def update_entries_ui(self, entries: MonitorEntries) -> None:
        updated_at = datetime.now()

        self.rich_log.write(f'{updated_at} UPDATING!')
        self.units_table.update_entries(entries)
        self.clock.update_time(updated_at)
        self.rich_log.write(f'{updated_at} UPDATED!')

        if self.refresh_every > 0:
            self.set_timer(delay=self.refresh_every, callback=self._update_entries)
        else:
            # update as fast as possible
            self._update_entries()
