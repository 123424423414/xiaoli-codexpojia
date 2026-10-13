#!/usr/bin/env python3
"""小李破甲 · Codex 指令配置引擎（Python 3.11+，仅标准库）。

职责：
  * 在 Codex 的 config.toml 中精确定位并写入 `model_instructions_file`
    （顶层键、profile 段内键、或 root 段的点号键三种写法都能定位）
  * 维护首次备份、安装状态、历史快照与恢复基线
  * 原子写 + 事务回滚 + 并发修改检测 + 陈旧锁提示

边界：不修改 Codex 程序、账号、模型设置或 API 密钥，不结束正在运行的 Codex，
不修改外部指令原文件（只在工具自己的目录里生成副本）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

KEY = "model_instructions_file"
TOOL_ID = "xiaoli-armor"
SCHEMA_VERSION = 1
BOM = b"\xef\xbb\xbf"
PROMPT_SUFFIX = ".md"
LOCK_STALE_SECONDS = 30 * 60
WRITE_RETRY_DELAYS = (0.0, 0.15, 0.5)


class ConfigError(ValueError):
    """配置无法安全处理：保留现场并停止。"""


class ConfigConflictError(ConfigError):
    """检测到并发修改或锁占用。"""


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 仅供调试
        return "<missing>"


_MISSING = _Missing()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        check = getattr(path, "is_junction", None)
        return bool(check()) if callable(check) else False
    except OSError:
        return False


def _fsync_dir(path: Path) -> None:
    try:
        handle = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


# --------------------------------------------------------------------------
# TOML 行级定位
# --------------------------------------------------------------------------


class _Lexer:
    """跟踪 TOML 字符串与容器状态，字符串里的“表头样子”文本不会被误判。"""

    def __init__(self) -> None:
        self.quote = ""
        self.depth = 0
        self.comment_at: int | None = None

    @property
    def idle(self) -> bool:
        return not self.quote and self.depth == 0

    def feed(self, line: str) -> None:
        i = 0
        while i < len(line):
            ch = line[i]
            if self.quote:
                if self.quote.startswith('"') and ch == "\\":
                    i += 2
                    continue
                if line.startswith(self.quote, i):
                    size = len(self.quote)
                    if size == 3:
                        while i + size < len(line) and line[i + size] == ch:
                            size += 1
                    self.quote = ""
                    i += size
                    continue
                i += 1
                continue
            if ch == "#":
                self.comment_at = i
                break
            if ch in "\"'":
                self.quote = ch * 3 if line.startswith(ch * 3, i) else ch
                i += len(self.quote)
                continue
            if ch in "[{":
                self.depth += 1
            elif ch in "]}":
                self.depth -= 1
            i += 1


def _without(node: object, keys: tuple[str, ...]) -> object:
    """复制配置数据，只去掉目标叶子键（同级其他键、数组表的其他元素都必须保留）。

    两处历史漏洞都在这里：
      * 早期版本直接删掉整个顶层节点，profiles.* 子树内的改动因此全部逃过「语义等价」检查；
      * 早期版本遇到数组表（[[items]] 解析成 list）直接原样返回，
        导致数组表内的合法替换被误判成「语义不一致」而拒绝写入。
    """
    if not keys:
        return node
    if isinstance(node, list):
        return [_without(item, keys) if isinstance(item, dict) else item for item in node]
    if not isinstance(node, dict):
        return node
    name, rest = keys[0], keys[1:]
    if name not in node:
        return node
    if not rest:
        out = dict(node)
        out.pop(name, None)
        return out
    out = dict(node)
    out[name] = _without(node[name], rest)
    return out


def _leaf_paths(node: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(node, dict):
        out: list[tuple[str, ...]] = []
        for name, value in node.items():
            out.extend(_leaf_paths(value, prefix + (str(name),)))
        return out
    if isinstance(node, list) and node:
        return _leaf_paths(node[-1], prefix)
    return [prefix]


def _probe_keys(statement: str) -> tuple[tuple[str, ...], str] | None:
    """把 `键 = 值` 语句解析成（规范化键路径, 原键文本）；不是赋值语句时返回 None。"""
    quote = ""
    i = 0
    while i < len(statement):
        ch = statement[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return None
        elif ch == "=":
            head = statement[:i]
            try:
                parsed = tomllib.loads(head + " = 0")
            except tomllib.TOMLDecodeError:
                return None
            paths = _leaf_paths(parsed)
            if len(paths) != 1:
                return None
            # 键文本去掉两端空白：缩进由 Assignment.indent 单独记录，
            # 否则回写时会变成双缩进（Tab 缩进尤其明显）。
            return paths[0], head.strip()
        i += 1
    return None


def _header_keys(line: str) -> tuple[str, ...] | None:
    body = line.strip()
    if not body.startswith("["):
        return None
    try:
        parsed = tomllib.loads(body + "\n__probe__ = 0\n")
    except tomllib.TOMLDecodeError:
        return None
    paths = _leaf_paths(parsed)
    if len(paths) != 1 or not paths[0] or paths[0][-1] != "__probe__":
        return None
    return paths[0][:-1]


@dataclass(frozen=True)
class Header:
    keys: tuple[str, ...]
    start: int
    end: int
    array: bool


@dataclass(frozen=True)
class Assignment:
    keys: tuple[str, ...]
    start: int
    end: int
    raw: str
    key_text: str
    indent: str
    comment: str = ""


@dataclass(frozen=True)
class Scan:
    assignments: list[Assignment]
    headers: list[Header]


def scan_toml(text: str) -> Scan:
    lexer = _Lexer()
    assignments: list[Assignment] = []
    headers: list[Header] = []
    offset = 0
    start = 0
    table: tuple[str, ...] = ()
    for line in text.splitlines(keepends=True):
        if lexer.idle:
            stripped = line.lstrip(" \t")
            if stripped.startswith("["):
                keys = _header_keys(line)
                if keys is None:
                    raise ConfigError(f"无法解析表头，未修改配置：{stripped.strip()}")
                header_start = offset
                lexer.comment_at = None
                lexer.feed(line)
                offset += len(line)
                if not lexer.idle:
                    raise ConfigError(f"表头未正常结束，未修改配置：{stripped.strip()}")
                headers.append(Header(keys, header_start, offset, stripped.startswith("[[")))
                table = keys
                start = offset
                continue
            start = offset
        was_idle = lexer.idle
        lexer.comment_at = None
        lexer.feed(line)
        offset += len(line)
        if lexer.idle:
            span = text[start:offset]
            probed = _probe_keys(span)
            if probed is not None:
                leaf, key_text = probed
                assignments.append(Assignment(
                    keys=table + leaf,
                    start=start,
                    end=offset,
                    raw=span,
                    key_text=key_text,
                    indent=span[: len(span) - len(span.lstrip(" \t"))],
                    comment=_tail_comment(line, lexer.comment_at) if was_idle else "",
                ))
    return Scan(assignments, headers)


def _tail_comment(line: str, comment_at: int | None) -> str:
    """取语句所在行 `#` 起（含前导空白）的尾部注释，改写时原样保留。"""
    if comment_at is None:
        return ""
    body = line.rstrip("\r\n")
    cut = comment_at
    while cut > 0 and body[cut - 1] in " \t":
        cut -= 1
    return body[cut:]


class ConfigDocument:
    """config.toml 的只读视图 + 单条赋值语句的定点改写。"""

    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw
        self.bom = bool(raw and raw.startswith(BOM))
        try:
            self.text = (raw or b"").decode("utf-8-sig")
            self.data = tomllib.loads(self.text)
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"config.toml 不是有效的 UTF-8 TOML：{exc}") from exc
        self.scan = scan_toml(self.text)

    @property
    def newline(self) -> str:
        return "\r\n" if "\r\n" in self.text else "\n"

    def encode(self, text: str) -> bytes:
        return (BOM if self.bom else b"") + text.encode("utf-8")

    def lookup(self, keys: tuple[str, ...]) -> object:
        node: object = self.data
        for name in keys:
            if not isinstance(node, dict) or name not in node:
                return _MISSING
            node = node[name]
        return node

    def find(self, keys: tuple[str, ...]) -> Assignment | None:
        for item in self.scan.assignments:
            if item.keys == keys:
                return item
        return None

    def declared(self, keys: tuple[str, ...]) -> bool:
        return self.lookup(keys) is not _MISSING or self.find(keys) is not None

    def _segment_end(self, table: tuple[str, ...]) -> int:
        """返回该表所在段的结束位置。

        TOML 语义：一个表的键必须写在它和**下一个表头**之间——无论那个表头是不是子表。
        早期版本用 `len(keys) <= len(table)` 判断，会把 `[profiles.work.mcp]` 这类子表头
        当成"不是边界"而跳过，键最终落到子表里；反过来把子表头都当边界也同样错。
        正确做法是取第一个后续表头。
        """
        headers = self.scan.headers
        index = None
        for position, header in enumerate(headers):
            if header.keys == table and not header.array:
                index = position
                break
        if index is None:
            raise ConfigError(f"未找到目标分区 [{'.'.join(table)}]，未修改配置。")
        for header in headers[index + 1:]:
            return header.start
        return len(self.text)

    def _segment_indent(self, table: tuple[str, ...]) -> str:
        for item in self.scan.assignments:
            if item.keys[: len(table)] == table and item.keys != table and item.indent:
                return item.indent
        return ""

    def set_assignment(self, keys: tuple[str, ...], value_literal: str) -> bytes:
        """替换目标键所在语句；不存在时插入到正确的分区内。"""
        text = self.text
        newline = self.newline
        item = self.find(keys)
        if item is not None:
            line = f"{item.indent}{item.key_text} = {value_literal}{item.comment}{newline}"
            return self._verified(self.encode(text[: item.start] + line + text[item.end:]), keys)
        table = keys[:-1]
        name = keys[-1]
        if table:
            end = self._segment_end(table)
            prefix = text[:end]
            if prefix and not prefix.endswith(newline):
                prefix += newline
            body = f"{self._segment_indent(table)}{name} = {value_literal}{newline}"
            return self._verified(self.encode(prefix + body + text[end:]), keys)
        offset = min((header.start for header in self.scan.headers), default=len(text))
        head = text[:offset]
        if head and not head.endswith(newline):
            head += newline
        installed = self.encode(f"{head}{name} = {value_literal}{newline}{text[offset:]}")
        return self._verified(installed, keys)

    def write_raw(self, keys: tuple[str, ...], raw_text: str) -> bytes:
        """把目标键所在语句整段替换成给定原文（用于还原历史行）。"""
        item = self.find(keys)
        if item is None:
            raise ConfigError("配置中找不到待还原的赋值语句，未修改配置。")
        return self._verified(self.encode(self.text[: item.start] + raw_text + self.text[item.end:]), keys)

    def _verified(self, installed: bytes, keys: tuple[str, ...]) -> bytes:
        """双通道交叉验证：能重新解析，且除目标键外所有配置值的语义完全不变。"""
        parsed = ConfigDocument(installed)
        if _without(self.data, keys) != _without(parsed.data, keys):
            raise ConfigError("改写后的配置语义与原文不一致，未写入文件。")
        return installed


# --------------------------------------------------------------------------
# 原子写与事务
# --------------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    """并发安全的目录创建。

    Windows 上两个进程同时用 parents=True/exist_ok=True 建同一条目录链，
    仍可能抛出 FileExistsError（exist_ok 的检查与创建之间存在竞态），
    这里把它吸收掉：只要最终确实是目录就算成功。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        if not path.is_dir():
            raise


