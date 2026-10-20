import queue
import threading
import time


class Task:
    __slots__ = ("task_type", "task_id", "fn", "args", "kwargs", "enqueued_at")
    def __init__(self, task_type, task_id, fn, args=None, kwargs=None):
        self.task_type = task_type
        self.task_id = task_id
        self.fn = fn
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.enqueued_at = time.time()


class QueueWorker(threading.Thread):
    def __init__(self, name, q):
        super().__init__(daemon=True, name=f"worker-{name}")
        self._q = q

    def run(self):
        while True:
            task = self._q.get()
            try:
                task.fn(*task.args, **task.kwargs)
            except Exception:
                pass
            finally:
                self._q.task_done()


class TaskScheduler:
    def __init__(self):
        self._queues = {}

    def create_queue(self, name, worker_count=1):
        q = queue.Queue()
        self._queues[name] = q
        for _ in range(worker_count):
            QueueWorker(name, q).start()
        return q

    def enqueue(self, queue_name, task):
        q = self._queues.get(queue_name)
        if q is None:
            raise KeyError(f"Queue {queue_name} not found")
        q.put(task)


scheduler = TaskScheduler()
scheduler.create_queue("create", worker_count=1)
scheduler.create_queue("delete", worker_count=1)
scheduler.create_queue("deploy", worker_count=2)
