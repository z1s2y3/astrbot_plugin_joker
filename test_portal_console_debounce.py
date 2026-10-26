"""Regression tests for the Windows CMD-event portal sync debounce."""

import ast
import copy
import logging
import unittest
from pathlib import Path
from types import MethodType


MAIN_PATH = Path(__file__).resolve().parent / "main.py"


def _load_main_methods(*names):
    """Extract the small sync methods without importing AstrBot runtime modules."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MyPlugin")
    namespace = {"logger": logging.getLogger("test_portal_console_debounce")}
    for name in names:
        node = next(child for child in class_node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name)
        extracted = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
        exec(compile(ast.fix_missing_locations(extracted), str(MAIN_PATH), "exec"), namespace)
    return {name: namespace[name] for name in names}


METHODS = _load_main_methods(
    "_on_local_console_event_written",
    "_schedule_portal_console_sync",
    "_flush_portal_console_sync",
    "_cancel_portal_console_sync",
    "_on_local_group_config_changed",
)


class _Handle:
    def __init__(self, callback):
        self.callback = callback
        self.was_cancelled = False

    def cancelled(self):
        return self.was_cancelled

    def cancel(self):
        self.was_cancelled = True


class _Loop:
    def __init__(self):
        self.soon = []
        self.later = []

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, callback):
        self.soon.append(callback)

    def call_later(self, delay, callback):
        handle = _Handle(callback)
        self.later.append((delay, handle))
        return handle


class _Plugin:
    _PORTAL_CONSOLE_SYNC_DEBOUNCE_SECONDS = 3.0


def _plugin(loop):
    plugin = _Plugin()
    plugin._portal_sync_loop = loop
    plugin._portal_console_sync_handle = None
    plugin.sync_requests = 0
    plugin._request_portal_sync = lambda: setattr(plugin, "sync_requests", plugin.sync_requests + 1)
    for name, method in METHODS.items():
        setattr(plugin, name, MethodType(method, plugin))
    return plugin


class PortalConsoleDebounceTests(unittest.TestCase):
    def test_burst_of_console_events_schedules_one_delayed_wake(self):
        loop = _Loop()
        plugin = _plugin(loop)

        plugin._on_local_console_event_written()
        plugin._on_local_console_event_written()
        self.assertEqual(2, len(loop.soon))
        # The callbacks are marshalled onto the event loop; run both in order.
        loop.soon.pop(0)()
        loop.soon.pop(0)()

        self.assertEqual(1, len(loop.later))
        delay, handle = loop.later[0]
        self.assertEqual(3.0, delay)
        self.assertEqual(0, plugin.sync_requests)

        handle.callback()
        self.assertEqual(1, plugin.sync_requests)
        self.assertIsNone(plugin._portal_console_sync_handle)

    def test_group_configuration_change_remains_immediate(self):
        loop = _Loop()
        plugin = _plugin(loop)

        plugin._on_local_group_config_changed("1102579807")

        self.assertEqual(1, len(loop.soon))
        loop.soon[0]()
        self.assertEqual(1, plugin.sync_requests)
        self.assertEqual(0, len(loop.later))

    def test_pending_console_wake_is_cancelled_on_cleanup(self):
        loop = _Loop()
        plugin = _plugin(loop)
        plugin._schedule_portal_console_sync()
        handle = plugin._portal_console_sync_handle
        self.assertIsNotNone(handle)

        plugin._cancel_portal_console_sync()

        self.assertTrue(handle.was_cancelled)
        self.assertIsNone(plugin._portal_console_sync_handle)


if __name__ == "__main__":
    unittest.main()
