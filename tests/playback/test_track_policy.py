"""默认音轨 / 字幕策略（``movieclaw_playback.track_policy``）。

音轨跟片子走（原声），字幕跟人走（库语言）。
"""

from __future__ import annotations

from movieclaw_db.models import FileSource, LibraryFile
from movieclaw_playback.subtitles import pick_default_subtitle
from movieclaw_playback.track_policy import (
    TrackContext,
    _external_marks,
    default_audio,
    default_subtitle,
    default_tracks,
)

ZH = TrackContext.build("zh-CN", "en")  # 中文库里的英文片
ZH_CN_FILM = TrackContext.build("zh-CN", "zh")  # 中文库里的国产片
EN = TrackContext.build("en-US", "en")  # 英文库里的英文片


def _file(audio=(), subs=(), externals=(), path="/m/Movie.2010.1080p.mkv") -> LibraryFile:
    return LibraryFile(
        library_id=1,
        file_path=path,
        size_bytes=1,
        source=FileSource.SCANNED,
        audio_streams=list(audio),
        subtitle_streams=list(subs),
        external_subtitles=list(externals),
    )


def _a(language, codec="ac3", channels=6, default=False, title=None, profile=None) -> dict:
    return {
        "codec": codec,
        "channels": channels,
        "language": language,
        "default": default,
        "title": title,
        "profile": profile,
    }


def _s(language, default=False, forced=False, title=None, codec="subrip") -> dict:
    return {
        "codec": codec,
        "language": language,
        "default": default,
        "forced": forced,
        "title": title,
    }


def _x(filename, language=None, title=None, **flags) -> dict:
    return {
        "filename": filename,
        "format": filename.rsplit(".", 1)[-1],
        "language": language,
        "title": title,
        "default": False,
        "forced": False,
        "sdh": False,
        **flags,
    }


# ---------------------------------------------------------------------------
# 音轨：按原声，同语言挑音质最好的
# ---------------------------------------------------------------------------


def test_audio_follows_the_original_language_and_takes_the_best_quality():
    """国语配音标了默认，英语原声有全景声和 AC3 两条：放英语、挑无损那条。"""
    f = _file(audio=[_a("chi", default=True), _a("eng", "truehd", 8), _a("eng", "ac3", 6)])
    assert default_audio(f, ZH) == default_audio(f, ZH).__class__(1, "original_language")


def test_the_container_default_wins_among_original_language_tracks():
    """英语 AC3 标了默认、另有英语 TrueHD：放标了默认的那条——直出放的就是它，
    不为多一点音质让网页端重封装、App 本机转音频。"""
    f = _file(audio=[_a("eng", "ac3", 6, default=True), _a("eng", "truehd", 8)])
    choice = default_audio(f, ZH)
    assert (choice.index, choice.reason) == (0, "original_language")


def test_hong_kong_films_default_to_cantonese_even_when_tagged_chi():
    """TMDB 的 cn 是粤语；很多片把粤语也标成 chi，标题里写着「粤语」就认它。"""
    f = _file(audio=[_a("chi", default=True, title="国语"), _a("chi", title="粤语")])
    choice = default_audio(f, TrackContext.build("zh-CN", "cn"))
    assert (choice.index, choice.reason) == (1, "original_language")


def test_commentary_and_unrecognized_tracks_are_never_the_automatic_pick():
    f = _file(
        audio=[
            _a("eng", "truehd", 8, title="Director's Commentary"),
            _a("eng", None, 6),  # 认不出编码（如菁彩声）
            _a("eng", "dts", 6, profile="DTS-HD MA"),
        ]
    )
    assert default_audio(f, ZH).index == 2


def test_audio_falls_back_to_the_container_default_without_an_original_language_track():
    f = _file(audio=[_a("eng"), _a("chi", default=True)])
    choice = default_audio(f, TrackContext.build("zh-CN", "ja"))
    assert (choice.index, choice.reason) == (1, "default_flag")
    assert default_audio(f).index == 1  # 没有上下文：旧规则