def _atomic_write(path: Path, data: bytes | None) -> None:
    """原子写入。Windows 上杀软扫描、编辑器句柄、同步盘都会短暂占用文件，
    因此这里对 PermissionError/OSError 做有限重试，再给出可操作的中文指引。"""
    if _is_link(path):
        raise ConfigError(f"不覆盖符号链接或目录联接：{path}")
    if data is None:
        _remove_with_retry(path)
        return
    _ensure_dir(path.parent)
    last: OSError | None = None
    for index, delay in enumerate(WRITE_RETRY_DELAYS):
        if index:
            time.sleep(delay)
        temp: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".xiaoli-", suffix=".tmp", dir=str(path.parent))
            temp = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            temp = None
            _fsync_dir(path.parent)
            return
        except OSError as exc:
            last = exc
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
    raise ConfigError(
        f"写入失败，重试 {len(WRITE_RETRY_DELAYS)} 次仍不成功：{path}\n"
        f"常见原因：杀毒/安全软件拦截、文件被编辑器或同步盘（如 OneDrive）占用、目录权限不足。\n"
        f"原始错误：{last}"
    ) from last


def _remove_with_retry(path: Path) -> None:
    last: OSError | None = None
    for index, delay in enumerate(WRITE_RETRY_DELAYS):
        if index:
            time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
            _fsync_dir(path.parent)
            return
        except OSError as exc:
            last = exc
    raise ConfigError(
        f"删除失败，重试 {len(WRITE_RETRY_DELAYS)} 次仍不成功：{path}\n"
        f"常见原因：文件正被杀毒软件或同步盘占用、文件为只读。原始错误：{last}"
    ) from last


