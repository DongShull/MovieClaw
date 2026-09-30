"""片段窗口字幕：只抽一小段时间里的内封字幕（刷片，docs/design/reels.md）。

整轨抽取要通读整个容器，刷片一条只放四五十秒、等不起；窗口抽取只读窗口那一段，
时间戳保持文件时间。真 ffmpeg 的用例标 integration（CI 跳过，本机有 ffmpeg 时跑）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import movieclaw_api.services.media_extract as media_extract
from movieclaw_db.models import FileSource, FileState, LibraryFile


def make_file(video: Path, codecs: list[str]) -> LibraryFile:
    return LibraryFile(
        id=9,
        library_id=1,
        media_item_id=1,
        file_path=str(video),
        size_bytes=1,
        source=FileSource.SCANNED,
        state=FileState.IN_PLACE,
        container="mkv",
        subtitle_streams=[{"codec": codec} for codec in codecs],
    )


@pytest.fixture
def cache(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "cache"
    monkeypatch.setattr(media_extract, "cache_dir", lambda: directory)
    return directory


# ---------------------------------------------------------------------------
# 哪些轨能做窗口、窗口怎么取整
# ---------------------------------------------------------------------------


def test_only_copyable_text_tracks_get_a_window(tmp_path: Path):
    file = make_file(tmp_path / "v.mkv", ["subrip", "ass", "ssa", "hdmv_pgs_subtitle", "mov_text"])
    assert [media_extract.window_format(file, i) for i in range(5)] == [
        "srt",
        "ass",
        "ass",
        None,  # 图形字幕：转码时由服务端压制
        None,  # mov_text 要转码才进得了 srt，重编码不认输入端 -t、会读到文件尾
    ]
    assert media_extract.window_format(file, 9) is None


def test_window_is_rounded_to_whole_seconds_and_capped(tmp_path: Path, cache: Path):
    file = make_file(tmp_path / "v.mkv", ["subrip"])
    spec = media_extract._window_spec(file, 0, 1_572_956, 1_630_956)
    assert (spec.start_s, spec.end_s) == (1572, 1631)
    assert spec.out_path == cache / "9.s0.w1572-1631.srt"
    # 超长窗口封顶：不能拿它当整轨抽取用
    huge = media_extract._window_spec(file, 0, 0, 7_200_000)
    assert huge.end_s - huge.start_s == media_extract.WINDOW_MAX_SECONDS


def test_full_track_cache_is_served_instead_of_a_window(tmp_path: Path, cache: Path):
    video = tmp_path / "v.mkv"
    video.write_bytes(b"source")
    cache.mkdir()
    full = cache / "9.s0.srt"
    full.write_text("1\n00:00:01,000 --> 00:00:02,000\n整轨\n", encoding="utf-8")
    os.utime(full, ns=(video.stat().st_mtime_ns + 10**9,) * 2)
    spec = media_extract._window_spec(make_file(video, ["subrip"]), 0, 1_000, 60_000)
    track = media_extract._extract_window(spec)
    assert track is not None and track.path == full


# ---------------------------------------------------------------------------
# 真抽取
# ---------------------------------------------------------------------------

integration = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要系统 ffmpeg")


@pytest.fixture
def long_video(tmp_path: Path) -> Path:
    """30 秒的 MKV：内封 SRT 与 ASS 各三句，分别在 2、15、27 秒。"""
    srt = tmp_path / "plain.srt"
    srt.write_text(
        "1\n00:00:02,000 --> 00:00:03,000\n开头那句\n\n"
        "2\n00:00:15,000 --> 00:00:16,500\n窗口里这句\n\n"
        "3\n00:00:27,000 --> 00:00:28,000\n结尾那句\n\n",
        encoding="utf-8",
    )
    ass = tmp_path / "styled.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour\nStyle: Karaoke,Arial,72,&H00FF00FF\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,Karaoke,开头那句\n"
        "Dialogue: 0,0:00:15.00,0:00:16.50,Karaoke,{\\pos(320,240)}窗口里这句\n"
        "Dialogue: 0,0:00:27.00,0:00:28.00,Karaoke,结尾那句\n",
        encoding="utf-8",
    )
    video = tmp_path / "long.mkv"
    proc = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10:duration=30",
            "-i",
            str(srt),
            "-i",
            str(ass),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-map",
            "2:s",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "20",
            "-c:s:0",
            "srt",
            "-c:s:1",
            "ass",
            "-y",
            str(video),
        ],  # fmt: skip
        capture_output=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-1500:]
    return video


def _cues(text: str) -> list[str]:
    return re.findall(r"(\d+:\d\d:\d\d[.,]\d+)", text)


@integration
@pytest.mark.integration
async def test_srt_window_keeps_only_that_stretch_in_file_time(long_video: Path, cache: Path):
    file = make_file(long_video, ["subrip", "ass"])
    track = await media_extract.extract_track_window_async(file, 0, 12_000, 20_000)
    assert track is not None and track.format == "srt"
    text = track.path.read_text(encoding="utf-8")
    assert "窗口里这句" in text
    assert "开头那句" not in text and "结尾那句" not in text
    # 时间戳仍是文件时间（15 秒），不是从窗口起点重新计时
    assert _cues(text)[0] == "00:00:15,000"


@integration
@pytest.mark.integration
async def test_ass_window_keeps_styles(long_video: Path, cache: Path):
    file = make_file(long_video, ["subrip", "ass"])
    track = await media_extract.extract_track_window_async(file, 1, 12_000, 20_000)
    assert track is not None and track.format == "ass"
    text = track.path.read_text(encoding="utf-8")
    assert "[V4+ Styles]" in text and "Karaoke" in text
    assert "\\pos(320,240)" in text and "窗口里这句" in text
    assert "结尾那句" not in text
    assert "0:00:15.00" in text


@integration
@pytest.mark.integration
async def test_window_without_dialogue_is_cached_as_empty(long_video: Path, cache: Path):
    file = make_file(long_video, ["subrip", "ass"])
    first = await media_extract.extract_track_window_async(file, 0, 5_000, 10_000)
    assert first is not None and first.path.read_text(encoding="utf-8").strip() == ""
    # 第二次直接命中缓存，不再起 ffmpeg
    stamp = first.path.stat().st_mtime_ns
    again = await media_extract.extract_track_window_async(file, 0, 5_000, 10_000)
    assert again is not None and again.path.stat().st_mtime_ns == stamp
