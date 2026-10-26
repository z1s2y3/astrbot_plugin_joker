"""Small lifecycle manager for plugin-owned background tasks.

The registry is process-wide on purpose: a hot-reloaded plugin instance can
replace the previous instance's task without inspecting or cancelling
unrelated asyncio tasks.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

try:  # AstrBot supplies the plugin logger in production.
    from astrbot.api import logger
except ImportError:
    try:
        from ..runtime_compat import logger
    except ImportError:
        from runtime_compat import logger


TaskFactory = Callable[[], Coroutine[Any, Any, Any]]


class BackgroundTaskScheduler:
    """Own and replace a named set of background tasks."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._owned_tasks: dict[str, asyncio.Task] = {}

    def start(self, name: str, factory: TaskFactory) -> asyncio.Task:
        """Start a task, replacing a task left by an older plugin instance."""
        key = f"{self._namespace}:{name}"
        tasks = self._shared_tasks()
        previous = tasks.get(key)
        if previous is not None and not previous.done():
            previous.cancel()

        task = asyncio.create_task(factory(), name=key)
        tasks[key] = task
        self._owned_tasks[name] = task
        task.add_done_callback(lambda finished: self._on_task_done(key, finished))
        return task

    def get(self, name: str) -> asyncio.Task | None:
        return self._shared_tasks().get(f"{self._namespace}:{name}")

    async def stop(self, name: str) -> None:
        key = f"{self._namespace}:{name}"
        tasks = self._shared_tasks()
        task = self._owned_tasks.pop(name, None)
        if task is None:
            return
        # A newer plugin instance may already have replaced this task under
        # the same key.  An old instance must never stop the newer instance.
        if tasks.get(key) is not task:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Background task stopped with an error: %s: %s", key, exc)
        if tasks.get(key) is task:
            tasks.pop(key, None)

    async def stop_active(self, name: str) -> None:
        """Stop the current task for a plugin-owned name, regardless of owner.

        This is used when a new configuration disables a periodic task that
        was started by an older plugin instance.
        """
        key = f"{self._namespace}:{name}"
        tasks = self._shared_tasks()
        task = tasks.get(key)
        self._owned_tasks.pop(name, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Background task stopped with an error: %s: %s", key, exc)
        if tasks.get(key) is task:
            tasks.pop(key, None)

    def cancel_active(self, name: str) -> None:
        """Synchronously request cancellation of a plugin-owned named task."""
        key = f"{self._namespace}:{name}"
        tasks = self._shared_tasks()
        task = tasks.get(key)
        self._owned_tasks.pop(name, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        if tasks.get(key) is task:
            tasks.pop(key, None)

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.stop(name) for name in tuple(self._owned_tasks)),
            return_exceptions=True,
        )
        self._owned_tasks.clear()

    @classmethod
    def _on_task_done(cls, key: str, task: asyncio.Task) -> None:
        tasks = cls._shared_tasks()
        if tasks.get(key) is task:
            tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("Background task failed: %s: %s", key, error)

    @staticmethod
    def _shared_tasks() -> dict[str, asyncio.Task]:
        """Keep the registry on asyncio so it survives a module hot reload."""
        registry = getattr(asyncio, "_joker_background_task_registry", None)
        if registry is None:
            registry = {}
            setattr(asyncio, "_joker_background_task_registry", registry)
        return registry