def _commit(updates: list[tuple[Path, bytes | None]], expected: dict[Path, bytes | None]) -> None:
    """先核对全部输入未变，再逐条原子写入；失败时按快照回滚。

    若回滚期间发现路径已被其他写入者改动，则保留其内容并报错，避免破坏并发修改。
    """
    snapshots = {path: expected[path] if path in expected else _bytes(path) for path, _ in updates}
    written = dict(updates)
    read_only = {path: raw for path, raw in expected.items() if path not in written}
    changed: list[Path] = []
    write_attempted = False
    try:
        for path, raw in expected.items():
            if _bytes(path) != raw:
                raise ConfigConflictError(f"文件在操作期间被更改，未覆盖新内容：{path}")
        for path, data in updates:
            for source, raw in read_only.items():
                if _bytes(source) != raw:
                    raise ConfigConflictError(f"配置来源在操作期间被更改，未覆盖：{source}")
            if _bytes(path) != snapshots[path]:
                raise ConfigConflictError(f"文件在操作期间被更改，未覆盖新内容：{path}")
            write_attempted = True
            _atomic_write(path, data)
            changed.append(path)
        for source, raw in read_only.items():
            if _bytes(source) != raw:
                raise ConfigConflictError(f"配置来源在操作期间被更改，未覆盖：{source}")
    except Exception as original:  # noqa: BLE001 - 需要区分回滚可行性
        if not write_attempted:
            raise
        problems: list[str] = []
        for path in written:
            # 已写入的路径期望当前值等于新内容（written），未写入的路径期望等于原内容（snapshots）。
            # 命名易被误读为条件反了，实际上是「按是否已提交选择期望值」；两者任一被外部改动都不能回滚。
            wanted = written[path] if path in changed else snapshots[path]
            try:
                if _bytes(path) != wanted:
                    problems.append(f"{path}: 出现其他修改，已保留本次相关文件")
            except OSError as exc:
                problems.append(f"{path}: 无法核对回滚条件，已保留本次相关文件：{exc}")
        if problems:
            raise ConfigError("操作失败；为保留并发修改及其依赖文件，未执行回滚：" + "; ".join(problems)) from original
        restored: dict[Path, bytes | None] = {}
        for path in reversed(changed):
            try:
                if any(_bytes(done) != raw for done, raw in restored.items()):
                    problems.append("回滚期间已恢复的文件再次变化；已保留其余依赖文件")
                    break
                if _bytes(path) != written[path]:
                    problems.append(f"{path}: 回滚前出现其他修改，已保留其余依赖文件")
                    break
                _atomic_write(path, snapshots[path])
                restored[path] = snapshots[path]
            except (OSError, ConfigError) as exc:
                problems.append(f"{path}: {exc}")
                break
        if problems:
            raise ConfigError("操作失败，部分回滚未完成：" + "; ".join(problems)) from original
        raise


