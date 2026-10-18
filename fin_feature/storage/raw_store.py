"""2B-1：采集原始证据存储（RawStore）。

契约依据：docs/stage-2/2b-contract.md 第 3 节、docs/stage-2/2b-schema.md。
本模块只处理"HTTP 客户端交给应用的完整响应体字节"的留存与校验，不做
JSON 解析、字段映射或数值标准化——那些会改变字节，不能作为原始证据摘要。

关键约定：
- 摘要对象是响应体原始字节：``compute_raw_hash(body)`` = SHA-256(body)。
  不是 ``resp.text``、不是重序列化 JSON、不是 metric/semantic 哈希。
- 落盘为 gzip，解压后必须逐字节等于 body；摘要对解压后的字节计算。
- 路径由生成的 UUID / 摘要 / 数字构成，绝不使用公司名或用户输入；
  路径固定为 ``{source}/{run_id}/{unit_key[:2]}/{unit_id}/p{页}/a{尝试}.body.gz``。
- 发布是"原子且不替换已有目标"：先写同目录唯一临时文件（flush + fsync），
  再用 ``os.link`` 创建目标。目标已存在且内容一致时复用；内容不同、文件损坏
  或不支持所需原子语义时明确失败，绝不回退为普通覆盖写。
- 失败时不自动删除已有证据；冲突时留下的临时文件由只读对账识别。
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import os
import re
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 原始响应格式标识：HTTP 客户端交给应用的响应体字节
RAW_FORMAT_HTTP_BODY = "http-body-v1"
# 旧库/旧测试写入的快照：没有经过文件校验，只能作审计引用
RAW_FORMAT_LEGACY = "legacy-unverified"

DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024  # 32 MiB

# 路径段白名单：来源名、UUID/摘要、数字。拒绝公司名、report_name 等用户输入。
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# os.link 在部分文件系统/平台上不可用；此时必须失败而不是回退覆盖写
_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM, errno.EACCES, errno.EOPNOTSUPP, errno.ENOTSUP,
    errno.EXDEV, errno.EMLINK,
}


class RawStoreError(Exception):
    """RawStore 基类异常。"""


class RawStoreConflict(RawStoreError):
    """同一证据身份已存在且内容不同，或目标文件损坏：拒绝覆盖。"""

    def __init__(self, message: str, *, temp_path: Path | None = None):
        super().__init__(message)
        self.temp_path = temp_path


class RawStorePathError(RawStoreError):
    """路径非法或越出 raw_root。"""


class RawStoreVerifyError(RawStoreError):
    """证据文件缺失、损坏、超出大小上限或哈希不一致。"""


def compute_raw_hash(body: bytes) -> str:
    """原始响应体摘要：SHA-256(字节)。

    契约明确禁止先 decode / JSON canonicalization / normalize_numeric 再算摘要：
    空格或字段顺序不同的 JSON 原文应得到不同 raw_hash，而规范化后相同的财务值
    仍复用内容版本（内容版本由 metric/semantic 哈希判定，见 hashing.py）。
    """
    if not isinstance(body, (bytes, bytearray)):
        raise RawStoreError("compute_raw_hash 只接受 bytes，不接受 str/dict")
    return hashlib.sha256(bytes(body)).hexdigest()


@dataclass(frozen=True)
class PublishContext:
    """一次响应体发布的身份：run / 单元 / 页 / HTTP 尝试。

    page_number 与 http_attempt 不进入单元范围摘要（请求范围不含页号与重试号），
    但进入证据身份：同一 run/unit/页/尝试最多一个 snapshot。
    """

    run_id: str
    unit_key: str
    unit_id: str
    page_number: int
    http_attempt: int
    source_name: str = "eastmoney"
    fetched_at: str | None = None        # 响应真实接收时间（UTC ISO-8601，Z）
    http_status: int | None = None
    content_type: str | None = None
    text_encoding: str | None = None


@dataclass(frozen=True)
class StoredResponse:
    """发布结果：数据库 raw_snapshot 需要的字段。"""

    file_path: str            # 相对 raw_root 的 posix 路径
    raw_hash: str             # SHA-256(解压后的原始字节)
    raw_bytes: int            # 解压后的字节数
    raw_format: str = RAW_FORMAT_HTTP_BODY
    reused: bool = False      # True 表示身份已存在且内容一致，复用既有文件


@dataclass(frozen=True)
class VerifiedBody:
    """校验结果：只读，不修改文件。"""

    file_path: str
    body: bytes
    raw_hash: str
    raw_bytes: int
    extra: dict[str, Any] = field(default_factory=dict)


class RawStore:
    """原始响应体留存：受 raw_root 限制的原子发布与只读校验。

    root 在构造时解析为固定绝对路径；数据库中只保存根下相对路径，
    便于整目录搬迁与对账。所有读写都先做越根检查。
    """

    def __init__(self, root: str | Path, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
                 *, readonly: bool = False):
        if not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool) or max_body_bytes <= 0:
            raise RawStoreError(f"max_body_bytes 必须为正整数，当前为 {max_body_bytes!r}")
        self.root = Path(root).expanduser().resolve()
        self.max_body_bytes = max_body_bytes
        self.readonly = readonly
        if readonly:
            # 只读模式：不创建根目录；缺根作为诊断/失败，不掩盖问题
            if not self.root.exists():
                raise RawStoreError(f"只读校验要求证据根已存在，但目录不存在：{self.root}")
        else:
            self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def path_for(self, context: PublishContext) -> str:
        """按固定布局生成相对路径；任何非法路径段直接拒绝。"""
        source = context.source_name
        if not isinstance(source, str) or not _SOURCE_RE.match(source):
            raise RawStorePathError(f"非法来源名作路径段：{source!r}")
        run_id = self._safe_id(context.run_id, "run_id")
        unit_id = self._safe_id(context.unit_id, "unit_id")
        unit_key = context.unit_key
        if not isinstance(unit_key, str) or not _HEX64_RE.match(unit_key):
            raise RawStorePathError(f"unit_key 必须为 64 位小写十六进制：{unit_key!r}")
        page = self._safe_index(context.page_number, "page_number")
        attempt = self._safe_index(context.http_attempt, "http_attempt")
        return (
            f"{source}/{run_id}/{unit_key[:2]}/{unit_id}/"
            f"p{page:06d}/a{attempt:06d}.body.gz"
        )

    @staticmethod
    def _safe_id(value: str, label: str) -> str:
        if not isinstance(value, str) or not _ID_RE.match(value) or value in {".", ".."}:
            raise RawStorePathError(f"非法路径段 {label}={value!r}（只允许 UUID/摘要字符）")
        return value

    @staticmethod
    def _safe_index(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RawStorePathError(f"{label} 必须为 >=1 的整数，当前为 {value!r}")
        return value

    def resolve_path(self, rel_path: str) -> Path:
        """把库内相对路径解析为 root 下的绝对路径，拒绝越根与 symlink 越根。"""
        if not isinstance(rel_path, str) or not rel_path:
            raise RawStorePathError(f"非法相对路径：{rel_path!r}")
        if os.path.isabs(rel_path) or rel_path.startswith("\\"):
            raise RawStorePathError(f"拒绝绝对路径：{rel_path!r}")
        parts = rel_path.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise RawStorePathError(f"拒绝含 .. 或空段的路径：{rel_path!r}")

        candidate = self.root.joinpath(*parts)
        # 逐段检查已存在的 symlink，避免通过链接写到根外
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                resolved = current.resolve()
                if not self._is_within(resolved):
                    raise RawStorePathError(f"拒绝 symlink 越根：{rel_path!r}")
        resolved = candidate.resolve()
        if not self._is_within(resolved):
            raise RawStorePathError(f"路径越出 raw_root：{rel_path!r}")
        return candidate

    def _is_within(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, context: PublishContext, body: bytes) -> StoredResponse:
        """原子发布一份响应体证据，返回 StoredResponse。

        - 目标不存在：临时文件 → fsync → os.link 创建目标 → fsync 父目录
        - 目标已存在且解压字节一致：复用，不改写既有证据与捕获时间
        - 目标已存在但内容不同/损坏：RawStoreConflict，不覆盖
        - 超出 max_body_bytes：明确失败，不把截断 body 当完整证据
        """
        if not isinstance(body, (bytes, bytearray)):
            raise RawStoreError("publish 只接受 bytes 响应体")
        body = bytes(body)
        if len(body) > self.max_body_bytes:
            raise RawStoreError(
                f"响应体 {len(body)} 字节超过 raw_store.max_body_bytes="
                f"{self.max_body_bytes}，不登记为完整证据"
            )

        rel_path = self.path_for(context)
        target = self.resolve_path(rel_path)
        digest = compute_raw_hash(body)

        target.parent.mkdir(parents=True, exist_ok=True)
        if not self._is_within(target.parent.resolve()):
            raise RawStorePathError(f"拒绝在 raw_root 之外创建目录：{rel_path!r}")
        if target.is_symlink():
            raise RawStoreConflict(f"目标为符号链接，拒绝写入：{rel_path}")

        if target.exists():
            return self._reuse_existing(rel_path, target, body, digest)

        payload = gzip.compress(body, mtime=0)
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        with open(temp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        try:
            os.link(temp, target)
        except FileExistsError:
            # 竞态：并发发布同一身份。谁先创建谁生效，本方按"已存在"处理。
            return self._reuse_existing(rel_path, target, body, digest, temp=temp)
        except OSError as exc:
            if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
                raise RawStoreError(
                    f"文件系统不支持原子不覆盖发布（errno={exc.errno}），"
                    f"拒绝回退为覆盖写：{rel_path}"
                ) from exc
            raise

        os.unlink(temp)
        self._fsync_dir(target.parent)
        return StoredResponse(
            file_path=rel_path, raw_hash=digest, raw_bytes=len(body), reused=False
        )

    def _reuse_existing(
        self,
        rel_path: str,
        target: Path,
        body: bytes,
        digest: str,
        temp: Path | None = None,
    ) -> StoredResponse:
        """目标已存在：内容一致则复用，否则报冲突并保留临时文件供对账。"""
        try:
            existing = self._read_body(target)
        except RawStoreVerifyError as exc:
            raise RawStoreConflict(
                f"证据身份 {rel_path} 已存在但文件不可用（{exc}）：拒绝覆盖", temp_path=temp
            ) from exc
        if existing != body:
            raise RawStoreConflict(
                f"证据身份 {rel_path} 已存在且内容不同：拒绝覆盖已有证据"
                + (f"（本次内容保留在临时文件 {temp.name}）" if temp else ""),
                temp_path=temp,
            )
        if temp is not None:
            os.unlink(temp)
        return StoredResponse(
            file_path=rel_path, raw_hash=digest, raw_bytes=len(body), reused=True
        )

    # ------------------------------------------------------------------
    # 校验（只读，写入与对账共用）
    # ------------------------------------------------------------------
    def verify(self, reference) -> VerifiedBody:
        """读取并校验证据文件：解压字节、大小与哈希。

        reference 可以是相对路径字符串、StoredResponse 或 VerifiedBody，
        也可带 ``raw_hash`` / ``raw_bytes`` 属性并据此比对。只返回校验结果，
        不修改文件。
        """
        if isinstance(reference, dict):
            rel_path = reference.get("file_path")
            expected_hash = reference.get("raw_hash")
            expected_bytes = reference.get("raw_bytes")
        else:
            rel_path = getattr(reference, "file_path", reference)
            expected_hash = getattr(reference, "raw_hash", None)
            expected_bytes = getattr(reference, "raw_bytes", None)
        if not isinstance(rel_path, str):
            raise RawStoreVerifyError(f"无法从 {reference!r} 解析证据路径")

        path = self.resolve_path(rel_path)
        if not path.exists():
            raise RawStoreVerifyError(f"证据文件缺失：{rel_path}")
        if path.is_symlink():
            raise RawStoreVerifyError(f"证据路径为符号链接，拒绝作为原体读取：{rel_path}")
        if not path.is_file():
            raise RawStoreVerifyError(f"证据路径不是普通文件：{rel_path}")

        body = self._read_body(path)
        digest = compute_raw_hash(body)

        if expected_hash is not None and expected_hash != digest:
            raise RawStoreVerifyError(
                f"证据哈希不一致：{rel_path} 记录为 {expected_hash}，实算 {digest}"
            )
        if expected_bytes is not None and expected_bytes != len(body):
            raise RawStoreVerifyError(
                f"证据大小不一致：{rel_path} 记录为 {expected_bytes}，实际 {len(body)}"
            )

        return VerifiedBody(
            file_path=rel_path, body=body, raw_hash=digest, raw_bytes=len(body)
        )

    def _read_body(self, path: Path) -> bytes:
        """有限解压：最多读 max_body_bytes + 1 字节，超限即失败。"""
        try:
            with gzip.open(path, "rb") as fh:
                data = fh.read(self.max_body_bytes + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise RawStoreVerifyError(f"gzip 解压失败：{path.name}（{exc}）") from exc
        if len(data) > self.max_body_bytes:
            raise RawStoreVerifyError(
                f"解压后超过 raw_store.max_body_bytes={self.max_body_bytes}：{path.name}"
            )
        return data

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        if os.name == "nt":  # Windows 不支持对目录 fsync（os.open 目录会抛 PermissionError）
            return
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # 只读对账支持
    # ------------------------------------------------------------------
    def iter_relative_files(self) -> list[str]:
        """列出 root 下全部文件（相对 posix 路径），供只读对账使用。"""
        if not self.root.exists():
            return []
        result: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() or path.is_symlink():
                result.append(path.relative_to(self.root).as_posix())
        return result

    @staticmethod
    def is_temp_path(rel_path: str) -> bool:
        """临时文件命名：``.<目标名>.<uuid>.tmp``。"""
        name = rel_path.rsplit("/", 1)[-1]
        return name.startswith(".") and name.endswith(".tmp")


# ----------------------------------------------------------------------
# 配置解析（契约：docs/stage-2/2b-schema.md「配置与审计输出」）
# ----------------------------------------------------------------------
def resolve_raw_root(config, db_path) -> Path:
    """解析 raw_root：默认"已解析 DB 文件所在目录/raw"。

    - raw_store.root 显式覆盖；相对路径同样相对已解析 DB 目录解释
    - 不改变 database.path 原有的解析方式
    """
    db_dir = Path(db_path).expanduser().resolve().parent
    configured = config.get("raw_store", "root", default=None) if config is not None else None
    if configured:
        candidate = Path(str(configured)).expanduser()
        return candidate if candidate.is_absolute() else (db_dir / candidate).resolve()
    return (db_dir / "raw").resolve()


def resolve_max_body_bytes(config) -> int:
    """解析 raw_store.max_body_bytes（默认 32 MiB，正整数）。"""
    value = config.get("raw_store", "max_body_bytes", default=DEFAULT_MAX_BODY_BYTES) \
        if config is not None else DEFAULT_MAX_BODY_BYTES
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"raw_store.max_body_bytes 必须为正整数，当前为 {value!r}")
    return value


def build_raw_store(config, db_path) -> RawStore:
    """按配置构建 RawStore（默认根为 DB 所在目录/raw）。"""
    return RawStore(resolve_raw_root(config, db_path), resolve_max_body_bytes(config))


def build_readonly_raw_store(config, db_path) -> RawStore:
    """只读对账用 RawStore：不创建根目录，缺根即失败。"""
    return RawStore(resolve_raw_root(config, db_path), resolve_max_body_bytes(config),
                   readonly=True)
