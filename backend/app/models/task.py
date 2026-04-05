"""
Task Status Management
Tracks long-running tasks (like graph building)

Phase 14 — optional disk persistence (GRAPH_JOB_PERSIST_DIR) for poll-after-restart.
"""

import json
import logging
import os
import uuid
import threading
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger("mirofish.task")


class TaskStatus(str, Enum):
    """Task status enumeration"""
    PENDING = "pending"          # Pending
    PROCESSING = "processing"    # Processing
    COMPLETED = "completed"      # Completed
    FAILED = "failed"            # Failed


@dataclass
class Task:
    """Task data class"""
    task_id: str
    task_type: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    progress: int = 0              # Overall progress percentage 0-100
    message: str = ""              # Status message
    result: Optional[Dict] = None  # Task result
    error: Optional[str] = None    # Error message
    metadata: Dict = field(default_factory=dict)  # Additional metadata
    progress_detail: Dict = field(default_factory=dict)  # Detailed progress information

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "progress": self.progress,
            "message": self.message,
            "progress_detail": self.progress_detail,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Task":
        """Rehydrate from ``to_dict`` / JSON snapshot."""
        def _parse_dt(s: str) -> datetime:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)

        return Task(
            task_id=data["task_id"],
            task_type=data["task_type"],
            status=TaskStatus(data["status"]),
            created_at=_parse_dt(data["created_at"]),
            updated_at=_parse_dt(data["updated_at"]),
            progress=int(data.get("progress", 0)),
            message=str(data.get("message", "")),
            result=data.get("result"),
            error=data.get("error"),
            metadata=dict(data.get("metadata") or {}),
            progress_detail=dict(data.get("progress_detail") or {}),
        )


class TaskManager:
    """
    Task Manager
    Thread-safe task status management
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks: Dict[str, Task] = {}
                    cls._instance._task_lock = threading.Lock()
        return cls._instance

    def _persist_base(self) -> str:
        from ..config import Config

        return (Config.GRAPH_JOB_PERSIST_DIR or "").strip()

    def _persist_path(self, task_id: str) -> Optional[str]:
        base = self._persist_base()
        if not base:
            return None
        return os.path.join(base, f"{task_id}.json")

    def _persist_snapshot(self, task: Task) -> None:
        path = self._persist_path(task.task_id)
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("Could not persist task %s: %s", task.task_id, e)

    def _load_from_disk(self, task_id: str) -> Optional[Task]:
        path = self._persist_path(task_id)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return Task.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Could not load task %s from disk: %s", task_id, e)
            return None

    def create_task(self, task_type: str, metadata: Optional[Dict] = None) -> str:
        """
        Create new task

        Args:
            task_type: Task type
            metadata: Additional metadata

        Returns:
            Task ID
        """
        task_id = str(uuid.uuid4())
        now = datetime.now()

        task = Task(
            task_id=task_id,
            task_type=task_type,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            metadata=metadata or {}
        )

        with self._task_lock:
            self._tasks[task_id] = task

        self._persist_snapshot(task)

        # Periodic memory-bound cleanup of finished tasks (fork pattern: short TTL)
        created = getattr(self, "_tasks_created_count", 0) + 1
        self._tasks_created_count = created
        if created % 100 == 0:
            self.cleanup_old_tasks(max_age_hours=1)

        return task_id

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task (memory, then optional disk snapshot)."""
        with self._task_lock:
            t = self._tasks.get(task_id)
        if t is not None:
            return t
        loaded = self._load_from_disk(task_id)
        if loaded is None:
            return None
        with self._task_lock:
            if task_id not in self._tasks:
                self._tasks[task_id] = loaded
            return self._tasks.get(task_id)

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        progress_detail: Optional[Dict] = None
    ):
        """
        Update task status

        Args:
            task_id: Task ID
            status: New status
            progress: Progress
            message: Message
            result: Result
            error: Error message
            progress_detail: Detailed progress information
        """
        task_copy: Optional[Task] = None
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task:
                task.updated_at = datetime.now()
                if status is not None:
                    task.status = status
                if progress is not None:
                    task.progress = progress
                if message is not None:
                    task.message = message
                if result is not None:
                    task.result = result
                if error is not None:
                    task.error = error
                if progress_detail is not None:
                    task.progress_detail = progress_detail
                task_copy = task
        if task_copy is not None:
            self._persist_snapshot(task_copy)

    def complete_task(self, task_id: str, result: Dict):
        """Mark task as completed"""
        self.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            progress=100,
            message="Task completed",
            result=result
        )

    def fail_task(self, task_id: str, error: str):
        """Mark task as failed"""
        self.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message="Task failed",
            error=error
        )

    def list_tasks(self, task_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tasks currently in memory."""
        with self._task_lock:
            tasks = list(self._tasks.values())
            if task_type:
                tasks = [t for t in tasks if t.task_type == task_type]
            return [t.to_dict() for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)]

    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """Clean up old tasks"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        with self._task_lock:
            old_ids = [
                tid for tid, task in self._tasks.items()
                if task.created_at < cutoff and task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]
            ]
            for tid in old_ids:
                del self._tasks[tid]
                p = self._persist_path(tid)
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
