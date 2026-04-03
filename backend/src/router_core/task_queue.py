from __future__ import annotations

from router_core.domain import SessionState, Task, TaskStatus


def sort_tasks(tasks: list[Task], priorities: dict[str, int]) -> list[Task]:
    return sorted(
        tasks,
        key=lambda task: (priorities.get(task.intent_code, 0), task.confidence),
        reverse=True,
    )


def queue_pending_tasks(session: SessionState, priorities: dict[str, int]) -> None:
    queued = [task for task in session.tasks if task.status in {TaskStatus.CREATED, TaskStatus.QUEUED}]
    ordered = sort_tasks(queued, priorities)
    for task in ordered:
        task.touch(TaskStatus.QUEUED)


def next_runnable_task(session: SessionState, priorities: dict[str, int]) -> Task | None:
    queued = [task for task in session.tasks if task.status == TaskStatus.QUEUED]
    if not queued:
        return None
    return sort_tasks(queued, priorities)[0]


def waiting_task(session: SessionState) -> Task | None:
    for task in session.tasks:
        if task.status == TaskStatus.WAITING_USER_INPUT:
            return task
    return None

