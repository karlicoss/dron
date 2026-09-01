import asyncio
from unittest.mock import patch

from textual import events

from dron.monitor import MonitorApp


def test_focus_events_adjust_refresh_schedule() -> None:
    async def test() -> None:
        app = MonitorApp(show_logger=False)
        with patch.object(MonitorApp, '_update_entries', return_value=None) as update_entries:
            async with app.run_test() as pilot:
                assert app.clock.focused is True
                update_entries.reset_mock()

                with patch.object(app, 'set_timer', wraps=app.set_timer) as set_timer:
                    app.post_message(events.AppBlur())
                    await pilot.pause()
                    assert app.clock.focused is False
                    assert set_timer.call_args.kwargs['delay'] == app.UNFOCUSED_REFRESH_EVERY
                    update_entries.assert_not_called()

                    app.post_message(events.AppFocus())
                    await pilot.pause()
                    assert app.clock.focused is True
                    update_entries.assert_called_once_with()

                    app.update_entries_ui({})
                    assert set_timer.call_args.kwargs['delay'] == app.refresh_every

    asyncio.run(test())
