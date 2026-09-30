"""落位原语（fsops.rename_no_replace）与三处调用点的防回退测试。

背景（issue #103）：极空间的 FUSE 存储对硬链接不产生索引事件，直接
os.link 到最终名的文件极影视永远看不到，落位因此改为「备好内容 →
rename 发布」。本文件锁死改造后的关键语义：

- 原语：改名成功 / 目标已存在原子拒绝 / NOREPLACE 不支持时的回退路径
  行为一致 / 回退路径绝不使用 os.link；
- ingest._transfer：硬链与复制的最终产物不变（inode 关系、内容）、
  临时文件必被清理、发布竞态输为 IngestError 不覆盖；
- organize._move_no_clobber：改名生效、占用拒绝、EXDEV 中文报错。
"""

from __future__ import annotations

import errno
import os

import pytest

import movieclaw_api.services.library.fsops as fsops
import movieclaw_api.services.library.ingest as ingest_mod
import movieclaw_api.services.library.organize as organize_mod
from movieclaw_api.services.library.fsops import rename_no_replace
from movieclaw_api.services.library.ingest import IngestError, _transfer
from movieclaw_api.services.library.organize import _move_no_clobber, _MoveError

# ---------------------------------------------------------------------------
# rename_no_replace 原语
# ---------------------------------------------------------------------------


def test_rename_no_replace_moves_file(tmp_path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dst = tmp_path / "b.mkv"
    rename_no_replace(src, dst)
    assert not src.exists()
    assert dst.read_bytes() == b"payload"


def test_rename_no_replace_refuses_existing_target(tmp_path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"new")
    dst = tmp_path / "b.mkv"
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        rename_no_replace(src, dst)
    # 源与目标都原样：既没搬走也没覆盖
    assert src.read_bytes() == b"new"
    assert dst.read_bytes() == b"old"


@pytest.fixture
def force_fallback(monkeypatch):
    """模拟文件系统不支持 RENAME_NOREPLACE（极空间的 zfuse 场景）。"""

    def _unsupported(*args):
        import ctypes

        ctypes.set_errno(errno.EINVAL)
        return -1

    monkeypatch.setattr(fsops, "_renameat2", _unsupported)


def test_fallback_moves_file(tmp_path, force_fallback):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    dst = tmp_path / "b.mkv"
    rename_no_replace(src, dst)
    assert not src.exists()
    assert dst.read_bytes() == b"payload"


def test_fallback_refuses_existing_target(tmp_path, force_fallback):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"new")
    dst = tmp_path / "b.mkv"
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        rename_no_replace(src, dst)
    assert dst.read_bytes() == b"old"


def test_fallback_never_uses_link(tmp_path, force_fallback, monkeypatch):
    """回退路径若退回 os.link，等于在极空间上重新引入索引盲区——锁死。"""

    def _forbidden(*args, **kwargs):
        raise AssertionError("回退路径不得调用 os.link")

    monkeypatch.setattr(os, "link", _forbidden)
    src = tmp_path / "a.mkv"
    src.write_bytes(b"payload")
    rename_no_replace(src, tmp_path / "b.mkv")


def test_real_oserror_propagates(tmp_path):
    # 跨设备等真实错误必须原样抛出（EXDEV 由调用方翻译成中文引导）
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    with pytest.raises(OSError) as exc_info:
        rename_no_replace(src, tmp_path / "no-such-dir" / "b.mkv")
    assert not isinstance(exc_info.value, FileExistsError)
    assert src.exists()


# ---------------------------------------------------------------------------
# ingest._transfer
# ---------------------------------------------------------------------------


def _no_leftover_tmp(directory) -> bool:
    return not list(directory.glob("*.part"))


def test_transfer_hardlink_same_inode_and_clean(tmp_path):
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "lib" / "电影 (2024)" / "电影 (2024).mkv"
    final = _transfer(src, dst, "hardlink", "v2")
    assert final == dst
    assert dst.stat().st_ino == src.stat().st_ino  # 仍是硬链接：保种零占用
    assert _no_leftover_tmp(dst.parent)  # 临时名不残留


def test_transfer_copy_new_inode_and_clean(tmp_path):
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "lib" / "电影 (2024)" / "电影 (2024).mkv"
    final = _transfer(src, dst, "copy", "v2")
    assert final == dst
    assert dst.read_bytes() == b"data"
    assert dst.stat().st_ino != src.stat().st_ino
    assert _no_leftover_tmp(dst.parent)


def test_transfer_dedupes_same_payload(tmp_path):
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "lib" / "电影 (2024).mkv"
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.link(src, dst)  # 已入库（同 inode）
    assert _transfer(src, dst, "hardlink", "v2") is None


