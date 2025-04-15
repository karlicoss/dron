from __future__ import annotations

from dataclasses import asdict, fields
from functools import lru_cache
from typing import Any

from textual import events, work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log

from .common import MonitorEntry, MonitorParams, unwrap
from .dron import get_entries_for_monitor, managed_units


# todo try RichLog? https://textual.textualize.io/guide/input
class LogHeader(Log):
    DEFAULT_CSS = """
    LogHeader {
        height: 5;
        dock: top;
    }
    """


MonitorEntries = dict[str, MonitorEntry]


@lru_cache(None)
def get_columns() -> list[str]:
    cols: list[str] = []
    for f in fields(MonitorEntry):
        cols.append(f.name)

    # TODO how to check it statically? MonitorEntry.pid isn't giving anything?
    cols.remove('pid')  # probs don't need?
    cols.remove('status_ok')
    return cols


def as_row(entry: MonitorEntry) -> dict[str, Any]:
    cols = get_columns()
    res = {k: v for k, v in asdict(entry).items() if k in cols}

    color = 'green' if entry.status_ok else 'red'
    res['status'] = f'[{color}]' + res['status'] + f'[/{color}]'

    # meh. workaround for next/left being max datetime
    if res['next'].startswith('9999-'):
        res['left'] = '--'
        res['next'] = '[yellow]' + 'never' + '[/yellow]'

    if entry.pid is not None:
        res['left'] = '--'
        res['next'] = '[yellow]' + 'running' + '[/yellow]'
    return res


class MonitorApp(App):
    def __init__(self, *, monitor_params: MonitorParams, refresh_every: int) -> None:
        super().__init__()
        self.monitor_params = monitor_params
        self.refresh_every = refresh_every

    def compose(self) -> ComposeResult:
        # TODO self.log is already defined?? what is it?
        self.logx = LogHeader(auto_scroll=True, highlight=True)

        # TODO input field to filter out jobs?

        # doesn't do anything??
        # self.log("HELLOOO")

        self.table: DataTable = DataTable(
            cursor_type='row',
            zebra_stripes=True,  # alternating colours
        )

        # NOTE: useful for debugging
        # yield self.logx
        yield self.table

    def on_mount(self) -> None:
        table = self.table

        for col in get_columns():
            table.add_column(label=col, key=col)

        # todo how to do async here as well?
        entries = self.get_entries()
        self.update(entries)

        self.set_focus(table)

        # TODO run and update it continuously? not sure
        # what if it isn't computed within interval?
        self.set_interval(interval=self.refresh_every, callback=self.update_in_background)

    def get_entries(self) -> MonitorEntries:
        managed = list(managed_units(with_body=False))  # body slows down this call quite a bit
        entries = get_entries_for_monitor(managed=managed, params=self.monitor_params)
        return {e.unit: e for e in entries}

    def update(self, entries: MonitorEntries) -> None:
        table = self.table

        # self.logx.write_line(f"HI {datetime.now().isoformat()}")

        current_rows: set[str] = {unwrap(x.value) for x in table.rows}
        to_remove = {x for x in current_rows if x not in entries}
        to_add = {x: entry for x, entry in entries.items() if x not in current_rows}
        to_update = {x: entry for x, entry in entries.items() if x in current_rows}

        for row_key in to_remove:
            table.remove_row(row_key=row_key)

        for row_key, entry in to_add.items():
            vals = as_row(entry).values()
            table.add_row(*vals, key=row_key)

        for row_key, entry in to_update.items():
            for col, value in as_row(entry).items():
                table.update_cell(row_key=row_key, column_key=col, value=value, update_width=True)

        columns = get_columns()

        def sort_key(row):
            data = dict(zip(columns, row, strict=True))
            is_running = 'running' in data['next']
            failed = 'exit-code' in data['status']
            return (not is_running, not failed, data['unit'])

        # TODO hmm kinda annoying, doesn't look like it preserves cursor position
        #  if the item pops on top of the list when a service is running?
        #  but I guess not a huge deal now
        table.sort(*columns, key=sort_key)

    @work(exclusive=True, thread=True)
    def update_in_background(self) -> None:
        entries = self.get_entries()
        self.call_from_thread(self.update, entries)

    def on_key(self, event: events.Key) -> None:
        actions = {
            'j': self.table.action_cursor_down,
            'k': self.table.action_cursor_up,
            'h': self.table.action_cursor_left,
            'l': self.table.action_cursor_right,
        }
        action = actions.get(event.key)
        if action is not None:
            action()
            return
        if event.key == 'q':
            self.exit()
        # TODO would be nice to display log on enter press or something?
