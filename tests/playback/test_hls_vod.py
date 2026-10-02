"""VOD 分片规划与播放列表生成单测（docs/design/web-player.md §12）。

分片边界规则必须与 ffmpeg hls muxer 一致（超过目标时长后的第一个关键帧切），
这里锁死规则本身；与真实 ffmpeg 的吻合由集成冒烟验证。
"""

from __future__ import annotations

from movieclaw_playback.hls_vod import (
    build_master_playlist,
    build_media_playlist,
    build_subtitle_playlist,
    compute_keyframe_plan,
    compute_uniform_plan,
)


def test_copy_plan_cuts_at_every_keyframe():
    """直通档每个关键帧一段（ffmpeg 侧 -hls_time 极小）：与从哪起转无关，连续转与重启转一致。"""
    plan = compute_keyframe_plan((0.0, 1.2, 3.9, 5.1, 8.0, 9.9, 12.5), 14.5)
    assert plan.boundaries == (0.0, 1.2, 3.9, 5.1, 8.0, 9.9, 12.5)
    assert plan.duration_of(plan.count - 1) == 2.0


def test_copy_plan_keeps_tail_and_drops_out_of_range():
    """片尾短段不合并（ffmpeg 不会合并）；时长之外、重复、乱序的关键帧不影响结果。"""
    plan = compute_keyframe_plan((8.0, 0.0, 4.0, 4.0, 9.5, 12.0), 9.0)
    assert plan.boundaries == (0.0, 4.0, 8.0)
    assert plan.duration_of(2) == 1.0


def test_copy_plan_merges_keyframes_hugging_the_head():
    """索引开头补出来的 0 点之后紧跟真正的首帧（0.083）：ffmpeg 不在首包上切，首段从 0 起。"""
    plan = compute_keyframe_plan((0.0, 0.083, 4.2, 6.0), 10.0)
    assert plan.boundaries == (0.0, 4.2, 6.0)


def test_seek_pad_never_crosses_the_next_keyframe():
    """重启时 -ss 往后多给的量：0.5 秒，但相邻关键帧很近时只给到一半，不能落进下一段。"""
    plan = compute_keyframe_plan((0.0, 4.0, 4.05, 9.0), 12.0)
    assert plan.seek_pad(0) == 0.5
    assert abs(plan.seek_pad(1) - 0.025) < 1e-9
    assert plan.seek_pad(2) == 0.5
    assert plan.seek_pad(3) == 0.5  # 末段止于片长，同样不越界


def test_segment_for_position():
    plan = compute_uniform_plan(30.0, target_s=4.0)
    assert plan.segment_for(0) == 0
    assert plan.segment_for(3.999) == 0
    assert plan.segment_for(4.0) == 1
    assert plan.segment_for(29.9) == plan.count - 1
    assert plan.segment_for(999) == plan.count - 1  # 越界钳到末段


def test_media_playlist_is_vod_with_endlist():
    plan = compute_uniform_plan(30.0, target_s=4.0)
    text = build_media_playlist(
        plan, init_name="init.mp4", segment_name="seg%05d.m4s", query="?token=t1"
    )
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert '#EXT-X-MAP:URI="init.mp4?token=t1"' in text
    assert "seg00000.m4s?token=t1" in text
    assert "seg00007.m4s?token=t1" in text
    # EXTINF 总和 = 片长（播放器据此显示总时长与 seek）
    total = sum(
        float(line[len("#EXTINF:"):-1])
        for line in text.splitlines()
        if line.startswith("#EXTINF:")
    )
    assert abs(total - 30.0) < 0.001


def test_media_playlist_starts_at_resume_point():
    """续播：列表写 EXT-X-START，播放器第一个请求就取续播点所在的分片——
    不写的话 AVPlayer 先要第 0 段，把正从续播点起转的转码拉回片头重启一轮。"""
    plan = compute_uniform_plan(30.0, target_s=4.0)
    text = build_media_playlist(
        plan, init_name="init.mp4", segment_name="seg%05d.m4s", start_s=13.25
    )
    assert "#EXT-X-START:TIME-OFFSET=13.250,PRECISE=YES" in text
    # 标签要在第一条分片之前（播放列表头部）
    assert text.index("#EXT-X-START") < text.index("#EXTINF")
    for start in (None, 0.0):
        text = build_media_playlist(
            plan, init_name="init.mp4", segment_name="seg%05d.m4s", start_s=start
        )
        assert "EXT-X-START" not in text


def test_start_offset_stays_inside_the_segment_the_server_starts_from():
    """起播点正压在分片边界（整秒 + 恰好是关键帧）：播放器按 6 位小数的 EXTINF
    累加找段，误差会把它算进前一段；写进列表的位置要夹进服务端起转那一段的内部。"""
    plan = compute_keyframe_plan(
        (0.0, 2.48, 597.52, 600.0, 600.96, 604.0, 896.6, 900.0, 903.0), 1000.0
    )
    for start in (600.0, 900.0, 897.0, 899.9999):
        head = plan.segment_for(start)
        offset = float(f"{plan.start_offset(start):.3f}")  # 列表里写的是 3 位小数
        # 用播放器的算法（逐段累加 EXTINF 文本）找它落在哪一段
        acc, found = 0.0, None
        for i in range(plan.count):
            duration = float(f"{plan.duration_of(i):.6f}")
            if acc <= offset < acc + duration:
                found = i
                break
            acc += duration
        assert found == head, (start, offset, found, head)
        assert abs(offset - start) < 0.01  # 只是挪进段内，不改变起播位置


def test_master_playlist_carries_subtitle_group():
    text = build_master_playlist(
        media_uri="media.m3u8",
        subtitles=[("中文", "sub0.m3u8"), ("英文", "sub1.m3u8")],
        query="?token=t1",
    )
    assert 'TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES' in text
    assert 'NAME="英文",DEFAULT=NO' in text
    assert 'SUBTITLES="subs"' in text
    assert "media.m3u8?token=t1" in text


def test_master_playlist_without_subtitles():
    text = build_master_playlist(media_uri="media.m3u8")
    assert "SUBTITLES" not in text
    assert "#EXT-X-STREAM-INF" in text


def test_master_playlist_carries_exact_codecs():
    text = build_master_playlist(
        media_uri="media.m3u8",
        codecs="avc1.640029,mp4a.40.2",
    )
    assert '#EXT-X-STREAM-INF:BANDWIDTH=80000000,CODECS="avc1.640029,mp4a.40.2"' in text


def test_subtitle_playlist_single_segment():
    text = build_subtitle_playlist(vtt_uri="sub.vtt", duration_s=1800.5, query="?token=t")
    assert "#EXTINF:1800.500000," in text
    assert "sub.vtt?token=t" in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
