#!/usr/bin/env python3
"""账号的个人观看数据：收藏、续播进度、看过的集、成员关注的订阅（固定随机种子）。

用途
----
iOS 性能实验室（``ios_lab.sh``）的媒体库首页有两行完全由「这个人看过什么」驱动：
「接下来继续」（``/playback/up-next``）与「我的收藏」（``/playback/favorites``）；
订阅首页的「刚刚入库」也要按「这个人看完没有」过滤。本脚本在账号建好之后
（成员要先经 API 创建）往 ``playback_state`` 等表里写入一份像真人的数据：

管理员（member_id=0，超管哨兵）
- 约 22 个最近 48 小时动过的续播点：10 部剧看到一半的集、6 部电影看到一半、
  6 部剧「刚看完上一集」（卡片指向下一集）。一半的剧集卡用**本地剧照资产**
  （w300，同刮削默认档位），一半走 TMDB 剧照（经图片代理回源假图床），2 集没有剧照；
  一半的电影卡用**本地背景资产**（original 原图 1~2 MB，同刮削默认档位——冷启动时
  派生 landscape-card 缩略图的 CPU 成本是真实存在的），一半走 TMDB，1 部没有背景图；
- 150 个收藏：70 部电影、60 部整剧、12 个整季、8 个单集，时间铺在近两年，
  约四成电影已看完（「未看优先」排序有东西可排），其中十来部是在追的订阅剧；
- 订阅剧的观看历史：在播的周更剧约 45% 已追平（最新入库的集看完了，因此不进
  「刚刚入库」），其余停在前几集；已完结的剧有的看完、有的看了一半。
  除「追平的在播剧」照实记最近一集的观看时间外，这些历史的最近播放时间都压到
  50 小时之前——追平的剧没有下一集可放、不会出卡，其余的不挤占「接下来继续」
  前 20 张，前排仍以上面的续播点为主。

成员（``--member`` 指定用户名，经 API 创建后才有 id）
- 关注 12 条订阅（6 部本周有新集的在播剧、3 部已完成、2 部季间歇、1 部在追的电影）——
  成员的「我的订阅」只看自己发起 + 关注的；
- 12 个收藏、5 个续播点、3 部关注剧的观看历史。

写入走 ``movieclaw_db.models`` 的 ORM 模型（与服务层同一套约束，唯一键冲突会当场
报错）。本地图片资产复用离线假图床（fake_image_origin.py）的底图池，硬链到
``<METADATA_DIR>/images/<条目 id>/``，与刮削管线的落盘布局一致。

用法::

    PYTHONPATH=src python scripts/perf/seed_watch_state.py --db <lab>/data/movieclaw.db \\
        --metadata-dir <lab>/data/metadata --pool-dir <lab>/origin-pool --member member
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlmodel import Session

from movieclaw_db.models import PlaybackState, SubscriptionFollower, utcnow

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_image_origin import ImagePool, spec_for  # noqa: E402  同目录的图片生成器

SEED = 20260928
TMDB_SUB_BASE = 500_000  # seed_subscriptions_dataset.py 的条目 tmdb_id 起点


class WatchSeeder:
    """按固定种子为一个账号生成观看状态；同一 (人, 条目, 季, 集) 只落一行。"""

    def __init__(
        self,
        session: Session,
        rng: random.Random,
        now: datetime,
        assets_root: Path,
        pool: ImagePool,
    ) -> None:
        self.session = session
        self.rng = rng
        self.now = now
        self.assets_root = assets_root
        self.pool = pool
        self.rows: dict[tuple[int, int, int, int], PlaybackState] = {}
        self.stats: dict[str, int] = defaultdict(int)
        self._load()

    # —— 候选：在位文件、时长、订阅剧 ——————————————————————————————————

    def _load(self) -> None:
        sql = self.session.connection()
        self.tv_units: dict[int, set[tuple[int, int]]] = defaultdict(set)
        self.movies: list[int] = []
        self.duration: dict[tuple[int, int, int], int] = {}
        for item_id, season, episode, kind, duration in sql.execute(
            text(
                "SELECT f.media_item_id, f.season_number, f.episode_number, l.kind, "
                "MAX(f.duration_seconds) FROM library_file f JOIN library l ON l.id = f.library_id "
                "WHERE f.state = 'in_place' AND f.media_item_id IS NOT NULL "
                "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3"
            )
        ):
            if kind == "tv" and season > 0:
                self.tv_units[item_id].add((season, episode))
            elif kind == "movie":
                self.movies.append(item_id)
            if duration:
                self.duration[(item_id, season, episode)] = int(duration)
        self.runtime = dict(
            sql.execute(text("SELECT media_item_id, runtime_minutes FROM media_metadata")).all()
        )
        self.sub_item: dict[int, int] = {}  # 订阅条目 → tmdb 序号，用来区分库里的老片
        for item_id, tmdb_id in sql.execute(
            text(f"SELECT id, tmdb_id FROM media_item WHERE tmdb_id >= {TMDB_SUB_BASE}")
        ):
            self.sub_item[item_id] = tmdb_id
        # 订阅剧：按 seed_subscriptions_dataset 的类目（在播 / 日更 / 已完成 …）取已入库单元
        self.imported: dict[int, list[tuple[int, int, datetime]]] = defaultdict(list)
        for item_id, season, episode, imported_at in sql.execute(
            text(
                "SELECT media_item_id, season_number, episode_number, imported_at FROM wanted_item "
                "WHERE status = 'imported' AND in_scope = 1 ORDER BY 1, 2, 3"
            )
        ):
            self.imported[item_id].append((season, episode, _dt(imported_at)))
        self.subs = [
            dict(row._mapping)
            for row in sql.execute(
                text(
                    "SELECT s.id, s.media_item_id, s.kind, s.status, s.follow_future, "
                    "m.status AS tmdb_status "
                    "FROM subscription s JOIN media_item m ON m.id = s.media_item_id ORDER BY s.id"
                )
            )
        ]

    def library_shows(self) -> list[int]:
        return [i for i in sorted(self.tv_units) if i not in self.sub_item]

    def library_movies(self) -> list[int]:
        return [i for i in self.movies if i not in self.sub_item]

    def duration_ms(self, item_id: int, season: int, episode: int) -> int:
        seconds = self.duration.get((item_id, season, episode))
        if not seconds:
            seconds = (self.runtime.get(item_id) or 45) * 60
        return seconds * 1000

    # —— 写行 ——————————————————————————————————————————————————————————

    def state(
        self, member: int, item_id: int, season: int, episode: int, at: datetime
    ) -> PlaybackState:
        """取（或建）一行；行的创建 / 更新时间跟着真实事件走（最早 / 最近一次）。"""
        key = (member, item_id, season, episode)
        row = self.rows.get(key)
        if row is None:
            row = PlaybackState(
                member_id=member,
                media_item_id=item_id,
                season_number=season,
                episode_number=episode,
                created_at=at,
                updated_at=at,
            )
            self.rows[key] = row
        row.created_at = min(row.created_at, at)
        row.updated_at = max(row.updated_at, at)
        return row

    def played(self, member: int, item_id: int, season: int, episode: int, at: datetime) -> None:
        row = self.state(member, item_id, season, episode, at)
        row.played, row.position_ms = True, 0
        row.play_count = max(row.play_count, 1)
        row.last_played_at = at

    def in_progress(
        self, member: int, item_id: int, season: int, episode: int, at: datetime
    ) -> None:
        row = self.state(member, item_id, season, episode, at)
        row.played = False
        row.position_ms = int(
            self.duration_ms(item_id, season, episode) * self.rng.uniform(0.15, 0.85)
        )
        row.play_count = max(row.play_count, 1)
        row.last_played_at = at

    def favorite(self, member: int, item_id: int, season: int, episode: int, at: datetime) -> None:
        row = self.state(member, item_id, season, episode, at)
        row.is_favorite = True
        row.favorited_at = at

    def recent(self, hours_lo: float, hours_hi: float) -> datetime:
        return self.now - timedelta(minutes=self.rng.uniform(hours_lo * 60, hours_hi * 60))

    # —— 本地图片资产（硬链自假图床的底图池）————————————————————————————

    def link_asset(self, rel: str, size: str, fake_name: str) -> None:
        spec = spec_for(size, fake_name)
        assert spec is not None
        source = self.pool.path_of(spec)
        if not source.exists():
            self.pool.load_or_render(spec)
        target = self.assets_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            try:
                os.link(source, target)  # 同一文件系统：不占额外磁盘
            except OSError:
                target.write_bytes(source.read_bytes())

    def still_asset(self, item_id: int, season: int, episode: int) -> None:
        rel = f"{item_id}/s{season:02d}e{episode:02d}.jpg"  # 同 media_scrape 的分集剧照布局
        self.link_asset(rel, "w300", f"still_{item_id}_{season}_{episode}.jpg")
        self.session.connection().execute(
            text(
                "UPDATE media_episode SET still_file = :rel WHERE media_item_id = :i "
                "AND season_number = :s AND episode_number = :e"
            ),
            {"rel": rel, "i": item_id, "s": season, "e": episode},
        )
        self.stats["本地剧照"] += 1

    def backdrop_asset(self, item_id: int) -> None:
        rel = f"{item_id}/backdrop.jpg"  # 同 media_scrape 的条目背景布局（original 档）
        self.link_asset(rel, "original", f"backdrop_{item_id}.jpg")
        self.session.connection().execute(
            text("UPDATE media_metadata SET backdrop_file = :rel WHERE media_item_id = :i"),
            {"rel": rel, "i": item_id},
        )
        self.stats["本地背景"] += 1

    # —— 管理员 ————————————————————————————————————————————————————————

    def seed_admin(self) -> list[int]:
        rng, admin = self.rng, 0
        shows = [i for i in self.library_shows() if len(self.tv_units[i]) >= 4]
        movies = self.library_movies()
        picked_shows = rng.sample(shows, k=16)
        picked_movies = rng.sample(movies, k=76)

        # 10 部剧看到一半：前面几集看过，当前这集有续播点
        for position, item_id in enumerate(picked_shows[:10]):
            units = sorted(self.tv_units[item_id])
            index = rng.randint(1, min(len(units) - 1, 12))
            season, episode = units[index]
            base = self.recent(0.2, 46)
            for back, (s, e) in enumerate(reversed(units[:index])):
                self.played(admin, item_id, s, e, base - timedelta(hours=6 + 20 * back))
            self.in_progress(admin, item_id, season, episode, base)
            if position < 5:
                self.still_asset(item_id, season, episode)
            elif position < 7:
                self._drop_still(item_id, season, episode)
            self.stats["续播·剧集"] += 1
        # 6 部剧刚看完上一集：卡片会指向下一集（advanced）
        for item_id in picked_shows[10:16]:
            units = sorted(self.tv_units[item_id])
            index = rng.randint(0, len(units) - 2)
            at = self.recent(1, 47)
            for back, (s, e) in enumerate(reversed(units[: index + 1])):
                self.played(admin, item_id, s, e, at - timedelta(hours=22 * back))
            nxt = units[index + 1]
            if rng.random() < 0.5:
                self.still_asset(item_id, *nxt)
            self.stats["续播·下一集"] += 1
        # 6 部电影看到一半：一半本地背景（原图），一半 TMDB，1 部没有背景图
        for position, item_id in enumerate(picked_movies[:6]):
            self.in_progress(admin, item_id, 0, 0, self.recent(0.5, 47))
            if position < 3:
                self.backdrop_asset(item_id)
            elif position == 5:
                self.session.connection().execute(
                    text("UPDATE media_item SET backdrop_path = NULL WHERE id = :i"), {"i": item_id}
                )
            self.stats["续播·电影"] += 1

        # 150 个收藏：70 电影 / 60 整剧 / 12 整季 / 8 单集
        fav_movies = picked_movies[6:76]
        for item_id in fav_movies:
            at = self.now - timedelta(days=rng.uniform(0.5, 720))
            self.favorite(admin, item_id, 0, 0, at)
            if rng.random() < 0.4:  # 约四成收藏的电影已经看完
                self.played(
                    admin,
                    item_id,
                    0,
                    0,
                    min(at + timedelta(days=rng.uniform(0.1, 20)), self.now - timedelta(days=3)),
                )
        used = set(picked_shows)
        # 收藏里有十来部是在追的订阅剧（有入库文件的才进得了「我的收藏」）
        sub_shows = [
            s["media_item_id"]
            for s in self.subs
            if s["kind"] == "tv" and self.imported.get(s["media_item_id"])
        ]
        fav_series = rng.sample([i for i in shows if i not in used], k=48) + rng.sample(
            sub_shows, k=12
        )
        for item_id in fav_series:
            self.favorite(admin, item_id, -1, -1, self.now - timedelta(days=rng.uniform(0.5, 720)))
        rest = [i for i in shows if i not in used and i not in fav_series]
        season_items = rng.sample(rest, k=12)
        for item_id in season_items:  # 整季收藏 (季, -1)
            season = sorted(self.tv_units[item_id])[0][0]
            self.favorite(
                admin, item_id, season, -1, self.now - timedelta(days=rng.uniform(1, 500))
            )
        for item_id in rng.sample([i for i in rest if i not in season_items], k=8):  # 单集收藏
            season, episode = rng.choice(sorted(self.tv_units[item_id]))
            self.favorite(
                admin, item_id, season, episode, self.now - timedelta(days=rng.uniform(1, 500))
            )
        self.stats["收藏"] = sum(
            1 for key, row in self.rows.items() if key[0] == admin and row.is_favorite
        )

        # 订阅剧的观看历史（最近播放都早于 50 小时前，不挤占「接下来继续」前排）
        horizon = self.now - timedelta(hours=50)
        for sub in self.subs:
            if sub["kind"] != "tv":
                continue
            units = self.imported.get(sub["media_item_id"], [])
            if not units:
                continue
            airing = sub["follow_future"] and sub["tmdb_status"] == "Returning Series"
            if airing and sub["status"] == "active":
                caught_up = rng.random() < 0.45
                upto = len(units) if caught_up else rng.randint(0, max(0, len(units) - 2))
            elif sub["status"] == "completed":
                roll = rng.random()
                upto = (
                    len(units) if roll < 0.35 else rng.randint(0, len(units)) if roll < 0.7 else 0
                )
            else:
                upto = rng.randint(0, len(units) // 2) if rng.random() < 0.3 else 0
            for season, episode, imported_at in units[:upto]:
                at = imported_at + timedelta(hours=rng.uniform(1, 30))
                # 追平的在播剧：最新一集就是最近看的，时间照实（可能在 50 小时内）
                self.played(
                    admin,
                    sub["media_item_id"],
                    season,
                    episode,
                    at if (upto == len(units) and airing) else min(at, horizon),
                )
            if upto:
                self.stats["订阅剧·有观看历史"] += 1
        return picked_shows

    def _drop_still(self, item_id: int, season: int, episode: int) -> None:
        """这一集没有剧照（TMDB 也没有）：卡片退回印集号的兜底画法。"""
        self.session.connection().execute(
            text(
                "UPDATE media_episode SET still_path = NULL, still_file = NULL "
                "WHERE media_item_id = :i "
                "AND season_number = :s AND episode_number = :e"
            ),
            {"i": item_id, "s": season, "e": episode},
        )
        self.stats["无剧照"] += 1

    # —— 成员 ——————————————————————————————————————————————————————————

    def seed_member(self, member: int, admin_shows: list[int]) -> None:
        rng = self.rng
        upcoming = {
            row[0]
            for row in self.session.connection().execute(
                text(
                    "SELECT DISTINCT subscription_id FROM wanted_item WHERE status = 'wanted' "
                    "AND air_date BETWEEN date('now', '+8 hours') "
                    "AND date('now', '+8 hours', '+7 days')"
                )
            )
        }
        buckets: dict[str, list[dict]] = defaultdict(list)
        for sub in self.subs:
            if sub["kind"] == "movie":
                buckets[f"movie-{sub['status']}"].append(sub)
            elif (
                sub["status"] == "active"
                and sub["follow_future"]
                and self.imported.get(sub["media_item_id"])
            ):
                buckets["airing" if sub["id"] in upcoming else "between"].append(sub)
            else:
                buckets[sub["status"]].append(sub)
        followed = (
            rng.sample(buckets["airing"], k=6)
            + rng.sample(buckets["completed"], k=3)
            + rng.sample(buckets["between"], k=2)
            + rng.sample(buckets["movie-active"], k=1)
        )
        for sub in followed:
            self.session.add(
                SubscriptionFollower(
                    subscription_id=sub["id"],
                    member_id=member,
                    created_at=self.now - timedelta(days=rng.uniform(1, 60)),
                    updated_at=self.now - timedelta(days=rng.uniform(0, 1)),
                )
            )
        self.stats["成员关注的订阅"] = len(followed)
        for sub in followed[:3]:  # 3 部关注的在播剧：看过前面几集
            units = self.imported.get(sub["media_item_id"], [])
            for season, episode, imported_at in units[: max(0, len(units) - 2)]:
                self.played(
                    member,
                    sub["media_item_id"],
                    season,
                    episode,
                    min(
                        imported_at + timedelta(hours=rng.uniform(2, 40)),
                        self.now - timedelta(hours=60),
                    ),
                )
        shows = [
            i for i in self.library_shows() if i not in admin_shows and len(self.tv_units[i]) >= 3
        ]
        movies = self.library_movies()
        for item_id in rng.sample(shows, k=3):  # 续播：3 集看到一半、2 部电影看到一半
            season, episode = rng.choice(sorted(self.tv_units[item_id])[1:])
            self.in_progress(member, item_id, season, episode, self.recent(0.5, 20))
        member_movies = rng.sample(movies, k=10)
        for item_id in member_movies[:2]:
            self.in_progress(member, item_id, 0, 0, self.recent(1, 30))
        for item_id in member_movies[2:10]:  # 收藏：8 部电影 + 4 部整剧
            self.favorite(member, item_id, 0, 0, self.now - timedelta(days=rng.uniform(1, 300)))
        for item_id in rng.sample(shows, k=4):
            self.favorite(member, item_id, -1, -1, self.now - timedelta(days=rng.uniform(1, 300)))
        self.stats["成员收藏"] = sum(
            1 for key, row in self.rows.items() if key[0] == member and row.is_favorite
        )


def _dt(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--metadata-dir", required=True, help="后端的 METADATA_DIR（资产根 = 其下 images/）"
    )
    parser.add_argument("--pool-dir", required=True, help="离线假图床的底图池目录")
    parser.add_argument("--member", default="member", help="成员用户名（须已经 API 创建）")
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")  # 后端可能正开着库：等锁而不是报错
        cursor.close()

    rng = random.Random(SEED)
    with Session(engine) as session:
        seeder = WatchSeeder(
            session,
            rng,
            utcnow(),
            Path(args.metadata_dir) / "images",
            ImagePool(Path(args.pool_dir)),
        )
        admin_shows = seeder.seed_admin()
        member_id = (
            session.connection()
            .execute(text("SELECT id FROM member WHERE username = :u"), {"u": args.member})
            .scalar()
        )
        if member_id is None:
            print(f"警告：没有找到成员「{args.member}」，跳过成员的观看数据（先经 API 创建成员）")
        else:
            seeder.seed_member(int(member_id), admin_shows)
        session.add_all(seeder.rows.values())
        session.commit()
    engine.dispose()
    print(
        "观看数据："
        + "，".join(f"{k}={v}" for k, v in seeder.stats.items())
        + f"，playback_state 共 {len(seeder.rows)} 行"
    )


if __name__ == "__main__":
    main()
