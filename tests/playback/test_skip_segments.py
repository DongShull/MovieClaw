"""跳过片头 / 片尾的整季比对算法（movieclaw_playback.skip_segments）。

用合成指纹验证设计文档 §2 的每条规则：真片头片尾逐帧几乎一致，配乐复用只是「像」
（每帧翻 7～9 位），同一集的另一个版本不当伙伴，两个版本的片头交替出现也认得出。
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np

from movieclaw_playback import skip_segments as K

SD = K.HASH_SECONDS


def frames(seconds: float) -> int:
    return int(round(seconds / SD))


def noise(rng: np.random.Generator, seconds: float) -> np.ndarray:
    return rng.integers(0, 2**32, size=frames(seconds), dtype=np.uint64).astype(np.uint32)


def flip_bits(rng: np.random.Generator, hashes: np.ndarray, bits: int) -> np.ndarray:
    """每帧随机翻 ``bits`` 位：0～2 位模拟同一份音频重新编码，7～9 位模拟对白盖住的配乐。"""
    out = hashes.copy()
    for i in range(len(out)):
        for b in rng.choice(32, size=bits, replace=False):
            out[i] ^= np.uint32(1 << int(b))
    return out


def episode(
    rng: np.random.Generator,
    file_id: int,
    number: int,
    *,
    duration: float = 2700.0,
    intro: np.ndarray | None = None,
    intro_at: float = 0.0,
    outro: np.ndarray | None = None,
    outro_to_end: float = 0.0,
    extra: list[tuple[np.ndarray, float]] | None = None,
) -> K.Episode:
    """一集：片头窗 600 秒、片尾窗 420 秒的随机指纹，再把片头 / 片尾 / 额外段贴进去。"""
    bounds = K.window_bounds(duration)
    head = noise(rng, bounds["intro"][1])
    tail = noise(rng, bounds["outro"][1])
    if intro is not None:
        i = frames(intro_at)
        head[i : i + len(intro)] = flip_bits(rng, intro, 1)
    if outro is not None:
        j = len(tail) - frames(outro_to_end) - len(outro)
        tail[j : j + len(outro)] = flip_bits(rng, outro, 1)
    for seg, at in extra or []:
        i = frames(at)
        head[i : i + len(seg)] = seg
    return K.Episode(
        file_id,
        number,
        duration,
        {"intro": K.Window(head, bounds["intro"][0]), "outro": K.Window(tail, bounds["outro"][0])},
    )


def kinds(segments: list[K.Segment]) -> list[str]:
    return [s.kind for s in segments]


def test_finds_intro_and_outro_across_season_with_cold_opens() -> None:
    rng = np.random.default_rng(1)
    intro = noise(rng, 60)
    outro = noise(rng, 120)
    # 片头位置每集不同（冷开场长短不一），片尾一直放到结尾
    eps = [episode(rng, n, n, intro=intro, intro_at=30 + 20 * n, outro=outro) for n in range(1, 7)]
    result = K.detect_season(eps)
    for e in eps:
        segs = result[e.file_id]
        assert kinds(segs) == ["intro", "outro"]
        head, tail = segs
        expected = 30 + 20 * e.episode
        assert abs(head.start - expected) < 1.5 and abs(head.end - (expected + 60)) < 1.5
        assert tail.to_end
        assert abs(tail.end - e.duration) < 1.5


def test_reused_score_under_dialogue_is_rejected() -> None:
    """片中复用的配乐：几集里都「像」（每帧翻 8 位），但不是同一份音频，不认。"""
    rng = np.random.default_rng(2)
    intro = noise(rng, 45)
    score = noise(rng, 40)
    eps = []
    for n in range(1, 9):
        extra = [(flip_bits(rng, score, 5), 300.0)] if n <= 3 else None
        eps.append(episode(rng, n, n, intro=intro, extra=extra))
    result = K.detect_season(eps)
    for e in eps:
        assert kinds(result[e.file_id]) == ["intro"], result[e.file_id]


def test_alternating_intro_versions_need_whole_season() -> None:
    """两个片头版本隔集交替：每个版本只在一半的集里，靠「对得很像的伙伴 ≥2 个」认出。"""
    rng = np.random.default_rng(3)
    a, b = noise(rng, 80), noise(rng, 80)
    eps = [episode(rng, n, n, intro=a if n % 2 else b, intro_at=10.0) for n in range(1, 11)]
    result = K.detect_season(eps)
    assert all(kinds(result[e.file_id]) == ["intro"] for e in eps)


def test_sponsor_ad_and_intro_back_to_back_merge_into_one_segment() -> None:
    """冠名广告紧接着片头（中间不到 3 秒）：合成一段，一个按钮跳完。"""
    rng = np.random.default_rng(4)
    ad, intro = noise(rng, 20), noise(rng, 70)
    eps = [episode(rng, n, n, extra=[(ad, 0.0), (intro, 21.0)]) for n in range(1, 6)]
    result = K.detect_season(eps)
    for e in eps:
        (seg,) = result[e.file_id]
        assert seg.kind == "intro"
        assert seg.start < 1.5 and abs(seg.end - 91) < 1.5


def test_separate_ad_is_other_and_longest_is_intro() -> None:
    rng = np.random.default_rng(5)
    ad, intro = noise(rng, 20), noise(rng, 80)
    eps = [episode(rng, n, n, extra=[(ad, 0.0), (intro, 200.0 + 10 * n)]) for n in range(1, 6)]
    result = K.detect_season(eps)
    for e in eps:
        assert kinds(result[e.file_id]) == ["other", "intro"]


def test_other_versions_of_same_episode_are_not_partners() -> None:
    """同一集的两个版本处处一样：不能当伙伴，否则整段正片都会被认成「重复」。"""
    rng = np.random.default_rng(6)
    head = noise(rng, 600)
    tail = noise(rng, 420)

    def version(file_id: int) -> K.Episode:
        return K.Episode(
            file_id,
            1,
            2700.0,
            {"intro": K.Window(head.copy(), 0.0), "outro": K.Window(tail.copy(), 2280.0)},
        )

    eps = [version(1), version(2), episode(rng, 3, 2), episode(rng, 4, 3)]
    result = K.detect_season(eps)
    assert all(result[e.file_id] == [] for e in eps)


def test_fewer_than_three_episodes_gives_nothing() -> None:
    rng = np.random.default_rng(7)
    intro = noise(rng, 60)
    eps = [episode(rng, n, n, intro=intro) for n in (1, 2)]
    assert K.detect_season(eps) == {1: [], 2: []}


def test_outro_window_keeps_only_last_segment() -> None:
    """片尾窗里的固定配乐场景（深夜食堂每集都有的吃饭戏）不当片尾，只认最后一段。"""
    rng = np.random.default_rng(8)
    scene, credits = noise(rng, 50), noise(rng, 90)
    eps = []
    for n in range(1, 7):
        e = episode(rng, n, n, outro=credits)
        tail = e.windows["outro"].hashes
        tail[frames(60) : frames(60) + len(scene)] = flip_bits(rng, scene, 1)
        eps.append(e)
    result = K.detect_season(eps)
    for e in eps:
        (seg,) = result[e.file_id]
        assert seg.kind == "outro" and seg.to_end


def test_fingerprint_file_roundtrip() -> None:
    rng = np.random.default_rng(9)
    head, tail = noise(rng, 30), noise(rng, 20)
    raw = K.encode_fingerprint(
        {"size": 1, "windows": {"intro": {"start": 0.0}, "outro": {"start": 100.0}}},
        {"intro": head.astype("<u4").tobytes(), "outro": tail.astype("<u4").tobytes()},
    )
    meta, windows = K.decode_fingerprint(raw)
    assert meta["size"] == 1
    assert np.array_equal(windows["intro"].hashes, head)
    assert windows["outro"].start == 100.0 and np.array_equal(windows["outro"].hashes, tail)
    assert K.decode_fingerprint(b"garbage") is None


def test_worker_entry_point(tmp_path) -> None:
    """子进程入口：读指纹文件、算、一行 JSON 写回；读不了的文件报在 unreadable 里。"""
    rng = np.random.default_rng(10)
    intro = noise(rng, 60)
    items = []
    for n in range(1, 5):
        e = episode(rng, n, n, intro=intro, intro_at=5.0)
        path = tmp_path / f"{n}.fp"
        path.write_bytes(
            K.encode_fingerprint(
                {"windows": {k: {"start": w.start} for k, w in e.windows.items()}},
                {k: w.hashes.astype("<u4").tobytes() for k, w in e.windows.items()},
            )
        )
        items.append({"file_id": n, "episode": n, "duration": e.duration, "path": str(path)})
    items.append({"file_id": 99, "episode": 9, "duration": 2700.0, "path": str(tmp_path / "x")})
    proc = subprocess.run(
        [sys.executable, "-m", "movieclaw_playback.skip_segments"],
        input=json.dumps({"episodes": items}),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["algo_version"] == K.ALGO_VERSION
    assert out["unreadable"] == [99]
    for n in range(1, 5):
        (seg,) = out["results"][str(n)]
        assert seg["type"] == "intro" and abs(seg["start_ms"] - 5000) < 1500
