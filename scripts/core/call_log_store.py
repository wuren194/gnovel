"""Private, bounded diagnostic storage; none of these files are project state."""

from __future__ import annotations

import errno
import codecs
import fcntl
import hashlib
import io
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .call_log_redaction import json_bytes


_CALL_ID = re.compile(r"CL-[0-9a-f]{32}\Z")
_PAYLOAD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_METADATA_CACHE = ".metadata-cache.json"


class CallLogStoreError(Exception):
    """A static code is safe to report without leaking paths or payloads."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise CallLogStoreError("LOG_DIRECTORY_UNSAFE")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CallLogStoreError("LOG_DIRECTORY_PERMISSIONS")
    return True


def _private_directory(path: Path) -> None:
    if _check_directory(path):
        return
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    if not _check_directory(path):
        raise CallLogStoreError("LOG_DIRECTORY_UNAVAILABLE")


def _check_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CallLogStoreError("LOG_FILE_UNSAFE")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CallLogStoreError("LOG_FILE_PERMISSIONS")


def _read_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _check_file(info)
        if info.st_size > limit:
            raise CallLogStoreError("LOG_FILE_TOO_LARGE")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise CallLogStoreError("LOG_FILE_TOO_LARGE")
        return data
    finally:
        os.close(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes, *, replace: bool = False) -> None:
    """Atomic publication without overwriting an earlier event or reference."""
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # A link publishes the complete inode and fails if the destination
        # already exists. This also prevents a repeated finish from rewriting
        # a previously recorded result.
        if replace:
            try:
                _check_file(path.lstat())
            except FileNotFoundError:
                pass
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        _sync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _timestamp(value: Any, fallback: float) -> float:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return fallback


class CallLogStore:
    """One immutable start and finish per call, with local payload references.

    Read methods never create directories, files, or locks. Values received by
    the write methods must already be sanitized copies; the store only knows
    their encoding and capture metadata, never the original objects.
    """

    def __init__(self, root: Path, policy: dict) -> None:
        raw = root.expanduser().absolute()
        # Normalize ancestor aliases (notably macOS /var) without following a
        # symlink at the chosen diagnostics directory itself.
        self.root = raw.parent.resolve() / raw.name
        self.calls = self.root / "calls"
        self.policy = policy

    @contextmanager
    def _lock(self, *, create: bool = False):
        if create:
            _private_directory(self.root)
            _private_directory(self.calls)
        elif not _check_directory(self.root) or not _check_directory(self.calls):
            raise CallLogStoreError("LOG_STORE_MISSING")
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _check_file(os.fstat(fd))
            deadline = time.monotonic() + self.policy["lockTimeoutSeconds"]
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CallLogStoreError("LOG_STORE_BUSY") from exc
                    time.sleep(min(self.policy["lockRetrySeconds"], remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _call_path(self, call_id: str) -> Path:
        if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
            raise CallLogStoreError("LOG_CALL_ID_INVALID")
        return self.calls / call_id

    def read_config(self) -> dict | None:
        """Read only this store's config; absence never initializes storage."""
        if not _check_directory(self.root):
            return None
        try:
            result = json.loads(_read_file(self.root / "config.json", self.policy["maxPayloadBytes"]))
        except FileNotFoundError:
            return None
        except (OSError, CallLogStoreError) as exc:
            raise CallLogStoreError("LOG_CONFIG_UNREADABLE") from exc
        except (ValueError, RecursionError) as exc:
            raise CallLogStoreError("LOG_CONFIG_INVALID") from exc
        if not isinstance(result, dict):
            raise CallLogStoreError("LOG_CONFIG_INVALID")
        return result

    def write_config(self, data: dict) -> None:
        """Publish an explicit user configuration under the storage lock."""
        encoded = json_bytes(data)
        with self._lock(create=True):
            _write_new(self.root / "config.json", encoded, replace=True)

    def _payloads(self, event: str, payloads: dict, mode: str) -> tuple[dict, dict[str, bytes]]:
        descriptors, blobs = {}, {}
        if mode not in {"summary", "full"}:
            raise CallLogStoreError("LOG_MODE_INVALID")
        for name, captured in payloads.items():
            if not isinstance(name, str) or not _PAYLOAD_NAME.fullmatch(name):
                raise CallLogStoreError("LOG_PAYLOAD_NAME_INVALID")
            content = json_bytes(captured["value"])
            descriptor = {
                "capture": "full" if captured["complete"] else "partial",
                "storage": "none",
                "originalBytes": captured["originalBytes"],
                "storedBytes": 0,
                "sanitizedBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "redactions": captured["redactions"],
                "issues": list(captured["issues"]),
            }
            if mode == "summary":
                descriptor["capture"] = "summary"
            elif len(content) > self.policy["maxPayloadBytes"]:
                descriptor["capture"] = "omitted"
                descriptor["issues"].append("payload_size_limit")
            else:
                descriptor["storedBytes"] = len(content)
                if len(content) <= self.policy["inlinePayloadBytes"]:
                    descriptor.update(storage="inline", value=captured["value"])
                else:
                    reference = f"{event}-{name}.json"
                    blobs[reference] = content
                    descriptor.update(storage="blob", ref=reference)
            descriptors[name] = descriptor
        return descriptors, blobs

    def start(self, call_id: str, metadata: dict, payloads: dict, mode: str) -> dict:
        return self._write_event(call_id, "start", metadata, payloads, mode)

    def finish(self, call_id: str, metadata: dict, payloads: dict, mode: str) -> dict:
        return self._write_event(call_id, "finish", metadata, payloads, mode)

    def _write_event(self, call_id: str, event: str, metadata: dict, payloads: dict, mode: str) -> dict:
        call_path = self._call_path(call_id)
        if len(json_bytes(metadata)) > self.policy["maxMetadataBytes"]:
            raise CallLogStoreError("LOG_METADATA_TOO_LARGE")
        descriptors, blobs = self._payloads(event, payloads, mode)
        record = {
            "schemaVersion": 1,
            "callId": call_id,
            "event": event,
            "metadata": metadata,
            "payloads": descriptors,
        }
        encoded = json_bytes(record)
        size = len(encoded) + sum(len(blob) for blob in blobs.values())
        with self._lock(create=event == "start"):
            if event == "start":
                if call_path.exists() or call_path.is_symlink():
                    raise CallLogStoreError("LOG_CALL_EXISTS")
            elif not _check_directory(call_path):
                raise CallLogStoreError("LOG_CALL_MISSING")
            if (call_path / f"{event}.json").exists():
                raise CallLogStoreError("LOG_EVENT_EXISTS")
            self._prune_locked(reserve_bytes=size, reserve_calls=int(event == "start"), exclude={call_id})
            if event == "start":
                call_path.mkdir(mode=0o700)
            for reference, data in blobs.items():
                _write_new(call_path / reference, data)
            _write_new(call_path / f"{event}.json", encoded)
            _sync_directory(self.calls)
        return record

    def _read_event(self, call_path: Path, event: str) -> tuple[dict | None, str | None]:
        try:
            record = json.loads(_read_file(call_path / f"{event}.json", self.policy["maxStoreBytes"]))
        except FileNotFoundError:
            return None, f"{event}_missing"
        except (OSError, CallLogStoreError, ValueError, RecursionError):
            return None, f"{event}_unreadable"
        if (
            not isinstance(record, dict) or record.get("schemaVersion") != 1
            or record.get("callId") != call_path.name or record.get("event") != event
            or not isinstance(record.get("metadata"), dict)
            or not isinstance(record.get("payloads"), dict)
        ):
            return None, f"{event}_invalid"
        return record, None

    def _read_pruning_event(self, call_path: Path, event: str) -> tuple[dict | None, str | None]:
        """Read the bounded metadata prefix, not an old inline manuscript.

        Only retention uses this fast path. Author-facing list/show still
        parse the complete event and verify payloads when requested.
        """
        if event == "finish":
            # Releasing active-call protection needs a complete event, not a
            # plausible prefix from a truncated file. A validated immutable
            # finish is cached against its real file signature thereafter.
            record, issue = self._read_event(call_path, event)
            if record and len(json_bytes(record["metadata"])) > self.policy["maxMetadataBytes"]:
                return None, f"{event}_metadata_invalid"
            return ({"metadata": record["metadata"], "payloads": {}} if record else None), issue
        prefix = (b'{"schemaVersion":1,"callId":' + json_bytes(call_path.name)
                  + b',"event":' + json_bytes(event) + b',"metadata":')
        separator = b',"payloads":'
        maximum = len(prefix) + self.policy["maxMetadataBytes"] + len(separator)
        try:
            fd = os.open(call_path / f"{event}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                _check_file(os.fstat(fd))
                content = b""
                decoder = json.JSONDecoder()
                while len(content) < maximum:
                    block = os.read(fd, min(io.DEFAULT_BUFFER_SIZE, maximum - len(content)))
                    if not block:
                        break
                    content += block
                    if len(content) < len(prefix):
                        continue
                    if not content.startswith(prefix):
                        # Formatting is not an authority boundary. A valid
                        # reformatted event remains readable from its bytes.
                        record, issue = self._read_event(call_path, event)
                        if record and len(json_bytes(record["metadata"])) <= self.policy["maxMetadataBytes"]:
                            return {"metadata": record["metadata"], "payloads": {}}, None
                        return None, issue or f"{event}_metadata_invalid"
                    try:
                        text = codecs.utf_8_decode(content[len(prefix):], "strict", False)[0]
                        metadata, end = decoder.raw_decode(text)
                    except (ValueError, UnicodeError):
                        continue
                    encoded_metadata = text[:end].encode("utf-8")
                    after = content[len(prefix) + len(encoded_metadata):]
                    if len(after) < len(separator):
                        continue
                    if (not isinstance(metadata, dict) or len(encoded_metadata) > self.policy["maxMetadataBytes"]
                            or not after.startswith(separator)):
                        return None, f"{event}_metadata_invalid"
                    return {"metadata": metadata, "payloads": {}}, None
            finally:
                os.close(fd)
        except FileNotFoundError:
            return None, f"{event}_missing"
        except (OSError, CallLogStoreError, ValueError, RecursionError):
            return None, f"{event}_unreadable"
        return None, f"{event}_metadata_invalid"

    @staticmethod
    def _file_signature(info: os.stat_result) -> list[int]:
        return [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                info.st_mode, info.st_uid, info.st_nlink]

    def _read_metadata_cache(self) -> dict:
        try:
            encoded = _read_file(self.root / _METADATA_CACHE, self.policy["maxStoreBytes"])
            prefix = b'{"schemaVersion":1,"sha256":"'
            delimiter = b'","entries":'
            digest_end = len(prefix) + hashlib.sha256().digest_size * 2
            if (not encoded.startswith(prefix) or encoded[digest_end:digest_end + len(delimiter)] != delimiter
                    or not encoded.endswith(b"}")):
                return {}
            body = encoded[digest_end + len(delimiter):-1]
            if hashlib.sha256(body).hexdigest().encode("ascii") != encoded[len(prefix):digest_end]:
                return {}
            entries = json.loads(body)
            return entries if isinstance(entries, dict) else {}
        except (OSError, CallLogStoreError, ValueError, KeyError, TypeError, RecursionError):
            return {}

    @staticmethod
    def _pruning_metadata(record: dict | None) -> dict | None:
        if record is None:
            return None
        metadata = record["metadata"]
        return {key: metadata[key] for key in ("pid", "startedAt") if key in metadata}

    @staticmethod
    def _cache_entry_matches(cached: Any, signature: dict) -> bool:
        return (isinstance(cached, dict) and cached.get("signature") == signature
                and "start" in cached and "finish" in cached
                and all(cached[event] is None or isinstance(cached[event], dict) for event in ("start", "finish"))
                and isinstance(cached.get("issues"), list))

    def _scan_call(self, directory: os.DirEntry, calls_fd: int, metadata_cache: dict | None, issues: list) -> dict:
        call_path = self.calls / directory.name
        info = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CallLogStoreError("LOG_DIRECTORY_UNSAFE")
        size = 0
        signature = {"start": None, "finish": None}
        call_fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=calls_fd)
        try:
            # Relative stat calls reuse the open directory instead of resolving
            # the complete user-local path for every historical event.
            with os.scandir(call_fd) as files:
                for item in files:
                    part = item.stat(follow_symlinks=False)
                    if item.name in {"start.json", "finish.json"}:
                        signature[item.name.removesuffix(".json")] = self._file_signature(part)
                    if not stat.S_ISREG(part.st_mode):
                        issues.append({"callId": directory.name, "code": "unsafe_entry"})
                        continue
                    size += part.st_size
        finally:
            os.close(call_fd)
        cached = metadata_cache.get(directory.name) if metadata_cache is not None else None
        if self._cache_entry_matches(cached, signature):
            start = {"metadata": cached["start"], "payloads": {}} if cached["start"] is not None else None
            finish = {"metadata": cached["finish"], "payloads": {}} if cached["finish"] is not None else None
            event_issues = cached["issues"]
        else:
            reader = self._read_pruning_event if metadata_cache is not None else self._read_event
            start, start_issue = reader(call_path, "start")
            finish, finish_issue = reader(call_path, "finish")
            event_issues = [item for item in (start_issue, finish_issue) if item]
        metadata = dict(start["metadata"]) if start else {}
        if finish:
            metadata.update(finish["metadata"])
        started = start["metadata"].get("startedAt") if start else metadata.get("startedAt")
        return {
            "path": call_path, "bytes": size, "start": start, "finish": finish,
            "metadata": metadata, "timestamp": _timestamp(started, info.st_mtime),
            "active": finish is None and _alive(metadata.get("pid")), "issues": event_issues,
            "cache": {"signature": signature, "start": self._pruning_metadata(start),
                      "finish": self._pruning_metadata(finish), "issues": event_issues},
        }

    def _scan(self, *, metadata_cache: dict | None = None) -> tuple[list[dict], list[dict], int]:
        if not _check_directory(self.root) or not _check_directory(self.calls):
            return [], [], 0
        entries, issues = [], []
        root_bytes = 0
        with os.scandir(self.root) as root_entries:
            for item in root_entries:
                if item.name != "calls":
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        root_bytes += info.st_size
        calls_fd = os.open(self.calls, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(calls_fd) as call_directories:
                for directory in call_directories:
                    if not _CALL_ID.fullmatch(directory.name):
                        issues.append({"code": "unknown_entry"})
                        unknown = directory.stat(follow_symlinks=False)
                        if stat.S_ISREG(unknown.st_mode):
                            root_bytes += unknown.st_size
                        continue
                    try:
                        entries.append(self._scan_call(directory, calls_fd, metadata_cache, issues))
                    except FileNotFoundError:
                        # A concurrent retention pass may have removed it.
                        continue
                    except (OSError, CallLogStoreError):
                        issues.append({"callId": directory.name, "code": "call_unreadable"})
        finally:
            os.close(calls_fd)
        return entries, issues, root_bytes

    @staticmethod
    def _descriptor_summary(descriptor: Any) -> Any:
        if not isinstance(descriptor, dict):
            return {"availability": "corrupt"}
        return {key: value for key, value in descriptor.items() if key != "value"}

    def list_calls(self) -> dict:
        entries, issues, _ = self._scan()
        calls = []
        for entry in sorted(entries, key=lambda item: item["timestamp"], reverse=True):
            metadata = entry["metadata"]
            status = metadata.get("status", "unknown") if entry["start"] and entry["finish"] else "incomplete"
            payloads = {}
            for event in ("start", "finish"):
                if entry[event]:
                    payloads[event] = {
                        name: self._descriptor_summary(descriptor)
                        for name, descriptor in entry[event]["payloads"].items()
                    }
            calls.append({
                **metadata, "callId": entry["path"].name, "status": status,
                "metadata": metadata, "storedBytes": entry["bytes"],
                "payloads": payloads, "issues": entry["issues"],
            })
        return {"calls": calls, "issues": issues}

    def _load_payload(self, call_path: Path, event: str, name: str, descriptor: Any) -> dict:
        if not isinstance(descriptor, dict):
            return {"availability": "corrupt"}
        result = dict(descriptor)
        storage = result.get("storage")
        if storage == "none":
            result["availability"] = "not_stored"
            return result
        try:
            if storage == "inline":
                data = json_bytes(result["value"])
            elif storage == "blob":
                if (
                    not isinstance(name, str) or not _PAYLOAD_NAME.fullmatch(name)
                    or result.get("ref") != f"{event}-{name}.json"
                ):
                    raise CallLogStoreError("LOG_REFERENCE_INVALID")
                data = _read_file(call_path / result["ref"], self.policy["maxPayloadBytes"])
            else:
                raise CallLogStoreError("LOG_STORAGE_INVALID")
            if len(data) != result.get("storedBytes") or hashlib.sha256(data).hexdigest() != result.get("sha256"):
                raise CallLogStoreError("LOG_PAYLOAD_CORRUPT")
            result["value"] = json.loads(data)
            result["availability"] = "available"
        except FileNotFoundError:
            result.pop("value", None)
            result["availability"] = "missing"
        except (OSError, CallLogStoreError, ValueError, TypeError, KeyError, RecursionError):
            result.pop("value", None)
            result["availability"] = "corrupt"
        return result

    def show(self, call_id: str, include_payloads: bool = True) -> dict:
        call_path = self._call_path(call_id)
        if not _check_directory(self.root) or not _check_directory(self.calls) or not _check_directory(call_path):
            raise CallLogStoreError("LOG_CALL_MISSING")
        start, start_issue = self._read_event(call_path, "start")
        finish, finish_issue = self._read_event(call_path, "finish")
        for event, record in (("start", start), ("finish", finish)):
            if record:
                record["payloads"] = {
                    name: self._load_payload(call_path, event, name, descriptor)
                    if include_payloads else self._descriptor_summary(descriptor)
                    for name, descriptor in record["payloads"].items()
                }
        metadata = dict(start["metadata"]) if start else {}
        if finish:
            metadata.update(finish["metadata"])
        return {
            "callId": call_id,
            "status": metadata.get("status", "unknown") if start and finish else "incomplete",
            "metadata": metadata,
            "start": start, "finish": finish,
            "issues": [item for item in (start_issue, finish_issue) if item],
        }

    def stats(self) -> dict:
        entries, issues, root_bytes = self._scan()
        return {
            "callCount": len(entries), "storedBytes": root_bytes + sum(entry["bytes"] for entry in entries),
            "incompleteCount": sum(entry["finish"] is None or entry["start"] is None for entry in entries),
            "sizeComplete": not issues, "issues": issues,
        }

    @staticmethod
    def _remove_call(path: Path) -> None:
        _check_directory(path)
        children = list(path.iterdir())
        if any(stat.S_ISDIR(child.lstat().st_mode) for child in children):
            raise CallLogStoreError("LOG_UNEXPECTED_DIRECTORY")
        # Unlinking an unexpected symlink removes only the link; never follow
        # it while reading contents or removing its external target.
        for child in children:
            child.unlink()
        path.rmdir()

    def _prune_locked(self, *, all_calls: bool = False, reserve_bytes: int = 0,
                      reserve_calls: int = 0, exclude: set[str] | None = None) -> dict:
        entries, issues, root_bytes = self._scan(metadata_cache=self._read_metadata_cache())
        if (reserve_bytes or reserve_calls) and issues:
            # Unreadable or unexpected inventory is not free capacity.
            raise CallLogStoreError("LOG_STORE_INVENTORY_INCOMPLETE")
        excluded = exclude or set()
        total_bytes = root_bytes + sum(entry["bytes"] for entry in entries)
        if total_bytes + reserve_bytes > self.policy["maxStoreBytes"]:
            # A derivative must never evict manuscript-bearing evidence just
            # to keep itself. This also permits lowering the configured cap.
            total_bytes -= self._discard_metadata_cache()
        count = len(entries)
        removed = []
        cutoff = time.time() - self.policy["retentionDays"] * 24 * 60 * 60
        for entry in sorted(entries, key=lambda item: item["timestamp"]):
            if entry["path"].name in excluded or entry["active"]:
                continue
            if entry["start"] is None and entry["finish"] is None and not all_calls:
                # Missing/unreadable ownership is not proof of a dead call.
                # An explicit purge may remove it; automatic retention does
                # not risk evicting an active call whose metadata was damaged.
                continue
            over_budget = (
                total_bytes + reserve_bytes > self.policy["maxStoreBytes"]
                or count + reserve_calls > self.policy["maxCalls"]
            )
            if not (all_calls or entry["timestamp"] < cutoff or over_budget):
                continue
            try:
                self._remove_call(entry["path"])
            except (OSError, CallLogStoreError):
                issues.append({"callId": entry["path"].name, "code": "prune_failed"})
                continue
            removed.append(entry["path"].name)
            total_bytes -= entry["bytes"]
            count -= 1
        if reserve_bytes or reserve_calls:
            if total_bytes + reserve_bytes > self.policy["maxStoreBytes"] or count + reserve_calls > self.policy["maxCalls"]:
                raise CallLogStoreError("LOG_CAPACITY_EXCEEDED")
        retained_cache = {entry["path"].name: entry["cache"] for entry in entries
                          if entry["path"].name not in removed}
        total_bytes += self._write_metadata_cache(retained_cache, total_bytes, reserve_bytes)
        if removed:
            _sync_directory(self.calls)
        return {
            "removedCallIds": removed, "removedCount": len(removed),
            "retainedCount": count, "storedBytes": total_bytes,
            "activeRetained": sum(entry["active"] for entry in entries), "issues": issues,
        }

    def _discard_metadata_cache(self) -> int:
        path = self.root / _METADATA_CACHE
        try:
            info = path.lstat()
        except FileNotFoundError:
            return 0
        _check_file(info)
        path.unlink()
        _sync_directory(self.root)
        return info.st_size

    def _write_metadata_cache(self, entries: dict, total_bytes: int, reserve_bytes: int) -> int:
        """Best-effort derived cache, accounted within the actual byte limit.

        Every retention scan still stats all real files. The cache only avoids
        reopening unchanged event metadata; it never proves payload contents
        or hides missing event files. A crash leaves signatures stale, causing
        the next scan to rebuild just those entries from the real events.
        """
        path = self.root / _METADATA_CACHE
        try:
            try:
                old_info = path.lstat()
                _check_file(old_info)
                old_bytes = old_info.st_size
            except FileNotFoundError:
                old_bytes = 0
            if not entries:
                if old_bytes:
                    path.unlink()
                return -old_bytes
            body = json_bytes(entries)
            encoded = (b'{"schemaVersion":1,"sha256":"' + hashlib.sha256(body).hexdigest().encode("ascii")
                       + b'","entries":' + body + b'}')
            delta = len(encoded) - old_bytes
            if total_bytes + reserve_bytes + delta > self.policy["maxStoreBytes"]:
                return 0
            _write_new(path, encoded, replace=True)
            return delta
        except (OSError, CallLogStoreError):
            # Cache failure cannot cancel a saved event. A leftover or stale
            # cache is rejected by shape/checksum/file-signature validation.
            return 0

    def prune(self, all_calls: bool = False) -> dict:
        if not _check_directory(self.root) or not _check_directory(self.calls):
            return {"removedCallIds": [], "removedCount": 0, "retainedCount": 0,
                    "storedBytes": 0, "activeRetained": 0, "issues": []}
        with self._lock():
            return self._prune_locked(all_calls=all_calls)
