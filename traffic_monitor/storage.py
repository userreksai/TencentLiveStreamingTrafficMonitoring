"""Atomic durable state and a process-scoped cross-platform lock."""

from contextlib import contextmanager
import json
import os
from pathlib import Path


class StateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, path, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        self.state = {"version": 1, "accounts": {}, "outbox": [], "errors": {}, "reports": {}}

    def load(self):
        if not self.path.exists():
            return self.state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (state.get("version") != 1 or not isinstance(state.get("accounts"), dict)
                    or not isinstance(state.get("outbox"), list) or not isinstance(state.get("errors"), dict)
                    or not isinstance(state.get("reports"), dict)):
                raise ValueError()
            for item in state["outbox"]:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or item.get("route") not in ("normal", "error") or not isinstance(item.get("text"), str)
                        or any(not isinstance(item.get(k), int) or item[k] < 0
                               for k in ("created_at", "next_attempt", "attempts"))):
                    raise ValueError()
            if any(not isinstance(v, dict) for v in state["accounts"].values()):
                raise ValueError()
            if any(not isinstance(v, dict) for v in state["errors"].values()):
                raise ValueError()
            if any(not isinstance(v, int) for v in state["reports"].values()):
                raise ValueError()
            self.state = state
        except (ValueError, AttributeError):
            raise StateError("状态文件损坏或版本不兼容，请保留文件排查，不会自动覆盖") from None
        return self.state

    def save(self):
        if self.read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(self.path))

    @contextmanager
    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise StateError("已有进程占用此状态文件，请勿同时运行多个监控服务") from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
