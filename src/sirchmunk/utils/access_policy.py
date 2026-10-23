# Copyright (c) ModelScope Contributors. All rights reserved.
"""请求级文件授权；独立于检索相关性、文件类型和传输协议。"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


class AccessDenied(PermissionError):
    """统一拒绝错误，不向调用方披露越界路径或文件是否存在。"""

    def __init__(self) -> None:
        super().__init__("Access denied: path is outside the authorized search scope")


def _canonical(path: Any) -> Path:
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise AccessDenied()
    raw = str(path).strip()
    if "\x00" in raw or raw.startswith("-") or "://" in raw:
        raise AccessDenied()
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise AccessDenied() from exc


@dataclass(frozen=True)
class AccessPolicy:
    """每层是授权根的并集，多层取交集；嵌套请求只能收紧权限。"""

    layers: tuple[tuple[tuple[str, bool], ...], ...]

    def __post_init__(self) -> None:
        if not self.layers or any(not layer for layer in self.layers):
            raise AccessDenied()

    @classmethod
    def from_paths(cls, paths: Any) -> "AccessPolicy":
        roots = []
        for raw in paths:
            path = _canonical(raw)
            roots.append((str(path), path.is_dir()))
        if not roots:
            raise AccessDenied()
        return cls((tuple(sorted(set(roots))),))

    def allows(self, raw: Any) -> bool:
        try:
            target = _canonical(raw)
            return all(any(
                target == Path(root) or (directory and target.is_relative_to(root))
                for root, directory in layer
            ) for layer in self.layers)
        except (AccessDenied, OSError, ValueError):
            return False

    def require(self, raw: Any) -> str:
        if not self.allows(raw):
            raise AccessDenied()
        return str(_canonical(raw))

    def intersect(self, other: "AccessPolicy") -> "AccessPolicy":
        return AccessPolicy(self.layers + other.layers)

    def narrow(self, paths: Any) -> "AccessPolicy":
        validated = [self.require(path) for path in paths]
        return self.intersect(AccessPolicy.from_paths(validated))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.layers).encode("utf-8")).hexdigest()


_ACCESS: contextvars.ContextVar[Optional[AccessPolicy]] = contextvars.ContextVar(
    "sirchmunk_file_access", default=None,
)


def current_policy() -> Optional[AccessPolicy]:
    return _ACCESS.get()


@contextmanager
def bind_access(policy: Optional[AccessPolicy]) -> Iterator[None]:
    """异常和取消后复原；None 不得解除外层网络请求的限制。"""
    parent = current_policy()
    effective = parent.intersect(policy) if parent and policy else parent or policy
    token = _ACCESS.set(effective)
    try:
        yield
    finally:
        _ACCESS.reset(token)


def policy_from_env(work_path: Any = None, *, strict: bool = False) -> Optional[AccessPolicy]:
    raw = os.getenv("SIRCHMUNK_ALLOWED_PATHS", "").strip()
    if not strict and not raw:
        return None
    work = _canonical(work_path or os.getenv("SIRCHMUNK_WORK_PATH", "~/.sirchmunk"))
    roots = [part.strip() for part in raw.split(",") if part.strip()]
    for name in ("data", "uploads"):
        default = work / name
        # 默认目录的符号链接不得隐式扩大工作目录权限。
        if default.resolve().is_relative_to(work):
            roots.append(default)
    return AccessPolicy.from_paths(roots)


def resolve_search_paths(paths: Any, defaults: Any = None, *, work_path: Any = None,
                         strict: bool = False) -> Any:
    """先选有效路径，再整批授权；显式越权输入不能降级到默认路径。"""
    def clean(value: Any) -> list[str]:
        if value is None:
            return []
        values = [value] if isinstance(value, (str, Path)) else value
        if not isinstance(values, (list, tuple)):
            raise AccessDenied()
        if any(not isinstance(item, (str, Path)) for item in values):
            raise AccessDenied()
        return [str(item).strip() for item in values if str(item).strip()]

    chosen = clean(paths) or clean(defaults)
    if not chosen:
        chosen = clean([p for p in os.getenv("SIRCHMUNK_SEARCH_PATHS", "").split(",") if p.strip()])
    if not chosen and strict:
        work = _canonical(work_path or os.getenv("SIRCHMUNK_WORK_PATH", "~/.sirchmunk"))
        chosen = [str(work / name) for name in ("data", "uploads") if (work / name).is_dir()]
        if not chosen:
            raise AccessDenied()
    policy = current_policy() or policy_from_env(work_path, strict=strict)
    if policy:
        chosen = [policy.require(path) for path in chosen]
        if not chosen:
            raise AccessDenied()
    return chosen or None


def source_allowed(path: Any) -> bool:
    policy = current_policy()
    return policy is None or policy.allows(path)


def require_source(path: Any) -> Any:
    policy = current_policy()
    return policy.require(path) if policy else path


@contextmanager
def open_source(path: Any, mode: str = "r", *, encoding: str = "utf-8",
                errors: str = "replace") -> Iterator[Any]:
    """受限文本读取通过目录描述符逐级打开，拒绝检查后的符号链接替换。"""
    if mode not in ("r", "rb"):
        raise ValueError("Source access is read-only")
    target = require_source(path)
    options = {} if mode == "rb" else {"encoding": encoding, "errors": errors}
    if current_policy() is None or os.name != "posix":
        with open(target, mode, **options) as stream:
            yield stream
        return
    canonical = Path(target)
    directory_fd = os.open(canonical.anchor, os.O_RDONLY | os.O_DIRECTORY)
    file_fd = None
    try:
        for part in canonical.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(canonical.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        stream = os.fdopen(file_fd, mode, **options)
        file_fd = None  # 描述符所有权交给 stream。
        with stream:
            yield stream
    except OSError as exc:
        raise AccessDenied() from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def read_source_text(path: Any, *, encoding: str = "utf-8", errors: str = "replace") -> str:
    with open_source(path, encoding=encoding, errors=errors) as stream:
        return stream.read()


def cache_partition(key: str) -> str:
    """受限缓存分区；本地旧键保持原样。"""
    policy = current_policy()
    return f"{key}:{policy.fingerprint}" if policy else key


def _field(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def cached_sources_allowed(obj: Any) -> bool:
    """整对象检查来源；不删片段后继续复用已混合的摘要或答案。"""
    if current_policy() is None:
        return True
    sources: list[Any] = []
    raw = _field(obj, "search_results", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return False
    if raw:
        if not isinstance(raw, (list, tuple)):
            return False
        sources.extend(raw)
    for resource in _field(obj, "resources", []) or []:
        if _field(resource, "type") in ("file", "url"):
            source = _field(resource, "value")
            if not source:
                return False
            sources.append(source)
    for evidence in _field(obj, "evidences", []) or []:
        source = _field(evidence, "file_or_url")
        if not source:
            return False
        sources.append(source)
    direct = _field(obj, "file_path") or _field(obj, "path")
    if direct:
        sources.append(direct)
    return bool(sources) and all(source_allowed(source) for source in sources)


def filter_retrieval_results(results: list[dict]) -> list[dict]:
    """检索返回的真实来源在进入证据、日志和模型之前再次授权。"""
    if current_policy() is None:
        return results
    allowed = []
    for item in results:
        path = item.get("path") or item.get("data", {}).get("path", {}).get("text")
        if path and source_allowed(path):
            allowed.append(item)
    return allowed


def guard_source(parameter: str) -> Callable:
    """在函数异常捕获之前授权并规范化源路径，保留原函数签名。"""
    def decorate(function: Callable) -> Callable:
        signature = inspect.signature(function)

        def arguments(args: tuple, kwargs: dict) -> inspect.BoundArguments:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            value = bound.arguments[parameter]
            if current_policy() is not None:
                def checked(path: Any) -> Any:
                    canonical = require_source(path)
                    return Path(canonical) if isinstance(path, Path) else canonical
                if isinstance(value, (list, tuple)):
                    bound.arguments[parameter] = [checked(p) for p in value]
                else:
                    bound.arguments[parameter] = checked(value)
            return bound

        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if current_policy() is None:
                    return await function(*args, **kwargs)
                bound = arguments(args, kwargs)
                return await function(*bound.args, **bound.kwargs)
            return async_wrapper

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if current_policy() is None:
                return function(*args, **kwargs)
            bound = arguments(args, kwargs)
            return function(*bound.args, **bound.kwargs)
        return wrapper
    return decorate


def scoped_search(function: Callable) -> Callable:
    """核心检索只继承显式绑定的权限，不改变本地 SDK 的默认行为。"""
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        policy = current_policy()
        if policy is None:
            return await function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        searcher = bound.arguments["self"]
        paths = searcher._resolve_paths(bound.arguments["paths"])
        paths = [policy.require(path) for path in paths]
        bound.arguments["paths"] = paths
        with bind_access(policy.narrow(paths)):
            return await function(*bound.args, **bound.kwargs)
    return wrapper