def test_audio_without_any_default_flag_takes_the_first_track():
    f = _file(audio=[_a("eng"), _a("fre")])
    assert (default_audio(f).index, default_audio(f).reason) == (0, "first")
    assert default_audio(_file()).index is None


# ---------------------------------------------------------------------------
# 字幕：按库语言，再看原声
# ---------------------------------------------------------------------------


def test_foreign_audio_turns_on_library_language_subtitles_even_without_a_default_flag():
    """英语默认旗标在英文字幕上、中文字幕没标默认：中文库照样开中文。"""
    f = _file(audio=[_a("eng", default=True)], subs=[_s("eng", default=True), _s("chi")])
    assert default_subtitle(f, ZH) == default_subtitle(f, ZH).__class__(
        "embedded:1", "library_language"
    )


def test_simplified_chinese_libraries_prefer_simplified_subtitles():
    f = _file(
        audio=[_a("eng")],
        subs=[_s("chi", title="繁體中文", default=True), _s("chi", title="简体中文")],
    )
    assert default_subtitle(f, ZH).ref == "embedded:1"
    tw = TrackContext.build("zh-TW", "en")
    assert default_subtitle(f, tw).ref == "embedded:0"


def test_chinese_libraries_keep_subtitles_on_for_chinese_films():
    """国内观众看国产片也习惯开中文字幕。"""
    f = _file(audio=[_a("chi", default=True)], subs=[_s("chi")])
    assert default_subtitle(f, ZH_CN_FILM).reason == "library_language"


def test_same_language_audio_in_other_libraries_only_turns_on_forced_subtitles():
    """英文库里的英文片：只开英语强制字幕（片中外语对白的翻译），没有就不开。"""
    f = _file(
        audio=[_a("eng", default=True)], subs=[_s("eng", default=True), _s("eng", forced=True)]
    )
    assert default_subtitle(f, EN) == default_subtitle(f, EN).__class__("embedded:1", "forced")
    plain = _file(audio=[_a("eng", default=True)], subs=[_s("eng", default=True)])
    assert default_subtitle(plain, EN) == default_subtitle(plain, EN).__class__(
        None, "same_language_off"
    )


def test_foreign_audio_in_an_english_library_turns_on_english_subtitles():
    f = _file(audio=[_a("fre", default=True)], subs=[_s("fre", default=True), _s("eng")])
    assert default_subtitle(f, TrackContext.build("en-US", "fr")).ref == "embedded:1"


def test_picking_another_audio_track_changes_the_subtitle_decision():
    """英文库：放英语原声时只开强制字幕；用户换成法语配音，就该开英文字幕了。"""
    f = _file(audio=[_a("eng", default=True), _a("fre")], subs=[_s("eng")])
    assert default_tracks(f, EN)[1].ref is None
    assert default_tracks(f, EN, preferred_audio="embedded:1")[1].ref == "embedded:0"


def test_commentary_subtitles_are_skipped_and_sdh_ranks_after_regular_ones():
    f = _file(
        audio=[_a("eng")],
        subs=[_s("chi", title="导评", default=True), _s("chi", title="SDH"), _s("chi")],
    )
    assert default_subtitle(f, ZH).ref == "embedded:2"


def test_ai_then_external_subtitles_win_among_the_same_language():
    f = _file(
        audio=[_a("eng")],
        subs=[_s("chi", default=True, title="简体")],
        externals=[
            _x("Movie.2010.1080p.chs&eng.ass", title="chs&eng"),
            _x("Movie.2010.1080p.ai-zh-Hans.srt", "chi", title="ai-zh-Hans"),
        ],
    )
    assert default_subtitle(f, ZH).ref == "external:Movie.2010.1080p.ai-zh-Hans.srt"
    no_ai = _file(
        audio=[_a("eng")],
        subs=[_s("chi", default=True, title="简体")],
        externals=[_x("Movie.2010.1080p.chs&eng.ass", title="chs&eng")],
    )
    assert default_subtitle(no_ai, ZH).ref == "external:Movie.2010.1080p.chs&eng.ass"


