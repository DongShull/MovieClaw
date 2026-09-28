"""身份匹配的派生数据缓存与必要条件预筛：只改快慢，不改判定结论。

必要条件（may_match / 检索文本里的别名子串）是发布预测 SQL 预筛与逐对匹配提前
返回的正确性基础：凡是 match_identity 会命中的组合，必要条件都必须成立。这里拿
test_identity 的全部用例（含 2026-07 真实样本）逐一核对，再补几类构造样本。
"""

from __future__ import annotations

import inspect

import pytest

from movieclaw_enrich.models import TorrentAttrs
from movieclaw_matcher import MediaIdentity, TorrentCandidate, match_identity
from movieclaw_matcher.identity import (
    MATCH_TEXT_SEPARATOR,
    alias_needles,
    candidate_match_text,
    match_text,
    may_match,
)

from . import test_identity as corpus


def _candidate(title: str, subtitle: str = "", **attrs) -> TorrentCandidate:
    return TorrentCandidate(
        site_id="test", torrent_id="1", title=title, subtitle=subtitle, attrs=TorrentAttrs(**attrs)
    )


def test_needles_are_normalized_deduplicated_and_minimal() -> None:
    """同形别名只留一条；包含另一条更短别名的冗余项去掉（"三体" 在，"三体第一季" 必在）。"""
    media = MediaIdentity(
        kind="tv", year=2023, aliases=("三体", "三體", "三体第一季", "Three-Body", "three body", "")
    )
    assert set(alias_needles(media)) == {"三体", "三體", "threebody"}
    assert alias_needles(MediaIdentity(kind="movie", year=2020, aliases=())) == ()


def test_match_text_covers_every_title_source() -> None:
    text = match_text("Ｔｈｅ．Ｓｈｏｗ S01E02", "中文名／别名｜类型", ["片名"], ["Show Name"])
    assert text.split(MATCH_TEXT_SEPARATOR) == [
        "theshows01e02",
        "中文名别名类型",
        "片名",
        "showname",
    ]


def test_necessary_condition_holds_on_identity_corpus(monkeypatch) -> None:
    """test_identity 全部用例里，每一次命中的 (候选, 条目) 都必须满足必要条件。"""
    observed: list[tuple[TorrentCandidate, MediaIdentity, object]] = []
    real = corpus.match_identity

    def spy(candidate, media):
        result = real(candidate, media)
        observed.append((candidate, media, result))
        return result

    monkeypatch.setattr(corpus, "match_identity", spy)
    ran = 0
    for name, fn in inspect.getmembers(corpus, inspect.isfunction):
        if name.startswith("test_") and not inspect.signature(fn).parameters:
            fn()
            ran += 1
    hits = [(c, m) for c, m, result in observed if result is not None]
    assert ran >= 40 and len(hits) >= 20, "语料用例数异常，检查 test_identity 是否改名/改结构"
    missed = [(c.title, c.subtitle, m.aliases) for c, m in hits if not may_match(c, m)]
    assert missed == []


@pytest.mark.parametrize(
    ("candidate", "media"),
    [
        # 季名组合等式：片名段恰好等于"别名+季名"
        (
            _candidate("中餐厅·南洋拾光季 第1期", media_type="tv", seasons=[10], episodes=[1]),
            MediaIdentity(
                kind="tv",
                year=2025,
                aliases=("中餐厅",),
                season_numbers=(10,),
                season_titles=("南洋拾光季",),
            ),
        ),
        # 只有 NER 片名里有别名，标题副标题都是拼音
        (
            _candidate(
                "Wo Bu Shi Da Shi S01E05",
                media_type="tv",
                seasons=[1],
                episodes=[5],
                titles_zh=["我不是大师"],
            ),
            MediaIdentity(kind="tv", year=2026, aliases=("我不是大师",), season_numbers=(1,)),
        ),
        # 短别名整词 + 年份相等
        (
            _candidate("Her.2013.1080p", "她 / 云端情人", media_type="movie", year=2013),
            MediaIdentity(kind="movie", year=2013, aliases=("她", "Her")),
        ),
        # 全角标题
        (
            _candidate(
                "Ｔｅｓｔ　Ｓｈｏｗ　Ｓ０１Ｅ０１", media_type="tv", seasons=[1], episodes=[1]
            ),
            MediaIdentity(kind="tv", year=None, aliases=("Test Show",), season_numbers=(1,)),
        ),
    ],
)
def test_constructed_hits_satisfy_necessary_condition(candidate, media) -> None:
    assert match_identity(candidate, media) is not None
    assert may_match(candidate, media)


def test_external_id_alone_satisfies_necessary_condition() -> None:
    candidate = TorrentCandidate(
        site_id="s",
        torrent_id="1",
        title="Totally Unrelated 2019",
        subtitle="",
        attrs=TorrentAttrs(media_type="movie", year=2019),
        imdb_id="tt1798709",
    )
    media = MediaIdentity(kind="movie", year=2013, aliases=("Her",), imdb_id="tt1798709")
    assert match_identity(candidate, media) is not None
    assert may_match(candidate, media)


def test_cached_derivations_do_not_leak_between_identities() -> None:
    """同一个候选依次比对多个条目（被动匹配的真实形态），结论与各用新对象现算一致。"""
    identities = [
        MediaIdentity(kind="tv", year=2024, aliases=("测试剧集", "Test Show"), season_numbers=(1,)),
        MediaIdentity(kind="tv", year=2024, aliases=("Test",), season_numbers=(1,)),
        MediaIdentity(kind="tv", year=2024, aliases=("另一部剧",), season_numbers=(1,)),
    ]
    shared = _candidate(
        "Test.Show.S01E03.2024.1080p",
        "测试剧集 第3集",
        media_type="tv",
        seasons=[1],
        episodes=[3],
        year=2024,
    )
    for media in identities:
        fresh = _candidate(
            "Test.Show.S01E03.2024.1080p",
            "测试剧集 第3集",
            media_type="tv",
            seasons=[1],
            episodes=[3],
            year=2024,
        )
        assert match_identity(shared, media) == match_identity(fresh, media)
    assert candidate_match_text(shared) == candidate_match_text(
        _candidate("Test.Show.S01E03.2024.1080p", "测试剧集 第3集")
    )