# --------------------------------------------------------------------------
# 配置仓库
# --------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否仍是活着的进程。

    Windows 上不能只看 OpenProcess 是否成功：进程已终止但在句柄全部释放前，
    OpenProcess 依然会返回有效句柄，所以必须再取退出码判断 STILL_ACTIVE。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True  # 查不到退出码时按存活保守处理
                return code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 探测失败时按“存活”保守处理
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


class ConfigStore:
    def __init__(self, codex_home: str | Path | None = None, *, tool_id: str = TOOL_ID) -> None:
        home = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        self.codex_home = Path(home).expanduser().resolve()
        self.config_path = self.codex_home / "config.toml"
        self.managed_dir = self.codex_home / "managed-prompts" / tool_id
        self.state_path = self.managed_dir / "install-state.json"
        self.backup_path = self.managed_dir / "config.toml.before-xiaoli.bak"
        self.lock_path = self.managed_dir / "install.lock"
        self.retained_paths: list[str] = []

    # -- 路径与状态 ------------------------------------------------------

    def _check_directories(self) -> None:
        for path in (self.managed_dir.parent, self.managed_dir, self.config_path, self.state_path, self.backup_path):
            if _is_link(path):
                raise ConfigError(f"不修改符号链接或目录联接：{path}")
        if self.codex_home.exists() and not self.codex_home.is_dir():
            raise ConfigError(f"配置目录路径不是目录：{self.codex_home}（请检查 --codex-home / CODEX_HOME）")
        if self.config_path.exists() and not self.config_path.is_file():
            raise ConfigError(f"config.toml 不是普通文件：{self.config_path}")
        if self.managed_dir.parent.exists() and not self.managed_dir.parent.is_dir():
            raise ConfigError(f"managed-prompts 路径已被占用且不是目录：{self.managed_dir.parent}")
        if self.managed_dir.exists() and not self.managed_dir.is_dir():
            raise ConfigError(f"工具目录路径不是目录：{self.managed_dir}")
        if self.state_path.exists() and not self.state_path.is_file():
            raise ConfigError(f"安装状态路径不是普通文件：{self.state_path}")
        if self.backup_path.exists() and not self.backup_path.is_file():
            raise ConfigError(f"备份路径不是普通文件：{self.backup_path}")

    def resolve_reference(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.codex_home / path
        return path.resolve()

    def _matches_reference(self, value: object, target: Path) -> bool:
        try:
            return isinstance(value, str) and bool(value.strip()) and self.resolve_reference(value) == target.resolve()
        except (OSError, ValueError, RuntimeError):
            return False

    def state_is_current(self, state: dict | None, doc: ConfigDocument | None) -> bool:
        """状态记录里的副本仍是当前有效引用且文件存在。"""
        if not state or doc is None:
            return False
        try:
            target = self._managed_prompt(state.get("prompt_path"))
        except ConfigError:
            return False
        return target.is_file() and self._matches_reference(doc.lookup(tuple(state["keys"])), target)

    def _managed_prompt(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ConfigError("安装状态中的指令副本路径无效，未操作任何文件。")
        path = Path(value)
        if (not path.is_absolute() or path.parent.resolve() != self.managed_dir.resolve()
                or path.suffix.lower() != PROMPT_SUFFIX or _is_link(path)):
            raise ConfigError("安装状态中的指令副本路径不属于本工具目录，未操作该文件。")
        return path

    def read_state(self) -> dict | None:
        self._check_directories()
        raw = _bytes(self.state_path)
        if raw is None:
            return None
        try:
            state = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError) as exc:
            raise ConfigError(f"安装状态无法读取，原配置与备份均已保留：{exc}") from exc
        if not isinstance(state, dict) or state.get("version") != SCHEMA_VERSION:
            raise ConfigError("安装状态版本不受支持，原配置与备份均已保留。")
        if not isinstance(state.get("config_existed"), bool) or not isinstance(state.get("had_line"), bool):
            raise ConfigError("安装状态的恢复基线不完整，原配置与备份均已保留。")
        keys = state.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(item, str) and item for item in keys):
            raise ConfigError("安装状态缺少目标键路径，原配置与备份均已保留。")
        if state["had_line"] and not isinstance(state.get("previous_line"), str):
            raise ConfigError("安装状态缺少原始赋值行，原配置与备份均已保留。")
        self._managed_prompt(state.get("prompt_path"))
        owned = state.get("owned_prompts", {})
        if not isinstance(owned, dict):
            raise ConfigError("安装状态的文件记录无效，原配置与备份均已保留。")
        for path, digest in owned.items():
            self._managed_prompt(path)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ConfigError("安装状态的文件校验值无效，原配置与备份均已保留。")
        return state

    # -- 锁 --------------------------------------------------------------

    def _lock_info(self) -> str:
        raw = _bytes(self.lock_path)
        if raw is None:
            return "锁文件已消失"
        try:
            info = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return "锁文件内容无法解析"
        return f"pid={info.get('pid')}, since={info.get('created_at')}"

    def _clear_stale_lock(self) -> None:
        """--break-lock 的清理路径。

        陈旧（内容不可解析 / pid 已退出 / 超过 30 分钟）→ 直接清理；
        仍标记着活动进程的锁 → 只有在用户显式 --break-lock 时才强制接管，
        接管前把锁文件归档到 history/lock-<id>/ 留痕；
        唯一绝不清理的情况是「锁记录的正是当前进程」。
        """
        raw = _bytes(self.lock_path)
        if raw is None:
            return
        stale = False
        pid = 0
        try:
            info = json.loads(raw.decode("utf-8", "replace"))
            pid = int(info.get("pid", 0))
        except (ValueError, TypeError, AttributeError):
            stale = True
        if not stale and pid == os.getpid():
            raise ConfigConflictError("锁文件记录的正是当前进程，未清理；请确认没有其他窗口在运行后重试。")
        if not stale:
            try:
                age = datetime.now(timezone.utc).timestamp() - self.lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > LOCK_STALE_SECONDS or not _pid_alive(pid):
                stale = True
        if stale:
            self.lock_path.unlink(missing_ok=True)
            return
        # 仍标记为活动：显式接管，先把原锁文件归档留痕，再清理。
        archive = self._new_recovery_dir("lock")
        archive_path = archive / "install.lock"
        _commit([(archive_path, raw), (self.lock_path, None)], {archive_path: None, self.lock_path: raw})

    @contextmanager
    def _lock(self, *, break_stale: bool = False):
        self._check_directories()
        _ensure_dir(self.managed_dir)
        if break_stale:
            self._clear_stale_lock()
        try:
            with self.lock_path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created_at": _now()}, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ConfigConflictError(
                f"另一个安装/恢复操作正在运行（{self._lock_info()}）；请等它结束；"
                f"确认是陈旧锁时加 --break-lock 重试。"
            ) from exc
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    # -- 注入点 ----------------------------------------------------------

    def detect_target(self, doc: ConfigDocument, mode: str = "auto") -> tuple[tuple[str, ...], str]:
        root_keys = (KEY,)
        current = doc.lookup(("profile",))
        profile_keys = ("profiles", str(current), KEY) if isinstance(current, str) and current else None
        profile_declared = bool(profile_keys) and doc.declared(profile_keys)
        if mode == "root":
            return root_keys, "root"
        if mode == "profile":
            if profile_keys is None:
                raise ConfigError("配置中没有可用的 profile 名称，无法写入分区键。")
            return profile_keys, "profile"
        if profile_declared:
            return profile_keys, "profile"
        return root_keys, "root"

    def _unused_prompt_path(self) -> Path:
        target = self.managed_dir / "active.md"
        while target.exists() or _is_link(target):
            target = self.managed_dir / f"active-{uuid.uuid4().hex[:12]}.md"
        return target

    def _new_recovery_dir(self, prefix: str = "reference") -> Path:
        history = self.managed_dir / "history"
        if _is_link(history) or (history.exists() and not history.is_dir()):
            raise ConfigError(f"历史快照位置不是普通目录，未修改配置：{history}")
        if history.resolve() != self.managed_dir.resolve() / "history":
            raise ConfigError("历史快照目录超出本工具目录，未修改配置。")
        target = history / f"{prefix}-{uuid.uuid4().hex}"
        if target.exists() or _is_link(target):
            raise ConfigConflictError("历史快照目录发生变化，未修改配置。")
        return target

    # -- 安装 ------------------------------------------------------------

    def install_content(self, content: str, filename: str, *, title: str, key: str = "custom",
                        expected_inputs: dict[Path, bytes | None] | None = None,
                        rebase_current: bool = False, target_mode: str = "auto",
                        break_stale: bool = False) -> dict:
        content = content.removeprefix("\ufeff")
        if not content.strip():
            raise ConfigError("指令内容为空，未写入配置。")
        if Path(filename).name != filename or not filename.lower().endswith(PROMPT_SUFFIX):
            raise ConfigError("指令副本必须使用不含目录的 .md 文件名。")
        with self._lock(break_stale=break_stale):
            for path, raw in (expected_inputs or {}).items():
                if _bytes(path) != raw:
                    raise ConfigConflictError(f"配置来源在安装前发生变化，未覆盖：{path}")
            state_raw = _bytes(self.state_path)
            state = self.read_state()
            doc = ConfigDocument(_bytes(self.config_path))
            keys, mode = self.detect_target(doc, target_mode)
            updates: list[tuple[Path, bytes | None]] = []
            expected: dict[Path, bytes | None] = {self.config_path: doc.raw, self.state_path: state_raw}
            archive: dict[Path, bytes | None] = {}
            reference_repaired = False
            recovery: Path | None = None
            replaced_edited: str | None = None
            orphan_archived: str | None = None

            if state:
                target = self._managed_prompt(state["prompt_path"])
                state_keys = tuple(state["keys"])
                matched = self._matches_reference(doc.lookup(state_keys), target) and target.is_file()
                if matched:
                    keys, mode = state_keys, str(state.get("target_mode") or mode)
                else:
                    if not rebase_current:
                        raise ConfigConflictError("当前指令引用已被其他操作更改，未覆盖；备份与状态仍保留。")
                    reference_repaired = True
                    recovery = self._new_recovery_dir()
                    snapshots: list[tuple[Path, bytes | None]] = [(recovery / "install-state.json", state_raw)]
                    if doc.raw is not None:
                        snapshots.append((recovery / "config-before-repair.toml", doc.raw))
                    original_backup = _bytes(self.backup_path)
                    archive[self.backup_path] = original_backup
                    if original_backup is not None:
                        snapshots.append((recovery / "config-before-first-install.bak", original_backup))
                    old_paths = {self._managed_prompt(value) for value in state.get("owned_prompts", {})}
                    old_paths.add(target)
                    for old in sorted(old_paths):
                        raw = _bytes(old)
                        archive[old] = raw
                        if raw is not None:
                            snapshots.append((recovery / "prompts" / old.name, raw))
                    updates.extend(snapshots)
                    expected.update({path: None for path, _ in snapshots})
                    # 新一轮安装以“当前有效引用”为恢复目标，不继承可能被当作恢复对象的旧文件归属。
                    keys, mode = self.detect_target(doc, target_mode)
                    item = doc.find(keys)
                    state = {
                        "version": SCHEMA_VERSION,
                        "installed_at": _now(),
                        "config_existed": doc.raw is not None,
                        "keys": list(keys),
                        "target_mode": mode,
                        "had_line": item is not None,
                        "previous_line": item.raw if item is not None else None,
                        "recovery_path": str(recovery),
                    }
                    target = self._unused_prompt_path()
                    target_raw = None
            else:
                if self.backup_path.exists():
                    # 孤儿备份（有备份但没有安装状态，例如状态文件被同步盘/杀软删掉）：
                    # 先把备份内容归档到 history，再继续安装；既不覆盖也不丢弃用户数据。
                    orphan = self._new_recovery_dir("orphan-backup")
                    orphan_path = orphan / "config-before-first-install.bak"
                    orphan_raw = _bytes(self.backup_path)
                    updates.append((orphan_path, orphan_raw))
                    expected[orphan_path] = None
                    expected[self.backup_path] = orphan_raw
                    orphan_archived = str(orphan)
                item = doc.find(keys)
                target = self._unused_prompt_path()
                target_raw = None
                state = {
                    "version": SCHEMA_VERSION,
                    "installed_at": _now(),
                    "config_existed": doc.raw is not None,
                    "keys": list(keys),
                    "target_mode": mode,
                    "had_line": item is not None,
                    "previous_line": item.raw if item is not None else None,
                }

            if state and not reference_repaired:
                target_raw = _bytes(target)
                digest = state.get("owned_prompts", {}).get(str(target))
                if target_raw is not None and digest != _sha(target_raw):
                    # 副本被手工改过：新建一个文件，避免覆盖用户内容。
                    replaced_edited = str(target)
                    target = self._unused_prompt_path()
                    target_raw = None

            raw_prompt = content.encode("utf-8")
            owned = dict(state.get("owned_prompts", {}))
            owned[str(target)] = _sha(raw_prompt)
            item = doc.find(keys)
            state = dict(state, version=SCHEMA_VERSION, profile_title=title, profile_key=key,
                         source_filename=filename, prompt_path=str(target), keys=list(keys),
                         target_mode=mode, had_line=state["had_line"], owned_prompts=owned, updated_at=_now())
            raw_state = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            installed = doc.set_assignment(keys, json.dumps(target.as_posix(), ensure_ascii=False))
            ConfigDocument(installed)  # 写前自校验
            expected[target] = target_raw
            if state_raw is None and doc.raw is not None:
                updates.append((self.backup_path, doc.raw))
                if self.backup_path not in expected:
                    expected[self.backup_path] = None
            updates.extend([(target, raw_prompt), (self.state_path, raw_state), (self.config_path, installed)])
            for path, raw in {**archive, **(expected_inputs or {})}.items():
                if _bytes(path) != raw:
                    raise ConfigConflictError(f"配置来源在安装期间发生变化，未覆盖：{path}")
            expected.update(archive)
            expected.update(expected_inputs or {})
            _commit(updates, expected)
            result = dict(state)
            result.update({
                "ok": True,
                "action": "install",
                "reference_repaired": reference_repaired,
                "recovery_path": str(recovery) if recovery else None,
                "backup_path": str(self.backup_path) if self.backup_path.exists() else None,
                "had_line_before": state["had_line"],
                "replaced_edited_prompt": replaced_edited,
                "orphan_backup_archived": orphan_archived,
            })
            return result

    # -- 恢复 ------------------------------------------------------------

    def restore(self, *, break_stale: bool = False) -> dict:
        self.retained_paths = []
        if not self.state_path.exists():
            return {"ok": True, "action": "restore", "restored": False, "retained_paths": []}
        with self._lock(break_stale=break_stale):
            state_raw = _bytes(self.state_path)
            state = self.read_state()
            if state is None:
                return {"ok": True, "action": "restore", "restored": False, "retained_paths": []}
            keys = tuple(state["keys"])
            target = self._managed_prompt(state["prompt_path"])
            doc = ConfigDocument(_bytes(self.config_path))
            if not self._matches_reference(doc.lookup(keys), target):
                raise ConfigConflictError("当前指令引用已被其他操作更改，未覆盖；备份与状态仍保留。")
            if state["had_line"]:
                restored = doc.write_raw(keys, state["previous_line"])
            else:
                restored = doc.write_raw(keys, "")
            ConfigDocument(restored)
            data = None if (not state["config_existed"] and restored == b"") else restored
            updates: list[tuple[Path, bytes | None]] = [(self.config_path, data)]
            expected: dict[Path, bytes | None] = {self.config_path: doc.raw, self.state_path: state_raw}
            owned = state.get("owned_prompts", {})
            for value in sorted(set(owned) | {str(target)}):
                path = self._managed_prompt(value)
                raw = _bytes(path)
                if raw is None:
                    continue
                if owned.get(value) == _sha(raw):
                    updates.append((path, None))
                    expected[path] = raw
                else:
                    self.retained_paths.append(str(path))
            updates.extend([(self.state_path, None), (self.backup_path, None)])
            _commit(updates, expected)
        for directory in (self.managed_dir, self.managed_dir.parent):
            try:
                directory.rmdir()  # 只删空目录，绝不递归
            except OSError:
                pass
        return {"ok": True, "action": "restore", "restored": True, "retained_paths": self.retained_paths}

    # -- 状态 ------------------------------------------------------------

    def status(self) -> dict:
        self._check_directories()
        result: dict = {
            "ok": True,
            "action": "status",
            "tool": TOOL_ID,
            "codex_home": str(self.codex_home),
            "config_path": str(self.config_path),
            "config_exists": self.config_path.is_file(),
            "backup_exists": self.backup_path.is_file(),
            "installed": False,
        }
        doc: ConfigDocument | None = None
        try:
            doc = ConfigDocument(_bytes(self.config_path))
        except ConfigError as exc:
            result["config_error"] = str(exc)
        state = None
        if self.state_path.exists():
            try:
                state = self.read_state()
            except ConfigError as exc:
                result["state_error"] = str(exc)
        if state:
            target = self._managed_prompt(state["prompt_path"])
            current = doc.lookup(tuple(state["keys"])) if doc else _MISSING
            raw_prompt = _bytes(target)
            result.update({
                "installed": True,
                "profile": state.get("profile_title"),
                "installed_at": state.get("installed_at"),
                "updated_at": state.get("updated_at"),
                "target_mode": state.get("target_mode"),
                "keys": state.get("keys"),
                "prompt_path": str(target),
                "prompt_exists": target.is_file(),
                "prompt_sha256_12": _sha(raw_prompt)[:12] if raw_prompt is not None else None,
                "prompt_bytes": len(raw_prompt) if raw_prompt is not None else 0,
                "reference_ok": self._matches_reference(current, target),
                "recovery_path": state.get("recovery_path"),
                "owned_prompts": len(state.get("owned_prompts", {})),
            })
        elif doc:
            keys, mode = self.detect_target(doc, "auto")
            value = doc.lookup(keys)
            result.update({
                "keys": list(keys),
                "target_mode": mode,
                "current_reference": value if isinstance(value, str) else None,
                "profile_declared": bool(doc.declared(("profile",))),
            })
        if doc is not None and doc.raw is not None:
            result["config_sha256_12"] = _sha(doc.raw)[:12]
        return result


    # -- 只读现场诊断 ----------------------------------------------------

    def diagnose(self) -> dict:
        """把当前现场归入一种明确情形，并给出下一步可执行命令；全程只读。"""
        cli = "python xiaoli.py"
        findings: list[str] = []
        commands: list[str] = []
        situation = ""
        severity = "ok"

        try:
            self._check_directories()
        except ConfigError as exc:
            return {
                "ok": True,
                "action": "doctor",
                "codex_home": str(self.codex_home),
                "config_path": str(self.config_path),
                "situation": "路径异常",
                "severity": "high",
                "findings": [str(exc)],
                "next_commands": ["检查 --codex-home / CODEX_HOME 指向的目录后重试"],
                "history_snapshots": 0,
            }

        doc: ConfigDocument | None = None
        config_error: str | None = None
        try:
            doc = ConfigDocument(_bytes(self.config_path))
        except ConfigError as exc:
            config_error = str(exc)

        state: dict | None = None
        state_error: str | None = None
        if self.state_path.exists():
            try:
                state = self.read_state()
            except ConfigError as exc:
                state_error = str(exc)

        history = self.managed_dir / "history"
        snapshots = sorted(history.iterdir()) if history.is_dir() else []
        reference = doc.lookup((KEY,)) if doc is not None else _MISSING
        reference_is_ours = False
        if isinstance(reference, str) and reference.strip():
            try:
                reference_is_ours = self.resolve_reference(reference).parent == self.managed_dir.resolve()
            except (OSError, ValueError, RuntimeError):
                reference_is_ours = False

        if config_error:
            situation, severity = "config.toml 无法解析", "high"
            findings.append(config_error)
            if self.backup_path.is_file():
                findings.append(f"首次备份可用作对照：{self.backup_path}")
            commands.append("手工修复 config.toml（或对照备份）后重跑 --doctor")
        elif state_error:
            situation, severity = "安装状态损坏", "high"
            findings.append(state_error)
            if self.managed_dir.is_dir():
                findings.append("工具目录内现有："
                                + "、".join(path.name for path in sorted(self.managed_dir.iterdir())[:8]))
            findings.append("状态损坏时 --install 与 --restore 都会拒绝写入，这是刻意保护。")
            commands.append(f"把 {self.state_path.name} 改名（例如加 .broken 后缀）保留现场，再执行：")
            commands.append(f"{cli} --install")
        elif state is None:
            leftovers = [path for path in sorted(self.managed_dir.iterdir()) if path.name != "history"] \
                if self.managed_dir.is_dir() else []
            if self.backup_path.is_file():
                situation, severity = "上次安装未完成（有孤儿首次备份、没有安装状态）", "medium"
                findings.append(f"孤儿备份仍在：{self.backup_path}")
                findings.append("重装会先把该备份归档到 history/orphan-backup-<随标识>/ 再继续，不会删除或覆盖它。")
                if reference_is_ours:
                    findings.append("当前引用仍指向本工具目录。")
                commands.append(f"{cli} --install")
            elif leftovers:
                situation, severity = "存在残留工具目录但没有安装状态", "medium"
                findings.append("残留文件：" + "、".join(path.name for path in leftovers[:8]))
                findings.append("这些文件不会被自动删除；确认不再需要时可以手工删除该工具目录。")
                commands.append(f"{cli} --install      # 需要重新安装时会重建状态与副本")
            elif isinstance(reference, str) and reference.strip():
                situation, severity = "当前引用未登记（可能是其他工具或手工配置）", "low"
                findings.append(f"当前引用：{reference}")
                if reference_is_ours:
                    findings.append("引用就在本工具目录内，但缺少安装状态。")
                commands.append(f"{cli} --install --dry-run")
            else:
                situation, severity = "未安装", "ok"
                commands.append(f"{cli} --install")
        else:
            target = self._managed_prompt(state["prompt_path"])
            raw_prompt = _bytes(target)
            status = self.status()
            if raw_prompt is None:
                situation, severity = "指令副本丢失", "high"
                findings.append(f"状态记录的副本不存在：{target}")
                commands.append(f"{cli} --install      # 会重建副本并保留原恢复基线")
            elif not status.get("reference_ok"):
                situation, severity = "配置引用被外部改动", "medium"
                findings.append(f"状态期望的副本：{target}")
                findings.append(f"当前引用：{reference if isinstance(reference, str) else '（无）'}")
                commands.append(f"{cli} --install      # 先归档旧状态，再按当前引用重建")
            elif state.get("recovery_path"):
                situation, severity = "已安装且引用一致（曾发生过引用修复）", "low"
                findings.append(f"历史快照：{state['recovery_path']}")
                commands.append(f"{cli} --verify")
            else:
                situation, severity = "已安装且引用一致", "ok"
                commands.append(f"{cli} --verify")

            if raw_prompt is not None and state.get("owned_prompts", {}).get(str(target)) != _sha(raw_prompt):
                if severity == "ok":
                    severity = "medium"
                findings.append(f"副本已被手工修改：{target}（--verify 会报 state_digest 未通过）")
                commands.append(f"{cli} --install      # 会另建新副本，手工改动的文件不会被删除")

        if self.lock_path.exists():
            pid = 0
            raw_lock = _bytes(self.lock_path)
            if raw_lock is not None:
                try:
                    pid = int(json.loads(raw_lock.decode("utf-8", "replace")).get("pid", 0))
                except (ValueError, TypeError, AttributeError):
                    pid = 0
            alive = _pid_alive(pid)
            if severity in ("ok", "low"):
                severity = "medium"
            findings.append(f"存在锁文件：{self.lock_path}（pid={pid}，"
                            + ("该进程仍在运行" if alive else "该进程已不存在") + "）")
            if alive:
                commands.append("等正在运行的安装/恢复结束后重试")
                commands.append(f"{cli} --break-lock      # 确认没有别的安装/恢复在跑时，可强制接管该锁（原锁会归档）")
            else:
                commands.append(f"{cli} --break-lock      # 清理陈旧锁（原锁会归档到 history/）")

        if snapshots:
            findings.append(f"可用历史快照 {len(snapshots)} 个，最新：{snapshots[-1]}")

        return {
            "ok": True,
            "action": "doctor",
            "codex_home": str(self.codex_home),
            "config_path": str(self.config_path),
            "situation": situation,
            "severity": severity,
            "findings": findings,
            "next_commands": commands,
            "history_snapshots": len(snapshots),
        }


def main(argv: list[str] | None = None) -> int:
    """独立自检入口：python xiaoli_engine.py --codex-home ... status"""

    import argparse

    parser = argparse.ArgumentParser(description="小李破甲配置引擎自检入口")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("action", choices=["status"], nargs="?", default="status")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(ConfigStore(args.codex_home).status(), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