def test_transfer_version_label_on_conflict(tmp_path):
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data-1080p!")
    dst = tmp_path / "lib" / "电影 (2024).mkv"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"other")  # 已有不同内容的同名文件
    final = _transfer(src, dst, "hardlink", "1080p")
    assert final == dst.with_name("电影 (2024) - 1080p.mkv")
    assert final.stat().st_ino == src.stat().st_ino


@pytest.mark.parametrize("strategy", ["copy", "hardlink"])
def test_transfer_base_freed_by_upgrade_still_dedupes_version_file(tmp_path, strategy):
    """洗版后基础名被让出：新版本早先落在「- 标签」退让名，旧版进回收站后
    基础名空了。同一来件再处理时必须认出退让名已是同内容，不得整份再落到
    基础名（线上实测 58.7 GB Remux 重复入库）。"""
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"remux-payload")
    dst = tmp_path / "lib" / "电影 (2019).mkv"
    dst.parent.mkdir(parents=True)
    first = _transfer(src, dst.with_name("电影 (2019) - 2160p.mkv"), strategy, "x")
    assert first is not None and first.exists()
    assert not dst.exists()  # 旧版已进回收站，基础名空着

    assert _transfer(src, dst, strategy, "2160p") is None
    assert not dst.exists()
    assert sorted(p.name for p in dst.parent.iterdir()) == ["电影 (2019) - 2160p.mkv"]


def test_transfer_base_free_with_different_version_file_lands_on_base(tmp_path):
    """退让名上是另一份内容时基础名照常可用：修复只挡同内容，不改变落位约定。"""
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"new-content")
    dst = tmp_path / "lib" / "电影 (2019).mkv"
    dst.parent.mkdir(parents=True)
    dst.with_name("电影 (2019) - 2160p.mkv").write_bytes(b"other")
    assert _transfer(src, dst, "copy", "2160p") == dst
    assert dst.read_bytes() == b"new-content"


def test_transfer_publish_race_raises_not_clobbers(tmp_path, monkeypatch):
    """exists 预检通过后、发布前目标恰好落地（并发整理）：报冲突不覆盖。"""
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "lib" / "电影 (2024).mkv"

    def _race(a, b):
        raise FileExistsError(errno.EEXIST, "File exists", str(a), None, str(b))

    monkeypatch.setattr(ingest_mod, "rename_no_replace", _race)
    with pytest.raises(IngestError, match="跳过以免覆盖"):
        _transfer(src, dst, "hardlink", "v2")
    assert _no_leftover_tmp(dst.parent)  # 失败路径同样不残留临时名


def test_transfer_failure_cleans_tmp(tmp_path, monkeypatch):
    src = tmp_path / "inbox" / "movie.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "lib" / "电影 (2024).mkv"

    def _boom(a, b):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(ingest_mod, "rename_no_replace", _boom)
    with pytest.raises(IngestError):
        _transfer(src, dst, "hardlink", "v2")
    assert _no_leftover_tmp(dst.parent)


# ---------------------------------------------------------------------------
# organize._move_no_clobber
# ---------------------------------------------------------------------------


def test_move_no_clobber_renames(tmp_path):
    src = tmp_path / "root" / "旧名.mkv"
    src.parent.mkdir()
    src.write_bytes(b"data")
    dst = tmp_path / "root" / "电影 (2024)" / "电影 (2024).mkv"
    _move_no_clobber(src, dst)
    assert not src.exists()
    assert dst.read_bytes() == b"data"


def test_move_no_clobber_refuses_occupied_target(tmp_path):
    src = tmp_path / "旧名.mkv"
    src.write_bytes(b"new")
    dst = tmp_path / "规范名.mkv"
    dst.write_bytes(b"old")
    with pytest.raises(_MoveError, match="跳过以免覆盖"):
        _move_no_clobber(src, dst)
    assert dst.read_bytes() == b"old"
    assert src.exists()


def test_move_no_clobber_exdev_message(tmp_path, monkeypatch):
    src = tmp_path / "旧名.mkv"
    src.write_bytes(b"data")

    def _exdev(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(organize_mod, "rename_no_replace", _exdev)
    with pytest.raises(_MoveError, match="不在同一文件系统"):
        _move_no_clobber(src, tmp_path / "新名.mkv")


def test_move_no_clobber_missing_source(tmp_path):
    with pytest.raises(_MoveError, match="源文件已不在原位"):
        _move_no_clobber(tmp_path / "不存在.mkv", tmp_path / "新名.mkv")
