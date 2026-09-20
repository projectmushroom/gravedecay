"""Owner-private, retry-safe console actions. No command is replayed on recovery."""
import codecs
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import uuid

RETENTION = 7 * 86400
KEY_AGE = 86400
MAX_RECORDS = 512
MAX_OUTPUT = 128 * 1024
MAX_EVENTS = 512
ACTIVE = ("queued", "running")
BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class OperationError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class ActionLock:
    """One exclusion boundary for legacy actions, durable actions and orphans."""
    def __init__(self, path):
        self.path = Path(path)
        self.mutex = threading.Lock()
        self.guard = None

    def acquire(self, blocking=False):
        if not self.mutex.acquire(blocking=blocking):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            self.guard = os.fdopen(fd, "a")
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            self.guard.close()
            self.guard = None
            self.mutex.release()
            return False
        except Exception:
            if self.guard:
                self.guard.close()
                self.guard = None
            self.mutex.release()
            raise

    def release(self):
        # Close, don't LOCK_UN: a surviving child inherits the same flock.
        self.guard.close()
        self.guard = None
        self.mutex.release()


class Store:
    def __init__(self, path, action_lock, timeout=1200):
        self.path = Path(path)
        self.action_lock = action_lock
        self.timeout = timeout
        self.instance = uuid.uuid4().hex
        self.mutex = threading.Lock()
        self.active = set()

    def _id(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}-[0-9a-f]{32}", value):
            raise OperationError(400, "invalid_id", "Use <Unix seconds>-<32 lowercase UUID hex digits>")
        return self.path / (value + ".json")

    def _read(self, path):
        try:
            with path.open() as stream:
                record = json.loads(stream.read(2 * 1024 * 1024))
            if (not isinstance(record, dict) or self._id(record["id"]) != path
                    or record["state"] not in (*ACTIVE, "succeeded", "failed", "timed_out", "interrupted")
                    or not isinstance(record["action"], str) or not isinstance(record["instance"], str)
                    or not isinstance(record["events"], list) or len(record["events"]) > MAX_EVENTS
                    or not {"created_at", "finished_at", "exit_code", "cursor", "bytes", "truncated", "message"} <= record.keys()
                    or any(not isinstance(e["seq"], int) or not isinstance(e["text"], str) for e in record["events"])
                    or sum(len(e["text"].encode("utf-8")) for e in record["events"]) > MAX_OUTPUT):
                raise ValueError("Invalid operation journal")
            return record
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, OperationError) as error:
            raise OSError("Unreadable operation journal") from error

    def _write(self, record):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.path, prefix=".operation-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._id(record["id"]))
            directory = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _recover(self, record):
        if record["state"] in ACTIVE and (record["instance"] != self.instance or record["id"] not in self.active):
            record.update(state="interrupted", finished_at=time.time(),
                          message="Dashboard restarted; outcome unknown. Check the grave before starting another action.")
            self._write(record)
        return record

    @staticmethod
    def _public(record, after=0):
        return {k: v for k, v in record.items() if k not in ("instance", "events", "bytes")} | {
            "events": [event for event in record["events"] if event["seq"] > after]}

    def get(self, operation_id, after=0):
        path = self._id(operation_id)
        if not isinstance(after, int) or after < 0:
            raise OperationError(400, "invalid_cursor", "Cursor must be a nonnegative integer")
        with self.mutex:
            record = self._read(path)
            if record is None:
                raise OperationError(404, "not_found", "Operation not found or history expired; it has not been restarted")
            return self._public(self._recover(record), after)

    def health(self):
        # Read-only doctor probe: no action, log contents or recovery side effects.
        records = list(self.path.glob("*.json"))
        for path in records:
            record = self._read(path)
            assert record and self._id(record["id"]) == path
            assert record["state"] in (*ACTIVE, "succeeded", "failed", "timed_out", "interrupted")
            assert len(record["events"]) <= MAX_EVENTS
            assert sum(len(e["text"].encode("utf-8")) for e in record["events"]) <= MAX_OUTPUT
        return {"ok": True, "protocol": 1, "records": len(records), "build": BUILD_ID}

    def start(self, operation_id, action, cmd):
        path = self._id(operation_id)
        with self.mutex:
            record = self._read(path)
            if record:
                if record["action"] != action:
                    raise OperationError(409, "id_conflict", "This operation ID belongs to a different action")
                return self._public(self._recover(record)), False
            now = time.time()
            if not now - KEY_AGE <= int(operation_id.split("-")[0]) <= now + 300:
                raise OperationError(409, "expired_id", "Unknown operation ID is too old or in the future; check history and device clocks")
            if not self.action_lock.acquire(blocking=False):
                raise OperationError(409, "busy", "Another action is running; no new operation was started")
            try:
                records = list(self.path.glob("*.json"))
                for old in records:
                    if int(old.stem.split("-")[0]) < now - RETENTION:
                        old.unlink()
                if len(list(self.path.glob("*.json"))) >= MAX_RECORDS:
                    raise OperationError(503, "history_full", "Operation history is full; try after old records expire")
                record = {"id": operation_id, "action": action, "state": "queued", "instance": self.instance,
                          "created_at": now, "finished_at": None, "exit_code": None,
                          "cursor": 0, "events": [], "bytes": 0, "truncated": False, "message": ""}
                self._write(record)
                worker = threading.Thread(target=self._run, args=(record, list(cmd)), daemon=True)
                self.active.add(operation_id)
                worker.start()
                return self._public(record), True
            except Exception:
                try:
                    if path.exists():
                        record.update(state="failed", finished_at=time.time(), message="Could not start action")
                        self._write(record)
                finally:
                    self.active.discard(operation_id)
                    self.action_lock.release()
                raise

    def _run(self, record, cmd):
        process = None
        try:
            with self.mutex:
                record["state"] = "running"
                self._write(record)
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=(self.action_lock.guard.fileno(),))
            deadline = time.monotonic() + self.timeout
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            with selectors.DefaultSelector() as selector, process.stdout:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fd, 4096)
                        text = decoder.decode(chunk, final=not chunk)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        with self.mutex:
                            if text and not record["truncated"]:
                                text = text.encode("utf-8")[:MAX_OUTPUT - record["bytes"]].decode("utf-8", "ignore")
                                record["bytes"] += len(text.encode("utf-8"))
                                record["cursor"] += 1
                                record["events"].append({"seq": record["cursor"], "text": text})
                                record["truncated"] = record["bytes"] >= MAX_OUTPUT - 3 or len(record["events"]) >= MAX_EVENTS
                                self._write(record)
                rc = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            with self.mutex:
                record.update(state="succeeded" if rc == 0 else "failed", exit_code=rc)
        except (TimeoutError, subprocess.TimeoutExpired):
            with self.mutex:
                record.update(state="timed_out", message="Action exceeded its time limit; check the grave before retrying")
        except Exception:
            with self.mutex:
                record.update(state="failed", message="Action runner failed; check the grave before retrying")
        finally:
            try:
                if process:
                    # Also stop descendants holding the output pipe after the
                    # main command exits. Never release the lock before cleanup.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    if process.stdout:
                        process.stdout.close()
                with self.mutex:
                    record["finished_at"] = time.time()
                    self._write(record)
            finally:
                with self.mutex:
                    self.active.discard(record["id"])
                self.action_lock.release()
