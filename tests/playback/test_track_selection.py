"""轨记忆只记用户的选择（``movieclaw_playback.state.apply_track_selection``）。

播放器上报的是「正在放的轨」，多数只是默认挑选的结果。照单全收会把默认挑选冻成
「用户选的」：默认策略以后改了这些条目跟不上，自动落成的「关闭」还会连带整部剧不开
字幕。这里测规则本身：和默认挑选相同的不记（选回默认就清空），不同的照记，服务端
判断不了的文件（原盘、没探测过轨、不知道是哪个文件）照原样记。
"""

from __future__ import annotations

from movieclaw_db.models import FileSource, LibraryFile, PlaybackState
from movieclaw_playback.state import apply_track_selection, track_report_changes


def _file(**fields) -> LibraryFile:
    return LibraryFile(library_id=1, file_path="/m/x.mkv", size_bytes=1,
                       source=FileSource.SCANNED, **fields)


#: 英语全景声标默认、国语 AC3 第二条；内封英文字幕、中文字幕标默认
MOVIE = _file(
    container="mkv",
    audio_streams=[
        {"codec": "truehd", "channels": 8, "language": "eng", "default": True},
        {"codec": "ac3", "channels": 6, "language": "chi", "default": False},
    ],
    subtitle_streams=[
        {"codec": "subrip", "language": "eng", "default": False, "forced": False},
        {"codec": "subrip", "language": "chi", "default": True, "forced": False},
    ],
    external_subtitles=[],
)


def _row(**tracks) -> PlaybackState:
    return PlaybackState(member_id=1, media_item_id=1, **tracks)


def test_default_choices_are_not_remembered():
    """播放器自己挑的就是默认轨：不记，也不动更新时间（不产生写库）。"""
    row = _row()
    before = row.updated_at
    apply_track_selection(row, audio_track="embedded:0", subtitle_track="embedded:1", files=[MOVIE])
    assert (row.audio_track, row.subtitle_track) == (None, None)
    assert row.updated_at == before


def test_changed_choices_are_remembered():
    row = _row()
    apply_track_selection(row, audio_track="embedded:1", subtitle_track="embedded:0", files=[MOVIE])
    assert (row.audio_track, row.subtitle_track) == ("embedded:1", "embedded:0")


def test_picking_the_default_again_clears_the_memory():
    """用户在菜单里特意选回默认那条：清空和记住效果一样，清空后还能跟上默认策略的调整。"""
    row = _row(audio_track="embedded:1", subtitle_track="off")
    apply_track_selection(row, audio_track="embedded:0", subtitle_track="embedded:1", files=[MOVIE])
    assert (row.audio_track, row.subtitle_track) == (None, None)


def test_off_is_a_choice_only_when_the_file_would_show_subtitles():
    """文件有默认字幕时关掉是用户的选择；本来就没有默认字幕时「关闭」只是默认状态。"""
    row = _row()
    apply_track_selection(row, subtitle_track="off", files=[MOVIE])
    assert row.subtitle_track == "off"

    bare = _file(container="mkv", subtitle_streams=[
        {"codec": "subrip", "language": "eng", "default": False, "forced": False},
    ], external_subtitles=[])
    row = _row()
    apply_track_selection(row, subtitle_track="off", files=[bare])
    assert row.subtitle_track is None


def test_installed_external_subtitle_is_the_default():
    """装了外挂字幕就默认选外挂（全端同一套默认字幕策略）：报它不算用户换过。"""
    with_external = _file(
        container="mkv",
        subtitle_streams=MOVIE.subtitle_streams,
        external_subtitles=[{"filename": "x.chs.srt", "format": "srt", "language": "chi",
                             "title": None, "forced": False}],
    )
    row = _row()
    apply_track_selection(row, subtitle_track="external:x.chs.srt", files=[with_external])
    assert row.subtitle_track is None
    apply_track_selection(row, subtitle_track="embedded:1", files=[with_external])
    assert row.subtitle_track == "embedded:1"


def test_tracks_the_server_cannot_judge_are_kept_as_reported():
    """原盘的轨服务端读不到、没探测过内封字幕的旧行不知道默认是哪条、不知道放的是哪个
    文件——都照上报原样记（和改口径之前一样），宁可多记也不能丢掉用户的选择。"""
    disc = _file(container="bluray", audio_streams=MOVIE.audio_streams,
                 subtitle_streams=MOVIE.subtitle_streams)
    row = _row()
    apply_track_selection(row, audio_track="embedded:0", files=[disc])
    assert row.audio_track == "embedded:0"

    unprobed = _file(container="mkv", audio_streams=MOVIE.audio_streams, subtitle_streams=None)
    row = _row()
    apply_track_selection(row, subtitle_track="off", files=[unprobed])
    assert row.subtitle_track == "off"

    row = _row()
    apply_track_selection(row, audio_track="embedded:0", subtitle_track="off")
    assert (row.audio_track, row.subtitle_track) == ("embedded:0", "off")


def test_any_version_counts_when_the_played_version_is_unknown():
    """多版本又没说放的是哪个：任一版本的默认挑选对得上就不记（调用方知道版本时只给那一个）。"""
    zh_first = _file(container="mkv", audio_streams=[
        {"codec": "aac", "channels": 2, "language": "chi", "default": True},
        {"codec": "aac", "channels": 2, "language": "eng", "default": False},
    ])
    row = _row()
    apply_track_selection(row, audio_track="embedded:1", files=[MOVIE, zh_first])
    assert row.audio_track == "embedded:1"  # 两个版本的默认都是 0
    apply_track_selection(row, audio_track="embedded:0", files=[MOVIE, zh_first])
    assert row.audio_track is None


def test_report_changes_only_when_a_track_differs_from_memory():
    row = _row(audio_track="embedded:1")
    assert not track_report_changes(row)
    assert not track_report_changes(row, audio_track="embedded:1")
    assert track_report_changes(row, audio_track="embedded:0")
    assert track_report_changes(row, subtitle_track="off")
