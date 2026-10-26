"""Regression tests for plugin-owned background task replacement."""

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType


def _load_scheduler_module():
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("test-task-scheduler")
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

    module_path = Path(__file__).parent / "qqadmin" / "task_scheduler.py"
    spec = importlib.util.spec_from_file_location("_task_scheduler_test", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


task_scheduler = _load_scheduler_module()


class TestBackgroundTaskScheduler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        setattr(asyncio, "_joker_background_task_registry", {})

    async def asyncTearDown(self):
        registry = getattr(asyncio, "_joker_background_task_registry", {})
        for task in tuple(registry.values()):
            if not task.done():
                task.cancel()
        if registry:
            await asyncio.gather(*registry.values(), return_exceptions=True)
        delattr(asyncio, "_joker_background_task_registry")

    async def test_new_instance_replaces_old_task(self):
        old_started = asyncio.Event()
        old_cancelled = asyncio.Event()
        new_started = asyncio.Event()
        new_cancelled = asyncio.Event()

        async def old_worker():
            old_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_cancelled.set()
                raise

        async def new_worker():
            new_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                new_cancelled.set()
                raise

        old_scheduler = task_scheduler.BackgroundTaskScheduler("test")
        old_task = old_scheduler.start("loop", old_worker)
        await asyncio.wait_for(old_started.wait(), timeout=1)

        new_scheduler = task_scheduler.BackgroundTaskScheduler("test")
        new_task = new_scheduler.start("loop", new_worker)
        await asyncio.wait_for(old_cancelled.wait(), timeout=1)
        await asyncio.wait_for(new_started.wait(), timeout=1)
        await asyncio.sleep(0)

        self.assertTrue(old_task.cancelled())
        self.assertIs(new_task, new_scheduler.get("loop"))

        # Old-instance termination must not cancel the replacement task.
        await old_scheduler.stop_all()
        self.assertFalse(new_task.done())

        await new_scheduler.stop_all()
        await asyncio.wait_for(new_cancelled.wait(), timeout=1)
        self.assertTrue(new_task.cancelled())
        self.assertIsNone(new_scheduler.get("loop"))

    async def test_new_configuration_can_stop_a_task_owned_by_old_instance(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        old_scheduler = task_scheduler.BackgroundTaskScheduler("test")
        task = old_scheduler.start("optional_loop", worker)
        await asyncio.wait_for(started.wait(), timeout=1)

        disabled_scheduler = task_scheduler.BackgroundTaskScheduler("test")
        await disabled_scheduler.stop_active("optional_loop")

        await asyncio.wait_for(cancelled.wait(), timeout=1)
        self.assertTrue(task.cancelled())
        self.assertIsNone(disabled_scheduler.get("optional_loop"))


if __name__ == "__main__":
    unittest.main()
