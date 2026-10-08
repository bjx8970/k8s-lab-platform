"""Technical, single-resource handler contracts and a fault-injectable fake."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecutionResult:
    phase: str
    external_task_ref: str | None = None
    exec_data: dict = field(default_factory=dict)
    output: dict | None = None
    error_code: str | None = None
    existence_state: str | None = None


class OutcomeUnknown(Exception):
    pass


class HandlerRegistry:
    def __init__(self):
        self._handlers = {}

    def register(self, plugin_id, plugin_version, driver_id, handler):
        key = (plugin_id, plugin_version, driver_id)
        if key in self._handlers:
            raise ValueError("重复的插件驱动版本")
        self._handlers[key] = handler

    def get(self, plugin_id, plugin_version, driver_id):
        try:
            return self._handlers[(plugin_id, plugin_version, driver_id)]
        except KeyError:
            raise ValueError("插件或驱动版本不可用") from None


class FakeHandler:
    actions = frozenset({"create", "observe", "start", "stop", "delete"})
    read_only_actions = frozenset({"observe"})

    def __init__(self):
        self.calls = []
        self.execute_results = []
        self.poll_results = []

    def execute(self, target, action, parameters):
        self.calls.append(("execute", target["resourceId"], action))
        if not self.execute_results:
            raise AssertionError("FakeHandler 缺少 execute 结果")
        result = self.execute_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result(target, action, parameters) if callable(result) else result

    def poll(self, target, external_task_ref, exec_data):
        self.calls.append(("poll", target["resourceId"], external_task_ref))
        if not self.poll_results:
            raise AssertionError("FakeHandler 缺少 poll 结果")
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result(target, external_task_ref, exec_data) if callable(result) else result
