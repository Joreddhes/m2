from __future__ import annotations

import base64
import ftplib
import hashlib
import io
import os
import posixpath
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from cryptography.fernet import Fernet
from flask import current_app

from .models import Source


@dataclass(frozen=True)
class StorageEntry:
    path: str
    name: str
    is_dir: bool
    size: int = 0
    modified: float = 0


def safe_relative(value: str) -> str:
    value = value.replace("\\", "/").strip()
    if value.startswith("/") or re_drive_prefix(value):
        raise ValueError("Недопустимый относительный путь")
    value = value.strip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Недопустимый относительный путь")
    return path.as_posix()


def re_drive_prefix(value: str) -> bool:
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def safe_component(value: str) -> str:
    value = "".join(character for character in value.strip() if ord(character) >= 32 and character not in '<>:"/\\|?*')
    value = value.rstrip(". ")
    if not value or value in {".", ".."}:
        raise ValueError("Недопустимое имя файла или папки")
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("Зарезервированное имя файла")
    return value


def _fernet() -> Fernet:
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_password(password: str) -> str:
    return _fernet().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_password(token: str | None) -> str:
    if not token:
        return ""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def _ftp_timestamp(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0


class StorageBackend:
    def list(self, relative: str) -> list[StorageEntry]:
        raise NotImplementedError

    def walk_files(self, relative: str) -> Iterator[StorageEntry]:
        for entry in self.list(relative):
            if entry.is_dir:
                yield from self.walk_files(entry.path)
            else:
                yield entry

    def mkdir(self, relative: str) -> None:
        raise NotImplementedError

    def read_bytes(self, relative: str) -> bytes:
        raise NotImplementedError

    def write_bytes(self, relative: str, data: bytes) -> None:
        raise NotImplementedError

    def download(self, relative: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.read_bytes(relative))

    def upload_file(self, relative: str, source: Path) -> None:
        self.write_bytes(relative, source.read_bytes())

    def exists(self, relative: str) -> bool:
        try:
            parent, name = posixpath.split(safe_relative(relative))
            return any(entry.name == name for entry in self.list(parent))
        except (FileNotFoundError, ftplib.error_perm):
            return False

    def local_file(self, relative: str) -> Path | None:
        return None


class LocalStorage(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()

    def _path(self, relative: str) -> Path:
        if relative in {"", "."}:
            return self.root
        relative = safe_relative(relative)
        target = (self.root / Path(*PurePosixPath(relative).parts)).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("Путь выходит за корень источника")
        return target

    def list(self, relative: str) -> list[StorageEntry]:
        target = self._path(relative)
        result = []
        for item in target.iterdir():
            stat = item.stat()
            child = f"{relative.strip('/')}/{item.name}".strip("/").replace("\\", "/")
            result.append(StorageEntry(child, item.name, item.is_dir(), stat.st_size, stat.st_mtime))
        return result

    def mkdir(self, relative: str) -> None:
        self._path(relative).mkdir(parents=True, exist_ok=True)

    def read_bytes(self, relative: str) -> bytes:
        return self._path(relative).read_bytes()

    def write_bytes(self, relative: str, data: bytes) -> None:
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.uploading")
        temporary.write_bytes(data)
        os.replace(temporary, target)

    def download(self, relative: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(relative), destination)

    def upload_file(self, relative: str, source: Path) -> None:
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.uploading")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)

    def local_file(self, relative: str) -> Path | None:
        return self._path(relative)


class FTPStorage(StorageBackend):
    def __init__(self, source: Source):
        self.source = source

    @contextmanager
    def _connection(self):
        cls = ftplib.FTP_TLS if self.source.use_tls else ftplib.FTP
        ftp = cls()
        ftp.connect(self.source.host or "", self.source.port or 21, timeout=20)
        ftp.login(self.source.username or "anonymous", decrypt_password(self.source.encrypted_password))
        if self.source.use_tls:
            ftp.prot_p()
        try:
            yield ftp
        finally:
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()

    def _remote(self, relative: str) -> str:
        root = "/" + self.source.root.strip("/")
        if relative in {"", "."}:
            return posixpath.normpath(root)
        relative = safe_relative(relative)
        return posixpath.normpath(posixpath.join(root, relative))

    def list(self, relative: str) -> list[StorageEntry]:
        relative = "" if relative in {"", "."} else safe_relative(relative)
        remote = self._remote(relative)
        result: list[StorageEntry] = []
        with self._connection() as ftp:
            try:
                listing = list(ftp.mlsd(remote))
                for name, facts in listing:
                    if name in {".", ".."}:
                        continue
                    child = f"{relative}/{name}".strip("/")
                    is_dir = facts.get("type") == "dir"
                    size = int(facts.get("size", 0)) if not is_dir else 0
                    result.append(StorageEntry(child, name, is_dir, size, _ftp_timestamp(facts.get("modify"))))
            except ftplib.error_perm:
                current = ftp.pwd()
                for full_name in ftp.nlst(remote):
                    name = posixpath.basename(full_name.rstrip("/"))
                    if name in {"", ".", ".."}:
                        continue
                    is_dir = False
                    try:
                        ftp.cwd(full_name)
                        is_dir = True
                    except ftplib.error_perm:
                        pass
                    finally:
                        ftp.cwd(current)
                    size = 0
                    if not is_dir:
                        try:
                            size = ftp.size(full_name) or 0
                        except ftplib.error_perm:
                            pass
                    result.append(StorageEntry(f"{relative}/{name}".strip("/"), name, is_dir, size, 0))
        return result

    def mkdir(self, relative: str) -> None:
        parts = PurePosixPath(safe_relative(relative)).parts
        current = "/" + self.source.root.strip("/")
        with self._connection() as ftp:
            for part in parts:
                current = posixpath.join(current, part)
                try:
                    ftp.mkd(current)
                except ftplib.error_perm as exc:
                    if not str(exc).startswith("550"):
                        raise

    def read_bytes(self, relative: str) -> bytes:
        output = io.BytesIO()
        with self._connection() as ftp:
            ftp.retrbinary(f"RETR {self._remote(relative)}", output.write)
        return output.getvalue()

    def write_bytes(self, relative: str, data: bytes) -> None:
        parent = posixpath.dirname(safe_relative(relative))
        if parent:
            self.mkdir(parent)
        with self._connection() as ftp:
            ftp.storbinary(f"STOR {self._remote(relative)}", io.BytesIO(data))

    def download(self, relative: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        with temporary.open("wb") as handle, self._connection() as ftp:
            ftp.retrbinary(f"RETR {self._remote(relative)}", handle.write)
        os.replace(temporary, destination)

    def upload_file(self, relative: str, source: Path) -> None:
        parent = posixpath.dirname(safe_relative(relative))
        if parent:
            self.mkdir(parent)
        with source.open("rb") as handle, self._connection() as ftp:
            ftp.storbinary(f"STOR {self._remote(relative)}", handle, blocksize=1024 * 1024)


def backend_for(source: Source) -> StorageBackend:
    if source.kind == "local":
        return LocalStorage(source.root)
    if source.kind == "ftp":
        return FTPStorage(source)
    raise ValueError(f"Неизвестный тип источника: {source.kind}")