def test_chinese_films_without_chinese_subtitles_stay_off_rather_than_showing_english():
    """国产片没有中文字幕时不开，不能给国产片挂一条英文字幕。"""
    f = _file(audio=[_a("chi", default=True)], subs=[_s("eng", default=True)])
    assert default_subtitle(f, ZH_CN_FILM) == default_subtitle(f, ZH_CN_FILM).__class__(
        None, "no_language_match"
    )


def test_foreign_audio_without_library_language_subtitles_falls_back_to_the_old_rules():
    f = _file(
        audio=[_a("eng")],
        subs=[_s("eng", default=True)],
        externals=[_x("Movie.2010.1080p.en.srt", "eng")],
    )
    assert default_subtitle(f, ZH) == default_subtitle(f, ZH).__class__(
        "external:Movie.2010.1080p.en.srt", "external"
    )


def test_without_context_the_subtitle_decision_equals_the_old_rules():
    f = _file(
        audio=[_a("eng")],
        subs=[_s("eng", default=True), _s("chi")],
        externals=[_x("Movie.2010.1080p.srt")],
    )
    assert default_subtitle(f).ref == pick_default_subtitle(f)


def test_only_the_marks_after_the_video_name_are_read_from_external_filenames():
    """视频名里的 TC（枪版）、Chinese 不能被当成繁体 / 中文。"""
    f = _file(path="/m/The.Chinese.Connection.1971.TC.mkv")
    assert _external_marks(f, "The.Chinese.Connection.1971.TC.eng.srt") == "eng"
    assert _external_marks(f, "Other.srt") == ""
    tc = _file(
        audio=[_a("eng")],
        path="/m/The.Chinese.Connection.1971.TC.mkv",
        externals=[_x("The.Chinese.Connection.1971.TC.srt")],
    )
    assert default_subtitle(tc, ZH).reason != "library_language"


def test_library_locale_and_original_language_are_normalized():
    ctx = TrackContext.build("zh-TW", "cn")
    assert (ctx.library_language, ctx.library_script, ctx.original_language) == (
        "chi",
        "hant",
        "yue",
    )
    assert TrackContext.build("en-US", "zh") == TrackContext("eng", None, "chi")
    assert TrackContext.build(None, None) == TrackContext()


# ---------------------------------------------------------------------------
# 只有标签明确说片源默认的不对才换（2026-09-30 NAS 全库核对发现的四类误判）
# ---------------------------------------------------------------------------


def test_tmdb_zh_counts_cantonese_as_original_too():
    """《麦兜故事》：TMDB 标 zh（分不清普通话、粤语），片源默认粤语——不能换成国语 2.0。"""
    f = _file(
        audio=[
            _a("chi", "dts", 6, default=True, title="Cantonese DTS 5.1"),
            _a("chi", "ac3", 2, title="Mandarin AC3 2.0"),
        ]
    )
    assert default_audio(f, ZH_CN_FILM).index == 0


def test_an_unlabeled_default_audio_track_is_kept():
    """《鹊刀门传奇》：默认轨没标语言（und），另一条标着 zho——标签没说默认轨不对，不换。"""
    f = _file(audio=[_a("und", "aac", 2, default=True), _a("zho", "eac3", 6)])
    assert default_audio(f, ZH_CN_FILM).index == 0


def test_several_default_flags_keep_the_first_one():
    """《漂白》《银河护卫队3》：两条原声都标了默认，放第一条（直出放的就是它），不为音质换第二条。"""
    f = _file(audio=[_a("chi", "aac", 2, default=True), _a("chi", "eac3", 6, default=True)])
    choice = default_audio(f, ZH_CN_FILM)
    assert (choice.index, choice.reason) == (0, "original_language")


def test_an_unlabeled_default_subtitle_stays_on_for_chinese_films():
    """《如果历史是一群喵》：国产片的默认字幕没标语言，多半就是中文字幕——不关。"""
    f = _file(audio=[_a("chi", default=True)], subs=[_s(None, default=True)])
    assert default_subtitle(f, ZH_CN_FILM).ref == "embedded:0"
    labeled = _file(audio=[_a("chi", default=True)], subs=[_s("jpn", default=True)])
    assert default_subtitle(labeled, ZH_CN_FILM).ref is None
