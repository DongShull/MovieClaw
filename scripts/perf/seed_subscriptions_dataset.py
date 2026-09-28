#!/usr/bin/env python3
"""订阅首页压测数据生成器（真实 ORM 模型、按「今天」排期、固定随机种子）。

用途
----
给 iOS 性能实验室（``ios_lab.sh``）灌一份「重度家庭用户」的订阅：约 300 条订阅
（约 210 部剧、90 部电影），让 App「我的订阅」页的每一块都跑在真实体量上：

- ``GET /subscriptions``：全部约 300 条，带真实形态的进度与分季收录（剧集有多季、
  已播 / 未播 / 已入库 / 缺集混杂）；
- ``GET /subscriptions/today-arrivals?window=week``：未来 7 天约 50 条预告
  （周更美剧 / 番剧 / 韩剧 / 综艺、日更国产剧、下周首播的新季，外加下载中 /
  整理中的在途工单），约六成带「资源发布时间预测」；
- ``GET /subscriptions/recent-arrivals``：最近 7 天入库、文件在位、还没看完的
  卡片（候选远多于 12 张，接口按上限返回 12 张；「看完了」由 seed_watch_state.py 决定）；
- ``GET /downloaders/tasks``：可选的假 qBittorrent 用 ``--qbt-json`` 产出的任务清单
  （与在途工单同一批 infohash）。

为什么不走 API / 真管线：订阅创建要打 TMDB、投递要真站点真下载器，几百条根本
跑不起来；而压测关心的是**读路径**。这里直接用 ``movieclaw_db.models`` 的 ORM
模型落库（与服务层同一套字段默认值与约束），行的形态按服务层的真实写法来：

- 订阅 = 期望集合 E 的定义：``selected_seasons`` + ``follow_future``；工单 = E 的
  物化，每个单元一行（电影是 (0,0) 哨兵），``status`` 走 wanted → grabbed →
  downloaded → imported，``in_scope`` 恒为真；
- 调度字段照 ``services/subscription/core.py``：补旧 = 已到期、追新 = 播出日 +
  48h、未定档 = NULL；电影按上映日 + 7 天（``movie_schedule``），工单不写播出日；
- 在途工单必带 ``info_hash``（模拟投递没有 infohash，首页不会把它当「下载中」）；
- 已入库单元在目标库里有在位的 ``library_file``（``source=imported``、
  ``identity_source=subscription_exact``），入账时间 = 入库时间，因此订阅入库的
  剧会出现在媒体库首页「最近添加」行的最前面——与真实部署一致；
- 每次投递一条 ``subscription_download_attempt``，外加 created / grabbed /
  downloaded / imported / completed / paused / searched 活动流水；
  ``subscription.last_activity_at`` 由数据库触发器随活动插入推进（海报墙排序键）；
- 条目的 ``poster_path`` / ``backdrop_path`` 全有，约 70% 有 ``logo_path``（其余是
  空串 = 「取过但没有」）。路径带类型前缀（``/poster_<hex>.jpg`` 等），离线假图床
  （fake_image_origin.py）据此生成对应尺寸的图。

时间口径
--------
所有日期都相对**运行当天**（站点日历 Asia/Shanghai，与后端 ``publish_calendar_date``
同口径）生成：同一天跑两次结果完全一致（固定种子），换一天跑则整体平移，
「未来 7 天」「最近 7 天」永远成立。数据集是 reset 时现生成的，所以隔几天再测
应重新 reset（或至少重跑本脚本所在的整套 reset）。

前置：已跑过 alembic 迁移，且已用 ``seed_library_dataset.py --profile home`` 建好
12 个库（按库名找目标库）。之后再跑 ``seed_poster_assets.py`` 给新条目补本地海报。

用法::

    PYTHONPATH=src python scripts/perf/seed_subscriptions_dataset.py \\
        --db <lab>/data/movieclaw.db --qbt-json <lab>/origin/qbt-torrents.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time as clock
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, func
from sqlmodel import Session, select

from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    MediaSeason,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)

SEED = 20260927
SITE_TZ = ZoneInfo("Asia/Shanghai")  # 与后端 matching.publish_calendar_date 同一个站点日历
TMDB_ID_BASE = 500_000  # 远离 seed_library_dataset.py 的 10 万段，身份锚不会撞车
FUTURE_GRACE = timedelta(hours=48)  # 同 core.FUTURE_GRACE
MOVIE_RELEASE_GRACE = timedelta(days=7)  # 同 core.MOVIE_RELEASE_GRACE

# 默认规则组与 services/rule_sets.ensure_default 的懒种子完全一致
DEFAULT_RULE_SET = ("默认规则组", {"resolutions": ["2160p", "1080p"], "min_seeders": 1})

SITES = [
    ("mteam", "M-Team"),
    ("hdsky", "HDSky"),
    ("chdbits", "CHDBits"),
    ("ourbits", "OurBits"),
    ("audiences", "Audiences"),
]
GROUPS = ["CHDWEB", "ADWeb", "HHWEB", "NTb", "FLUX", "MWeb", "QHstudIo", "FRDS"]

# —— 片名素材（按类型分池；池子用完时加「第 N 季」后缀复用）——————————————
TITLES = {
    "cn": [
        "繁花",
        "漫长的季节",
        "狂飙",
        "庆余年",
        "梦华录",
        "苍兰诀",
        "长相思",
        "莲花楼",
        "三体",
        "人世间",
        "山海情",
        "觉醒年代",
        "沉默的真相",
        "隐秘的角落",
        "开端",
        "风起陇西",
        "警察荣誉",
        "去有风的地方",
        "星汉灿烂",
        "卿卿日常",
        "与凤行",
        "南来北往",
        "追风者",
        "我的阿勒泰",
        "玫瑰的故事",
        "墨雨云间",
        "边水往事",
        "唐朝诡事录",
        "长安十二时辰",
        "琅琊榜",
        "大明王朝1566",
        "父母爱情",
        "欢乐颂",
        "都挺好",
        "小欢喜",
        "司藤",
        "雪中悍刀行",
        "赘婿",
        "白夜追凶",
        "无证之罪",
        "猎罪图鉴",
        "显微镜下的大明",
        "天道",
        "潜伏",
        "山河令",
        "知否知否应是绿肥红瘦",
        "甄嬛传",
        "大江大河",
        "县委大院",
        "问心",
        "春色寄情人",
        "度华年",
        "异人之下",
        "新生",
        "错位",
        "白色城堡",
        "我是刑警",
        "北上",
        "六姊妹",
        "国色芳华",
    ],
    "us": [
        "The Last of Us",
        "House of the Dragon",
        "Severance",
        "The Bear",
        "Andor",
        "Shōgun",
        "Fallout",
        "The Boys",
        "Slow Horses",
        "Silo",
        "Foundation",
        "Reacher",
        "Loki",
        "Yellowstone",
        "The Penguin",
        "Ted Lasso",
        "Succession",
        "Wednesday",
        "Stranger Things",
        "The Mandalorian",
        "Only Murders in the Building",
        "True Detective",
        "Dune: Prophecy",
        "The Diplomat",
        "Bad Monkey",
        "Sugar",
        "Presumed Innocent",
        "Lioness",
        "Tulsa King",
        "The Morning Show",
        "Invincible",
        "Arcane",
        "Blue Eye Samurai",
        "The White Lotus",
        "Hacks",
        "Abbott Elementary",
        "Poker Face",
        "Dark Matter",
        "3 Body Problem",
        "Ripley",
        "Mr. & Mrs. Smith",
        "The Gentlemen",
        "Monarch: Legacy of Monsters",
        "Percy Jackson and the Olympians",
        "Masters of the Air",
        "The Pitt",
        "Paradise",
        "Andor: Rogue",
        "Squid Game",
        "The Studio",
        "Adolescence",
        "Black Mirror",
        "Daredevil: Born Again",
    ],
    "anime": [
        "葬送的芙莉莲",
        "药屋少女的呢喃",
        "咒术回战",
        "间谍过家家",
        "迷宫饭",
        "我推的孩子",
        "鬼灭之刃",
        "进击的巨人",
        "电锯人",
        "蓝色监狱",
        "排球少年!!",
        "海贼王",
        "名侦探柯南",
        "为美好的世界献上祝福！",
        "无职转生",
        "关于我转生变成史莱姆这档事",
        "怪兽8号",
        "胆大党",
        "败犬女主太多了！",
        "物语系列",
        "孤独摇滚！",
        "赛马娘",
        "紫罗兰永恒花园",
        "辉夜大小姐想让我告白",
        "堀与宫村",
        "Re:从零开始的异世界生活",
        "凡人修仙传",
        "斗罗大陆",
        "完美世界",
        "吞噬星空",
        "仙逆",
        "一人之下",
        "灵笼",
        "时光代理人",
        "天官赐福",
        "雾山五行",
        "中国奇谭",
        "章鱼噼的原罪",
        "我独自升级",
        "地。-关于地球的运动-",
    ],
    "kr": [
        "黑暗荣耀",
        "非常律师禹英禑",
        "眼泪女王",
        "财阀家的小儿子",
        "我的解放日志",
        "机智的医生生活",
        "王国",
        "信号",
        "请回答1988",
        "孤独的美食家",
        "半泽直树",
        "Silent",
        "非自然死亡",
        "重启人生",
        "LEGAL HIGH",
        "东京爱情故事",
        "月薪娇妻",
        "First Love 初恋",
        "寄生兽：灰色部队",
        "低谷医生",
        "背着善宰跑吧",
        "照明商店",
        "Moving",
        "苦尽柑来遇见你",
        "未知的首尔",
        "天国的阶梯",
    ],
    "variety": [
        "奔跑吧",
        "向往的生活",
        "乘风破浪",
        "歌手",
        "披荆斩棘",
        "极限挑战",
        "脱口秀和Ta的朋友们",
        "地球脉动",
        "我们的星球",
        "河西走廊",
        "舌尖上的中国",
        "人生一串",
        "风味人间",
        "种地吧",
        "现在就出发",
        "喜人奇妙夜",
        "一年一度喜剧大赛",
        "中国诗词大会",
        "朗读者",
        "奇葩说",
    ],
    "movie": [
        "沙丘2",
        "奥本海默",
        "封神第一部：朝歌风云",
        "流浪地球2",
        "热辣滚烫",
        "第二十条",
        "飞驰人生2",
        "年会不能停！",
        "周处除三害",
        "默杀",
        "抓娃娃",
        "异形：夺命舰",
        "死侍与金刚狼",
        "头脑特工队2",
        "猩球崛起：新世界",
        "疯狂的麦克斯：狂暴女神",
        "哥斯拉大战金刚2：帝国崛起",
        "功夫熊猫4",
        "美国内战",
        "挑战者",
        "坠落的审判",
        "可怜的东西",
        "枯叶",
        "完美的日子",
        "花月杀手",
        "拿破仑",
        "奇迹笨小孩",
        "满江红",
        "消失的她",
        "孤注一掷",
        "长安三万里",
        "八角笼中",
        "志愿军：雄兵出击",
        "三大队",
        "涉过愤怒的海",
        "河边的错误",
        "维和防暴队",
        "云边有个小卖部",
        "逆行人生",
        "从21世纪安全撤离",
        "野孩子",
        "好东西",
        "误杀3",
        "蛟龙行动",
        "哪吒之魔童闹海",
        "唐探1900",
        "射雕英雄传：侠之大者",
        "小丑2：双重妄想",
        "毒液：最后一舞",
        "角斗士2",
        "海洋奇缘2",
        "魔法坏女巫",
        "狮子王：木法沙传奇",
        "超人",
        "神奇4侠：初露锋芒",
        "侏罗纪世界：重生",
        "碟中谍8：最终清算",
        "F1：狂飙飞车",
        "星际宝贝史迪奇",
        "罪人",
        "美国队长4",
        "驯龙高手",
        "疯狂动物城2",
        "阿凡达：火与烬",
        "创：战神",
        "名侦探柯南：独眼的残像",
        "鬼灭之刃：无限城篇",
        "电锯人：蕾塞篇",
        "你想活出怎样的人生",
        "铃芽之旅",
        "灌篮高手",
        "蜘蛛侠：纵横宇宙",
        "银河护卫队3",
        "速度与激情10",
        "巨齿鲨2：深渊",
        "芭比",
        "夺宝奇兵：命运转盘",
        "疾速追杀4",
        "龙与地下城：侠盗荣耀",
        "瞬息全宇宙",
        "悲情三角",
        "塔尔",
        "西线无战事",
        "阿凡达：水之道",
        "捉妖记3",
        "南京照相馆",
        "长安的荔枝",
        "浪浪山小妖怪",
        "东极岛",
        "731",
        "戏台",
        "酱园弄",
        "独一无二",
    ],
}

# 剧集类型 → (目标库名, 单集时长, 每季集数区间, 排播节奏, 当地播出钟点)
SHOW_TYPES = {
    "cn": ("剧集", 45, (24, 32), "weekdays", 20.0),
    "us": ("美剧", 55, (8, 10), "weekly", 9.0),  # 美东周日晚 = 北京周一上午
    "anime": ("动画", 24, (12, 13), "weekly", 22.5),
    "kr": ("日韩剧", 65, (12, 16), "twice_weekly", 20.0),
    "variety": ("综艺", 90, (10, 12), "weekly", 20.0),
}

# 各类订阅的数量与类型构成（合计 210 部剧）。类目含义见 _plan_tv 各分支注释
TV_PLAN = {
    "airing": {"us": 11, "anime": 10, "kr": 2, "variety": 7},
    "daily": {"cn": 1},
    "premiere": {"us": 1, "anime": 1},
    "between": {"cn": 25, "us": 18, "anime": 12, "kr": 6, "variety": 4},
    "backfill": {"cn": 15, "us": 10, "anime": 8, "kr": 7},
    "completed": {"cn": 18, "us": 14, "anime": 10, "kr": 8},
    "paused": {"cn": 8, "us": 6, "anime": 5, "kr": 3},
}
# 电影（合计 90 部）
MOVIE_PLAN = {
    "completed": 25,
    "paused": 8,
    "searching": 25,
    "unreleased": 20,
    "just_released": 9,
    "pipeline": 3,
}
# 在途（下载中 / 整理中）的剧集单元：从周更剧里挑 3 部、补缺失里挑 2 部
PIPELINE_TV = {"airing": 3, "backfill": 2}

GENRES = {
    "cn": [(18, "剧情"), (80, "犯罪"), (9648, "悬疑"), (10749, "爱情")],
    "us": [(18, "剧情"), (10765, "Sci-Fi & Fantasy"), (80, "犯罪"), (10759, "动作冒险")],
    "anime": [(16, "动画"), (10765, "Sci-Fi & Fantasy"), (10759, "动作冒险"), (35, "喜剧")],
    "kr": [(18, "剧情"), (10749, "爱情"), (35, "喜剧"), (9648, "悬疑")],
    "variety": [(10764, "真人秀"), (99, "纪录"), (10767, "脱口秀")],
    "movie": [(28, "动作"), (878, "科幻"), (18, "剧情"), (35, "喜剧"), (53, "惊悚"), (16, "动画")],
}
COUNTRIES = {"cn": ["CN"], "us": ["US"], "anime": ["JP"], "kr": ["KR"], "variety": ["CN"]}


# ---------------------------------------------------------------------------
# 计划（纯内存）：先把 300 条订阅「应该长什么样」算清楚，再一次性落库
# ---------------------------------------------------------------------------


@dataclass
class TorrentPlan:
    """一次投递（一个 infohash）：单集种或整季包，覆盖若干单元。"""

    info_hash: str
    title: str
    site: tuple[str, str]
    torrent_id: str
    size: int
    grabbed_at: datetime
    downloaded_at: datetime | None
    imported_at: datetime | None
    units: list[UnitPlan] = field(default_factory=list)
    progress: float = 1.0  # 在途任务的下载进度（给假 qBittorrent）


@dataclass
class UnitPlan:
    """一个期望单元（工单）。电影是 (0, 0)。"""

    season: int
    episode: int
    air: date | None
    status: str = WantedStatus.WANTED
    torrent: TorrentPlan | None = None
    search_attempts: int = 0
    last_search_at: datetime | None = None
    next_search_at: datetime | None = None
    priority: int = 0
    reject: str | None = None
    forecast: dict | None = None


@dataclass
class EpisodePlan:
    season: int
    episode: int
    air: date | None
    name: str
    still: str | None


@dataclass
class ShowPlan:
    """一条订阅 + 它的条目 / 季集 / 工单 / 投递 / 额外库存文件。"""

    index: int
    kind: str  # tv / movie
    category: str
    show_type: str
    title: str
    year: int
    tmdb_status: str
    library: Library
    runtime: int
    poster: str
    backdrop: str
    logo: str
    sub_status: str
    created_at: datetime
    selected: list[int] = field(default_factory=list)
    follow_future: bool = False
    episodes: list[EpisodePlan] = field(default_factory=list)
    units: list[UnitPlan] = field(default_factory=list)
    torrents: list[TorrentPlan] = field(default_factory=list)
    # 订阅之前就在库里的季（不生成工单，只有库存文件）：(季, 集, 入账时间)
    prior_files: list[tuple[int, int, datetime]] = field(default_factory=list)
    release_date: date | None = None
    paused_at: datetime | None = None


class Planner:
    """按类目生成订阅计划。所有随机性来自同一个固定种子的 rng，时间相对 now。"""

    def __init__(self, rng: random.Random, now: datetime, libraries: dict[str, Library]) -> None:
        self.rng = rng
        self.now = now
        self.today = now.replace(tzinfo=UTC).astimezone(SITE_TZ).date()
        self.libraries = libraries
        self.used_titles: set[str] = set()
        self.title_cursor = {kind: 0 for kind in TITLES}
        self.plans: list[ShowPlan] = []

    # —— 小工具 ——————————————————————————————————————————————————————

    def at_local(self, day: date, hour: float) -> datetime:
        """站点当地某天某钟点 → 库里的 naive UTC。"""
        local = datetime.combine(day, time(0), tzinfo=SITE_TZ) + timedelta(hours=hour)
        return local.astimezone(UTC).replace(tzinfo=None)

    def token(self) -> str:
        return f"{self.rng.getrandbits(64):016x}"

    def info_hash(self) -> str:
        return f"{self.rng.getrandbits(160):040x}"

    def title(self, kind: str) -> str:
        pool = TITLES[kind]
        while True:
            cursor = self.title_cursor[kind]
            self.title_cursor[kind] += 1
            base = pool[cursor % len(pool)]
            lap = cursor // len(pool)
            title = base if lap == 0 else f"{base} 第{'二三四五六'[lap - 1]}季"
            if title not in self.used_titles:
                self.used_titles.add(title)
                return title

    def not_after_now(self, moment: datetime, slack_minutes: tuple[int, int] = (6, 90)) -> datetime:
        """刚播的集算出来的入库时刻可能晚于现在：夹回到「现在之前几十分钟」。"""
        limit = self.now - timedelta(minutes=slack_minutes[0])
        if moment <= limit:
            return moment
        return self.now - timedelta(minutes=self.rng.randint(*slack_minutes))

    def schedule(self, first: date, count: int, cadence: str) -> list[date]:
        """按排播节奏展开一季的播出日。"""
        days: list[date] = []
        cursor = first
        while len(days) < count:
            if cadence == "weekly":
                days.append(first + timedelta(weeks=len(days)))
            elif cadence == "twice_weekly":  # 韩剧：每周连播两天
                week, second = divmod(len(days), 2)
                days.append(first + timedelta(weeks=week, days=second))
            else:  # weekdays：国产剧工作日日更
                if cursor.weekday() < 5:
                    days.append(cursor)
                cursor += timedelta(days=1)
        return days

    def season_span(self, show_type: str, count: int) -> int:
        """一季从首播到最后一集的天数（按排播节奏）。"""
        days = self.schedule(self.today, count, SHOW_TYPES[show_type][3])
        return (days[-1] - days[0]).days

    def new_plan(
        self,
        kind: str,
        show_type: str,
        category: str,
        *,
        sub_status: str,
        created_at: datetime,
        tmdb_status: str,
        year: int,
    ) -> ShowPlan:
        library_name = SHOW_TYPES[show_type][0] if kind == "tv" else self.movie_library()
        plan = ShowPlan(
            index=len(self.plans),
            kind=kind,
            category=category,
            show_type=show_type,
            title=self.title(show_type if kind == "tv" else "movie"),
            year=year,
            tmdb_status=tmdb_status,
            library=self.libraries[library_name],
            runtime=SHOW_TYPES[show_type][1]
            if kind == "tv"
            else self.rng.choice([98, 112, 126, 141, 166]),
            poster=f"/poster_{self.token()}.jpg",
            backdrop=f"/backdrop_{self.token()}.jpg",
            # 约 70% 有片名 Logo；其余是空串（「取过但该片没有」，前端回落文字片名）
            logo=f"/logo_{self.token()}.png" if self.rng.random() < 0.7 else "",
            sub_status=sub_status,
            created_at=created_at,
        )
        self.plans.append(plan)
        return plan

    def movie_library(self) -> str:
        roll = self.rng.random()
        return "电影" if roll < 0.7 else "4K 电影" if roll < 0.85 else "华语电影"

    def add_season(
        self, plan: ShowPlan, season: int, first: date | None, count: int
    ) -> list[EpisodePlan]:
        cadence = SHOW_TYPES[plan.show_type][3]
        days: list[date | None] = self.schedule(first, count, cadence) if first else [None] * count
        episodes = []
        for number, day in enumerate(days, start=1):
            aired = day is not None and day < self.today
            name = (
                f"第{number}集"
                if plan.show_type in ("cn", "kr", "variety")
                else (f"第{number}话" if plan.show_type == "anime" else f"Episode {number}")
            )
            episodes.append(
                EpisodePlan(
                    season, number, day, name, f"/still_{self.token()}.jpg" if aired else None
                )
            )
        plan.episodes.extend(episodes)
        return episodes

    def torrent_name(self, plan: ShowPlan, units: list[UnitPlan]) -> str:
        slug = f"MC{TMDB_ID_BASE + plan.index}"
        group = self.rng.choice(GROUPS)
        if plan.kind == "movie":
            return f"{slug}.{plan.year}.2160p.WEB-DL.H265.HDR.DDP5.1-{group}"
        if len(units) == 1:
            unit = units[0]
            return f"{slug}.S{unit.season:02d}E{unit.episode:02d}.1080p.WEB-DL.H264.AAC-{group}"
        return f"{slug}.S{units[0].season:02d}.1080p.WEB-DL.H265.DDP5.1-{group}"

    def deliver(
        self,
        plan: ShowPlan,
        units: list[UnitPlan],
        grabbed_at: datetime,
        *,
        status: str = WantedStatus.IMPORTED,
        progress: float = 1.0,
    ) -> TorrentPlan:
        """给一组单元建一次投递并推进到指定状态（imported / downloaded / grabbed）。"""
        grabbed_at = self.not_after_now(grabbed_at, (45, 180))
        downloaded_at = imported_at = None
        if status in (WantedStatus.DOWNLOADED, WantedStatus.IMPORTED):
            downloaded_at = self.not_after_now(
                grabbed_at + timedelta(minutes=self.rng.randint(8, 50) * max(1, len(units) // 4)),
                (4, 30),
            )
        if status == WantedStatus.IMPORTED:
            imported_at = self.not_after_now(
                downloaded_at + timedelta(minutes=self.rng.randint(1, 6)), (2, 20)
            )
        per_unit = (plan.runtime * 60 * self.rng.randint(4_000_000, 12_000_000)) // 8
        torrent = TorrentPlan(
            info_hash=self.info_hash(),
            title="",
            site=self.rng.choice(SITES),
            torrent_id=str(self.rng.randint(100_000, 999_999)),
            size=per_unit * len(units),
            grabbed_at=grabbed_at,
            downloaded_at=downloaded_at,
            imported_at=imported_at,
            units=units,
            progress=progress,
        )
        torrent.title = self.torrent_name(plan, units)
        for unit in units:
            unit.status = status
            unit.torrent = torrent
        plan.torrents.append(torrent)
        return torrent

    def undeliver(self, plan: ShowPlan, unit: UnitPlan) -> None:
        """把一个单元从它的投递里摘出来（整季包里「其实少了这一集」）。"""
        torrent = unit.torrent
        if torrent is not None:
            torrent.units.remove(unit)
            if not torrent.units:
                plan.torrents.remove(torrent)
        unit.torrent = None

    def live_import(self, plan: ShowPlan, unit: UnitPlan) -> None:
        """跟播：播出后一两个小时出种、投递、入库（单集种）。"""
        assert unit.air is not None
        publish = self.at_local(
            unit.air, SHOW_TYPES[plan.show_type][4] + self.rng.uniform(0.6, 3.0)
        )
        self.deliver(plan, [unit], publish + timedelta(minutes=self.rng.randint(5, 40)))

    def want(self, unit: UnitPlan, *, searching: bool) -> None:
        """未满足的工单。

        ``searching=True``：该搜的（已播的缺集 / 已上映的电影）——已经真实搜过若干轮，
        按退避排着下一次，约一半带最近一次的拒绝原因；否则按 core.schedule_for 的口径：
        有播出日 = 追新（播出日 + 48h 首搜、高优先级），没有 = 未定档（不可调度）。
        """
        unit.status = WantedStatus.WANTED
        if searching:
            unit.search_attempts = self.rng.randint(2, 24)
            unit.last_search_at = self.now - timedelta(minutes=self.rng.randint(20, 60 * 30))
            unit.next_search_at = unit.last_search_at + timedelta(
                hours=self.rng.choice([6, 12, 24, 72])
            )
            if self.rng.random() < 0.55:
                site = self.rng.choice(SITES)[1]
                unit.reject = self.rng.choice(
                    [
                        f"{site} · 候选分辨率 720p，低于规则组要求的 1080p",
                        f"{site} · 做种人数 0，规则组要求至少 1 个做种",
                        f"{site} · 候选标注的季集与期望单元不符",
                        f"{site} · 片名相同但年份对不上（疑似同名作品）",
                    ]
                )
        elif unit.air is None:
            unit.next_search_at = None
        else:
            unit.next_search_at = datetime.combine(unit.air, time(0)) + FUTURE_GRACE
            unit.priority = 10

    def forecast(self, plan: ShowPlan, unit: UnitPlan, samples: int) -> dict:
        """照 release_forecast 的快照形态造一份「资源发布时间预测」。"""
        assert unit.air is not None
        predicted = self.at_local(
            unit.air, SHOW_TYPES[plan.show_type][4] + self.rng.uniform(0.8, 2.5)
        )
        confidence = "bootstrap" if samples == 1 else "growing" if samples == 2 else "stable"
        lower, upper = {"bootstrap": (-2, 8), "growing": (-1.5, 6), "stable": (-1, 1.5)}[confidence]
        start, end = predicted + timedelta(hours=lower), predicted + timedelta(hours=upper)

        def iso(moment: datetime) -> str:
            return moment.replace(tzinfo=UTC).isoformat()

        span = end - start
        probes = [iso(start + span * fraction) for fraction in (0.2, 0.5, 0.8)]
        generated = self.now - timedelta(minutes=self.rng.randint(10, 600))
        site = self.rng.choice(SITES)[0]
        forecast = {
            "version": 1,
            "generated_at": iso(generated),
            "target_air_date": unit.air.isoformat(),
            "predicted_at": iso(predicted),
            "window_start": iso(start),
            "window_end": iso(end),
            "confidence": confidence,
            "sample_count": samples,
            "cadence_days": 7,
            "basis_units": [[unit.season, max(1, unit.episode - n)] for n in range(samples, 0, -1)],
            "basis_torrent_row_ids": [],
            "sites": [
                {
                    "site_id": site,
                    "predicted_at": iso(predicted),
                    "window_start": iso(start),
                    "window_end": iso(end),
                    "lag_minutes": 0,
                    "coverage_count": samples,
                    "probe_times": probes if confidence != "volatile" else [],
                }
            ],
        }
        forecast["first"] = {
            key: forecast[key]
            for key in (
                "generated_at",
                "predicted_at",
                "window_start",
                "window_end",
                "confidence",
                "sample_count",
            )
        }
        return forecast

    def units_for(self, plan: ShowPlan, episodes: list[EpisodePlan]) -> list[UnitPlan]:
        units = [UnitPlan(ep.season, ep.episode, ep.air) for ep in episodes]
        plan.units.extend(units)
        return units

    def prior_seasons(self, plan: ShowPlan, upto: int, *, last_air_before: date) -> None:
        """订阅之前的季：只有元数据；约 30% 的剧这几季早就在库里（只有文件、没有工单）。"""
        in_library = self.rng.random() < 0.3
        count_range = SHOW_TYPES[plan.show_type][2]
        first = last_air_before - timedelta(days=365 * (upto - 1) + self.rng.randint(200, 400))
        for season in range(1, upto):
            episodes = self.add_season(plan, season, first, self.rng.randint(*count_range))
            if in_library:
                added = self.at_local(episodes[-1].air or first, 12) + timedelta(
                    days=self.rng.randint(1, 60)
                )
                for ep in episodes:
                    if self.rng.random() > 0.08:  # 老季偶尔缺一两集
                        plan.prior_files.append((ep.season, ep.episode, min(added, self.now)))
            first += timedelta(days=365)
        plan.year = (plan.episodes[0].air or self.today).year if plan.episodes else plan.year

    # —— 剧集类目 ——————————————————————————————————————————————————————

    def plan_tv(self) -> None:
        for category, types in TV_PLAN.items():
            for show_type, count in types.items():
                for _ in range(count):
                    getattr(self, f"_tv_{category}")(show_type)

    def _tv_airing(self, show_type: str) -> None:
        """正在播出的周更剧：当季全勾 + 追新；已播的跟播入库，未播的追新（进一周预告）。"""
        rng = self.rng
        seasons = rng.randint(1, 4)
        count = rng.randint(*SHOW_TYPES[show_type][2])
        cadence_days = 7 if SHOW_TYPES[show_type][3] == "weekly" else 3.5
        aired_target = rng.randint(2, count - 2)
        first = self.today - timedelta(days=int(cadence_days * aired_target) + rng.randint(0, 6))
        plan = self.new_plan(
            "tv",
            show_type,
            "airing",
            sub_status=SubscriptionStatus.ACTIVE,
            created_at=self.at_local(first - timedelta(days=rng.randint(3, 40)), 10),
            tmdb_status="Returning Series",
            year=first.year,
        )
        self.prior_seasons(plan, seasons, last_air_before=first)
        episodes = self.add_season(plan, seasons, first, count)
        plan.selected, plan.follow_future = [seasons], True
        units = self.units_for(plan, episodes)
        aired = [u for u in units if u.air is not None and u.air < self.today]
        upcoming = [u for u in units if u.air is not None and u.air >= self.today]
        for unit in aired:
            if rng.random() < 0.03:
                self.want(unit, searching=True)  # 偶发缺集：一直找不到资源
            else:
                self.live_import(plan, unit)
        for position, unit in enumerate(upcoming):
            self.want(unit, searching=False)
            if position == 0 and rng.random() < 0.6:
                unit.forecast = self.forecast(plan, unit, min(4, max(1, len(aired))))

    def _tv_daily(self, show_type: str) -> None:
        """工作日日更的国产剧：一周预告里一部剧占五六天。"""
        rng = self.rng
        count = rng.randint(32, 36)
        first = self.today - timedelta(days=rng.randint(14, 20))
        plan = self.new_plan(
            "tv",
            show_type,
            "daily",
            sub_status=SubscriptionStatus.ACTIVE,
            created_at=self.at_local(first - timedelta(days=2), 21),
            tmdb_status="Returning Series",
            year=first.year,
        )
        episodes = self.add_season(plan, 1, first, count)
        plan.selected, plan.follow_future = [1], True
        for unit in self.units_for(plan, episodes):
            if unit.air is not None and unit.air < self.today:
                self.live_import(plan, unit)
            else:
                self.want(unit, searching=False)

    def _tv_premiere(self, show_type: str) -> None:
        """只追未来：新一季下周内首播，全季工单都是追新。"""
        rng = self.rng
        seasons = rng.randint(2, 4)
        premiere = self.today + timedelta(days=rng.randint(1, 6))
        plan = self.new_plan(
            "tv",
            show_type,
            "premiere",
            sub_status=SubscriptionStatus.ACTIVE,
            created_at=self.now - timedelta(days=rng.randint(2, 30)),
            tmdb_status="Returning Series",
            year=premiere.year,
        )
        self.prior_seasons(plan, seasons, last_air_before=premiere - timedelta(days=300))
        episodes = self.add_season(plan, seasons, premiere, rng.randint(*SHOW_TYPES[show_type][2]))
        plan.selected, plan.follow_future = [], True
        for unit in self.units_for(plan, episodes):
            self.want(unit, searching=False)

    def _tv_between(self, show_type: str) -> None:
        """季间歇 / 追平了：上一季全部入库。

        下一季 30% 已定档（都在一周之后，不进一周预告）、15% 未定档、其余没消息。
        """
        rng = self.rng
        seasons = rng.randint(1, 4)
        count = rng.randint(*SHOW_TYPES[show_type][2])
        # 上一季至少两周前就播完了：首播日 = 今天 - 整季跨度 - 两周到一年
        first = self.today - timedelta(
            days=self.season_span(show_type, count) + rng.randint(14, 350)
        )
        plan = self.new_plan(
            "tv",
            show_type,
            "between",
            sub_status=SubscriptionStatus.ACTIVE,
            created_at=self.at_local(first - timedelta(days=rng.randint(1, 30)), 11),
            tmdb_status="Returning Series",
            year=first.year,
        )
        self.prior_seasons(plan, seasons, last_air_before=first)
        episodes = self.add_season(plan, seasons, first, count)
        plan.selected, plan.follow_future = [seasons], True
        for unit in self.units_for(plan, episodes):
            self.live_import(plan, unit)
        roll = rng.random()
        if roll < 0.45:
            dated = roll < 0.30
            premiere = self.today + timedelta(days=rng.randint(12, 90)) if dated else None
            upcoming = self.add_season(
                plan, seasons + 1, premiere, rng.randint(*SHOW_TYPES[show_type][2])
            )
            for unit in self.units_for(plan, upcoming):
                self.want(unit, searching=False)

    def _tv_backfill(
        self,
        show_type: str,
        *,
        sub_status: str = SubscriptionStatus.ACTIVE,
        category: str = "backfill",
    ) -> None:
        """补缺失：已完结的老剧，勾选若干季；大部分整季包入库，剩下的在搜（带退避与拒绝原因）。"""
        rng = self.rng
        seasons = rng.randint(2, 5)
        count_range = SHOW_TYPES[show_type][2]
        first = self.today - timedelta(days=365 * (seasons + rng.randint(1, 7)))
        created = self.now - timedelta(days=rng.randint(5, 220), hours=rng.randint(0, 23))
        plan = self.new_plan(
            "tv",
            show_type,
            category,
            sub_status=sub_status,
            created_at=created,
            tmdb_status=rng.choice(["Ended", "Ended", "Canceled"]),
            year=first.year,
        )
        selected = (
            list(range(1, seasons + 1))
            if rng.random() < 0.6
            else list(range(1, rng.randint(2, seasons) + 1))
        )
        plan.selected, plan.follow_future = selected, False
        recent_pack = rng.random() < 0.18  # 少数剧最近三天刚补进一整季：进「刚刚入库」候选
        for season in range(1, seasons + 1):
            episodes = self.add_season(
                plan, season, first + timedelta(days=365 * (season - 1)), rng.randint(*count_range)
            )
            if season not in selected:
                continue
            units = self.units_for(plan, episodes)
            roll = rng.random()
            if roll < 0.7:  # 整季包已入库
                span = max(1, int((self.now - created).total_seconds() // 3600))
                at = created + timedelta(hours=rng.randint(1, span))
                if recent_pack and season == selected[-1]:
                    at = self.now - timedelta(hours=rng.randint(4, 72))
                self.deliver(plan, units, at)
            elif roll < 0.85:  # 整季都还缺
                for unit in units:
                    self.want(unit, searching=True)
            else:  # 零星缺集：包里少了几集
                missing = set(rng.sample(range(len(units)), k=rng.randint(1, 3)))
                self.deliver(
                    plan,
                    [u for i, u in enumerate(units) if i not in missing],
                    created + timedelta(hours=rng.randint(1, 48)),
                )
                for i in missing:
                    self.want(units[i], searching=True)
        if (
            all(u.status == WantedStatus.IMPORTED for u in plan.units)
            and sub_status == SubscriptionStatus.ACTIVE
        ):
            # 补缺失的订阅至少还缺一集，否则就是已完成：让最后一集成为包里缺的那集
            self.undeliver(plan, plan.units[-1])
            self.want(plan.units[-1], searching=True)

    def _tv_completed(self, show_type: str) -> None:
        """已收齐：完结剧全季入库。少数剧最后一季是最近几天才补齐的。"""
        rng = self.rng
        seasons = rng.randint(1, 4)
        first = self.today - timedelta(
            days=365 * (seasons + rng.randint(0, 5)) + rng.randint(30, 200)
        )
        created = self.now - timedelta(days=rng.randint(8, 700))
        plan = self.new_plan(
            "tv",
            show_type,
            "completed",
            sub_status=SubscriptionStatus.COMPLETED,
            created_at=created,
            tmdb_status="Ended",
            year=first.year,
        )
        plan.selected = list(range(1, seasons + 1))
        plan.follow_future = rng.random() < 0.2
        recent = rng.random() < 0.08
        for season in range(1, seasons + 1):
            episodes = self.add_season(
                plan,
                season,
                first + timedelta(days=365 * (season - 1)),
                rng.randint(*SHOW_TYPES[show_type][2]),
            )
            units = self.units_for(plan, episodes)
            at = created + timedelta(hours=rng.randint(1, 72) + 24 * (season - 1))
            if recent and season == seasons:
                at = self.now - timedelta(hours=rng.randint(6, 120))
            self.deliver(plan, units, at)

    def _tv_paused(self, show_type: str) -> None:
        """已暂停：一部分是追到一半停了的在播剧，其余是停掉的补缺失。"""
        if self.rng.random() < 0.36:
            self._tv_airing(show_type)
            plan = self.plans[-1]
            plan.category, plan.sub_status = "paused", SubscriptionStatus.PAUSED
            # 暂停之后播出的集不再有人投递：退回 wanted（首页预告也不会再算它们）
            paused_at = self.now - timedelta(days=self.rng.randint(8, 20))
            plan.paused_at = paused_at
            for unit in plan.units:
                if unit.torrent is not None and unit.torrent.grabbed_at > paused_at:
                    self.undeliver(plan, unit)
                    self.want(unit, searching=False)
            return
        self._tv_backfill(show_type, sub_status=SubscriptionStatus.PAUSED, category="paused")
        self.plans[-1].paused_at = self.now - timedelta(days=self.rng.randint(1, 40))

    # —— 电影 ————————————————————————————————————————————————————————

    def plan_movies(self) -> None:
        rng = self.rng
        for category, count in MOVIE_PLAN.items():
            for _ in range(count):
                status = {
                    "completed": SubscriptionStatus.COMPLETED,
                    "paused": SubscriptionStatus.PAUSED,
                }.get(category, SubscriptionStatus.ACTIVE)
                if category == "unreleased":
                    release = self.today + timedelta(days=rng.randint(10, 200))
                    tmdb_status = rng.choice(["Post Production", "In Production"])
                elif category == "just_released":
                    release = self.today - timedelta(days=rng.randint(1, 14))
                    tmdb_status = "Released"
                else:
                    release = self.today - timedelta(days=rng.randint(60, 365 * 12))
                    tmdb_status = "Released"
                created = self.now - timedelta(days=rng.randint(1, 400))
                if category == "just_released":
                    created = self.at_local(release, 12) - timedelta(days=rng.randint(10, 90))
                plan = self.new_plan(
                    "movie",
                    "movie",
                    category,
                    sub_status=status,
                    created_at=created,
                    tmdb_status=tmdb_status,
                    year=release.year,
                )
                plan.release_date = release
                unit = UnitPlan(0, 0, None)  # 电影哨兵单元不写播出日（见 core.movie_schedule）
                plan.units.append(unit)
                if category == "completed":
                    at = created + timedelta(hours=rng.randint(1, 200))
                    if plan.index % 8 == 0:
                        at = self.now - timedelta(hours=rng.randint(3, 140))  # 最近几天刚入库
                    self.deliver(plan, [unit], at)
                elif category == "pipeline":
                    done = plan.index % 3 == 0
                    self.deliver(
                        plan,
                        [unit],
                        self.now - timedelta(minutes=rng.randint(25, 240)),
                        status=WantedStatus.DOWNLOADED if done else WantedStatus.GRABBED,
                        progress=1.0 if done else round(rng.uniform(0.08, 0.93), 3),
                    )
                else:
                    available = datetime.combine(release, time(0)) + MOVIE_RELEASE_GRACE
                    if available > self.now:  # 未上映 / 刚上映：被动匹配为主，到点兜底
                        unit.next_search_at, unit.priority = available, 10
                    else:
                        self.want(unit, searching=True)

    # —— 在途单元：从周更剧与补缺失里挑几集改成「下载中 / 整理中」————————————

    def assign_pipeline(self) -> None:
        for category, count in PIPELINE_TV.items():
            candidates = [
                p
                for p in self.plans
                if p.category == category and p.sub_status == SubscriptionStatus.ACTIVE
            ]
            for position, plan in enumerate(self.rng.sample(candidates, k=count)):
                if category == "airing":
                    unit = max(
                        (u for u in plan.units if u.status == WantedStatus.IMPORTED),
                        key=lambda u: (u.season, u.episode),
                    )
                    self.undeliver(plan, unit)  # 最新一集改成还在下载 / 整理
                else:
                    unit = next(u for u in reversed(plan.units) if u.status == WantedStatus.WANTED)
                done = position % 2 == 1
                self.deliver(
                    plan,
                    [unit],
                    self.now - timedelta(minutes=self.rng.randint(20, 150)),
                    status=WantedStatus.DOWNLOADED if done else WantedStatus.GRABBED,
                    progress=1.0 if done else round(self.rng.uniform(0.1, 0.9), 3),
                )
                unit.search_attempts = unit.search_attempts or 1


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


def _unit_text(units: list[UnitPlan]) -> str:
    if len(units) == 1:
        unit = units[0]
        return (
            "电影"
            if (unit.season, unit.episode) == (0, 0)
            else f"S{unit.season:02d}E{unit.episode:02d}"
        )
    return (
        f"S{units[0].season:02d}E{units[0].episode:02d}–E{units[-1].episode:02d}（{len(units)} 集）"
    )


def _iso(moment: datetime | None) -> str | None:
    return moment.replace(tzinfo=UTC).isoformat() if moment else None


_AUDIO = [
    {
        "codec": "eac3",
        "profile": None,
        "channels": 6,
        "channel_layout": "5.1",
        "language": "zho",
        "title": "国语",
        "default": True,
    }
]
_SUBS = [{"codec": "subrip", "language": "zho", "title": "简体", "forced": False, "default": True}]


def materialize(session: Session, planner: Planner, rule_set_id: int) -> dict:
    rng, now = planner.rng, planner.now
    stats = {
        "media_items": 0,
        "episodes": 0,
        "wanted": 0,
        "files": 0,
        "attempts": 0,
        "activities": 0,
    }

    # 1) 条目 + 元数据 + 季 + 集
    items: list[MediaItem] = []
    for plan in planner.plans:
        first_air = min((ep.air for ep in plan.episodes if ep.air), default=plan.release_date)
        item = MediaItem(
            kind=plan.kind,
            tmdb_id=TMDB_ID_BASE + plan.index,
            external_id=str(TMDB_ID_BASE + plan.index),
            imdb_id=f"tt{9_100_000 + plan.index}",
            douban_id=str(36_000_000 + plan.index) if rng.random() < 0.7 else None,
            title=plan.title,
            original_title=plan.title,
            english_title=None,
            year=first_air.year if first_air else plan.year,
            aliases=[],
            identity_twins=[],
            status=plan.tmdb_status,
            poster_path=plan.poster,
            backdrop_path=plan.backdrop,
            logo_path=plan.logo,
            scrape_library_id=plan.library.id,
            metadata_refreshed_at=now - timedelta(hours=rng.randint(1, 96)),
            next_refresh_at=now + timedelta(hours=rng.randint(6, 72)),
            created_at=plan.created_at,
            updated_at=now - timedelta(hours=rng.randint(1, 96)),
        )
        items.append(item)
    session.add_all(items)
    session.flush()
    stats["media_items"] = len(items)

    rows: list = []
    for plan, item in zip(planner.plans, items, strict=True):
        genres = rng.sample(GENRES[plan.show_type], k=min(2, len(GENRES[plan.show_type])))
        first_air = min((ep.air for ep in plan.episodes if ep.air), default=plan.release_date)
        rows.append(
            MediaMetadata(
                media_item_id=item.id,
                overview="这是一段用于压测的剧情简介，长度与真实刮削结果相当。" * 4,
                tagline=None,
                genres=[g[1] for g in genres],
                genre_ids=[g[0] for g in genres],
                runtime_minutes=plan.runtime,
                release_date=first_air,
                original_language={"us": "en", "anime": "ja", "kr": "ko"}.get(plan.show_type, "zh"),
                origin_countries=COUNTRIES.get(
                    plan.show_type, ["US"] if rng.random() < 0.5 else ["CN"]
                ),
                studios=["压测影业"],
                vote_average=round(rng.uniform(6.2, 9.3), 1),
                vote_count=rng.randint(80, 30_000),
                directors=["某导演"],
                cast=[
                    {
                        "name": f"演员{i}",
                        "character": f"角色{i}",
                        "order": i,
                        "profile_path": f"/a{i}.jpg",
                    }
                    for i in range(8)
                ],
                scraped_at=now - timedelta(days=rng.randint(0, 30)),
                scrape_language="zh-CN",
                created_at=plan.created_at,
                updated_at=now,
            )
        )
        seasons: dict[int, list[EpisodePlan]] = {}
        for ep in plan.episodes:
            seasons.setdefault(ep.season, []).append(ep)
        for number, episodes in seasons.items():
            rows.append(
                MediaSeason(
                    media_item_id=item.id,
                    season_number=number,
                    name=f"第 {number} 季",
                    air_date=episodes[0].air,
                    episode_count=len(episodes),
                    overview="季简介",
                    poster_path=f"/poster_{planner.token()}.jpg",
                )
            )
            for ep in episodes:
                rows.append(
                    MediaEpisode(
                        media_item_id=item.id,
                        season_number=ep.season,
                        episode_number=ep.episode,
                        name=ep.name,
                        overview="分集简介，压测用。" * 3,
                        air_date=ep.air,
                        runtime_minutes=plan.runtime,
                        vote_average=round(rng.uniform(6.5, 9.5), 1),
                        still_path=ep.still,
                    )
                )
                stats["episodes"] += 1
    session.add_all(rows)
    session.flush()

    # 2) 订阅（last_activity_at 先落创建时刻，之后插入活动时由触发器推进）
    rule_subs: list[Subscription] = []
    for plan, item in zip(planner.plans, items, strict=True):
        default_library = plan.library.is_default
        rule_subs.append(
            Subscription(
                media_item_id=item.id,
                kind=plan.kind,
                selected_seasons=plan.selected,
                follow_future=plan.follow_future,
                library_id=None if default_library else plan.library.id,
                rule_set_id=rule_set_id,
                status=plan.sub_status,
                created_by_member_id=None,
                last_activity_at=plan.created_at,
                created_at=plan.created_at,
                updated_at=plan.created_at,
            )
        )
    session.add_all(rule_subs)
    session.flush()

    # 3) 工单 + 投递尝试 + 库存文件
    wanted_rows: list[tuple[UnitPlan, WantedItem]] = []
    attempts: list[tuple[TorrentPlan, SubscriptionDownloadAttempt]] = []
    files: list[LibraryFile] = []
    for plan, item, sub in zip(planner.plans, items, rule_subs, strict=True):
        for unit in plan.units:
            torrent = unit.torrent
            row = WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=unit.season,
                episode_number=unit.episode,
                status=unit.status,
                in_scope=True,
                air_date=unit.air,
                priority=unit.priority,
                next_search_at=None if torrent else unit.next_search_at,
                search_attempts=unit.search_attempts,
                last_search_at=unit.last_search_at,
                release_forecast=unit.forecast,
                grabbed_at=torrent.grabbed_at if torrent else None,
                last_reject_reason=unit.reject,
                grab_title=f"{torrent.site[1]} · {torrent.title}" if torrent else None,
                info_hash=torrent.info_hash if torrent else None,
                downloaded_at=torrent.downloaded_at if torrent else None,
                imported_at=torrent.imported_at if torrent else None,
                created_at=plan.created_at,
                updated_at=(torrent.imported_at or torrent.downloaded_at or torrent.grabbed_at)
                if torrent
                else (unit.last_search_at or plan.created_at),
            )
            wanted_rows.append((unit, row))
        for torrent in plan.torrents:
            in_flight = torrent.imported_at is None
            attempt_status = (
                DownloadAttemptStatus.IMPORTED
                if not in_flight
                else DownloadAttemptStatus.COMPLETED
                if torrent.downloaded_at
                else DownloadAttemptStatus.ACTIVE
            )
            attempts.append(
                (
                    torrent,
                    SubscriptionDownloadAttempt(
                        subscription_id=sub.id,
                        info_hash=torrent.info_hash,
                        site_id=torrent.site[0],
                        torrent_id=torrent.torrent_id,
                        torrent_title=torrent.title,
                        identity_confidence="exact_id",
                        download_name=torrent.title,
                        save_path=plan.library.root_paths[0],
                        units=[[u.season, u.episode] for u in torrent.units],
                        quality={
                            "resolution": "1080p" if plan.kind == "tv" else "2160p",
                            "source": "WEB-DL",
                        },
                        owned_by_movieclaw=True,
                        purpose="download",
                        status=attempt_status,
                        last_downloader_state="downloading"
                        if attempt_status == DownloadAttemptStatus.ACTIVE
                        else "completed",
                        last_observed_at=torrent.imported_at
                        or now - timedelta(seconds=rng.randint(5, 60)),
                        completed_at=torrent.downloaded_at,
                        last_completed_bytes=int(torrent.size * torrent.progress),
                        last_downloaded_bytes=int(torrent.size * torrent.progress),
                        last_progress_at=torrent.downloaded_at
                        or now - timedelta(seconds=rng.randint(5, 90)),
                        created_at=torrent.grabbed_at,
                        updated_at=torrent.imported_at or now,
                    ),
                )
            )
            if torrent.imported_at is None:
                continue
            for unit in torrent.units:
                files.append(
                    _file(
                        plan,
                        item,
                        unit.season,
                        unit.episode,
                        torrent.imported_at,
                        rng,
                        source="imported",
                        torrent=torrent,
                    )
                )
        for season, episode, added in plan.prior_files:
            files.append(
                _file(plan, item, season, episode, added, rng, source="scanned", torrent=None)
            )
    session.add_all([row for _unit, row in wanted_rows])
    session.add_all([attempt for _torrent, attempt in attempts])
    session.add_all(files)
    session.flush()
    stats.update(wanted=len(wanted_rows), attempts=len(attempts), files=len(files))

    # 4) 活动流水（触发器随插入推进 subscription.last_activity_at）
    activities: list[SubscriptionActivity] = []
    wanted_by_unit = {(id(unit)): row for unit, row in wanted_rows}
    for plan, sub in zip(planner.plans, rule_subs, strict=True):
        seasons_text = "、".join(f"第 {s} 季" for s in plan.selected) or "只追新集"
        created_msg = (
            f"订阅了《{plan.title}》"
            if plan.kind == "movie"
            else f"订阅了《{plan.title}》：{seasons_text}"
            + ("，并自动追更新集" if plan.follow_future else "")
        )
        activities.append(
            SubscriptionActivity(
                subscription_id=sub.id,
                type=ActivityType.CREATED,
                message=created_msg,
                payload={"selected_seasons": plan.selected, "follow_future": plan.follow_future},
                created_at=plan.created_at,
                updated_at=plan.created_at,
            )
        )
        for torrent in plan.torrents:
            text = _unit_text(torrent.units)
            units_payload = [[u.season, u.episode] for u in torrent.units]
            first_row = wanted_by_unit.get(id(torrent.units[0]))
            activities.append(
                SubscriptionActivity(
                    subscription_id=sub.id,
                    wanted_item_id=first_row.id if first_row else None,
                    type=ActivityType.GRABBED,
                    message=f"已投递 {text}：{torrent.site[1]} · {torrent.title}",
                    payload={
                        "site_id": torrent.site[0],
                        "torrent_id": torrent.torrent_id,
                        "info_hash": torrent.info_hash,
                        "units": units_payload,
                        "submitted_at": _iso(torrent.grabbed_at),
                        "resource_publish_time": _iso(
                            torrent.grabbed_at - timedelta(minutes=rng.randint(4, 50))
                        ),
                        "resource_first_seen_at": _iso(
                            torrent.grabbed_at - timedelta(minutes=rng.randint(1, 4))
                        ),
                        "dry_run": False,
                    },
                    created_at=torrent.grabbed_at,
                    updated_at=torrent.grabbed_at,
                )
            )
            if torrent.downloaded_at:
                activities.append(
                    SubscriptionActivity(
                        subscription_id=sub.id,
                        type=ActivityType.DOWNLOADED,
                        message=f"{text} 下载完成",
                        payload={"info_hash": torrent.info_hash, "units": units_payload},
                        created_at=torrent.downloaded_at,
                        updated_at=torrent.downloaded_at,
                    )
                )
            if torrent.imported_at:
                activities.append(
                    SubscriptionActivity(
                        subscription_id=sub.id,
                        type=ActivityType.IMPORTED,
                        message=f"{text} 已整理入库到「{plan.library.name}」",
                        payload={
                            "info_hash": torrent.info_hash,
                            "units": units_payload,
                            "library_id": plan.library.id,
                        },
                        created_at=torrent.imported_at,
                        updated_at=torrent.imported_at,
                    )
                )
        searching = [u for u in plan.units if u.last_search_at]
        if searching:
            last = max(u.last_search_at for u in searching if u.last_search_at)
            activities.append(
                SubscriptionActivity(
                    subscription_id=sub.id,
                    type=ActivityType.SEARCHED,
                    message=f"搜索了 {len(SITES)} 个站点：找到 {rng.randint(0, 9)} 个候选，"
                    "暂无符合规则的资源",
                    payload={"sites": [s[0] for s in SITES], "units": len(searching)},
                    created_at=last,
                    updated_at=last,
                )
            )
        if plan.sub_status == SubscriptionStatus.COMPLETED:
            done = max(
                (t.imported_at for t in plan.torrents if t.imported_at), default=plan.created_at
            )
            activities.append(
                SubscriptionActivity(
                    subscription_id=sub.id,
                    type=ActivityType.COMPLETED,
                    message="订阅已收齐：期望的内容都已安排完毕，且暂无会新增的内容",
                    payload={},
                    created_at=done,
                    updated_at=done,
                )
            )
        if plan.sub_status == SubscriptionStatus.PAUSED and plan.paused_at:
            activities.append(
                SubscriptionActivity(
                    subscription_id=sub.id,
                    type=ActivityType.PAUSED,
                    message="已暂停追踪",
                    payload={},
                    created_at=plan.paused_at,
                    updated_at=plan.paused_at,
                )
            )
    session.add_all(activities)
    session.flush()
    stats["activities"] = len(activities)

    # 订阅的 updated_at 跟最近一次活动走（与服务层每次写订阅都刷新它的口径近似）
    latest = dict(
        session.exec(
            select(
                SubscriptionActivity.subscription_id, func.max(SubscriptionActivity.created_at)
            ).group_by(SubscriptionActivity.subscription_id)
        ).all()
    )
    for sub in rule_subs:
        sub.updated_at = latest.get(sub.id, sub.created_at)
    return stats


def _file(
    plan: ShowPlan,
    item: MediaItem,
    season: int,
    episode: int,
    added: datetime,
    rng: random.Random,
    *,
    source: str,
    torrent: TorrentPlan | None,
) -> LibraryFile:
    root = plan.library.root_paths[0]
    if plan.kind == "movie":
        path = f"{root}/{plan.title} ({plan.year})/{plan.title} ({plan.year}).mkv"
    else:
        path = (
            f"{root}/{plan.title} ({plan.year})/Season {season:02d}/"
            f"{plan.title} S{season:02d}E{episode:02d}.mkv"
        )
    duration = plan.runtime * 60 + rng.randint(-120, 120)
    return LibraryFile(
        library_id=plan.library.id,
        media_item_id=item.id,
        season_number=season,
        episode_number=episode,
        file_path=path,
        size_bytes=(torrent.size // max(1, len(torrent.units)))
        if torrent
        else rng.randint(400, 3000) * 1024 * 1024,
        file_mtime_ns=int(added.replace(tzinfo=UTC).timestamp() * 1e9),
        container="mkv",
        resolution="2160p" if plan.kind == "movie" and rng.random() < 0.6 else "1080p",
        video_codec=rng.choice(["hevc", "h264"]),
        hdr=rng.choice([None, None, "HDR10", "Dolby Vision"]),
        bit_depth=10,
        duration_seconds=max(60, duration),
        bit_rate=rng.randint(4_000_000, 25_000_000),
        frame_rate=rng.choice([23.976, 25.0]),
        color_space="BT.709",
        audio_streams=_AUDIO,
        subtitle_streams=_SUBS,
        external_subtitles=[],
        media_source="WEB-DL",
        release_group=rng.choice(GROUPS),
        source=source,
        site_id=torrent.site[0] if torrent else None,
        torrent_id=torrent.torrent_id if torrent else None,
        added_batch_id=f"sub-{plan.index}-{torrent.info_hash[:8]}"
        if torrent
        else f"scan-{plan.index}",
        state="in_place",
        identity_source="subscription_exact" if torrent else "resolved",
        resolved_version=5,
        created_at=added,
        updated_at=added,
    )


def ensure_rule_set(session: Session) -> int:
    name, spec = DEFAULT_RULE_SET
    row = session.exec(select(RuleSet).where(RuleSet.is_default.is_(True))).first()  # type: ignore[attr-defined]
    if row is None:
        row = RuleSet(name=name, is_default=True, spec=spec, match_rules=[])
        session.add(row)
        session.flush()
    assert row.id is not None
    return row.id


def write_qbt_json(path: Path, planner: Planner) -> int:
    """假 qBittorrent 的任务清单：在途工单的种子 + 几个与订阅无关的做种任务。"""
    rng, now = planner.rng, planner.now
    torrents = []
    for plan in planner.plans:
        for torrent in plan.torrents:
            if torrent.imported_at is not None:
                continue
            done = torrent.downloaded_at is not None
            speed = 0 if done else rng.randint(2, 14) * 1024 * 1024
            left = int(torrent.size * (1 - torrent.progress))
            torrents.append(
                _qbt_row(
                    torrent.info_hash,
                    torrent.title,
                    torrent.size,
                    torrent.progress,
                    speed,
                    left // speed if speed else 0,
                    "stalledUP" if done else "downloading",
                    plan.library.root_paths[0],
                    torrent.grabbed_at,
                    rng,
                )
            )
    for n in range(4):  # 与订阅无关的老种：任务中心应把它们归为「非订阅」或过滤掉
        size = rng.randint(2, 40) * 1024**3
        torrents.append(
            _qbt_row(
                planner.info_hash(),
                f"Seeding.Archive.{n + 1}.1080p.BluRay.x264-CMCT",
                size,
                1.0,
                0,
                0,
                "stalledUP",
                "/downloads/other",
                now - timedelta(days=rng.randint(30, 300)),
                rng,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(torrents, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(torrents)


def _qbt_row(
    info_hash: str,
    name: str,
    size: int,
    progress: float,
    speed: int,
    eta: int,
    state: str,
    save_path: str,
    added: datetime,
    rng: random.Random,
) -> dict:
    completed = int(size * progress)
    return {
        "hash": info_hash,
        "name": name,
        "size": size,
        "total_size": size,
        "progress": progress,
        "dlspeed": speed,
        "upspeed": rng.randint(0, 3) * 1024 * 256,
        "eta": eta if eta else 8640000,
        "state": state,
        "num_seeds": rng.randint(3, 60),
        "num_leechs": rng.randint(0, 20),
        "num_complete": rng.randint(20, 400),
        "num_incomplete": rng.randint(0, 40),
        "ratio": round(rng.uniform(0.0, 3.0), 3),
        "uploaded": int(completed * rng.uniform(0, 2)),
        "downloaded": completed,
        "completed": completed,
        "amount_left": size - completed,
        "save_path": save_path,
        "content_path": f"{save_path}/{name}",
        "added_on": int(added.replace(tzinfo=UTC).timestamp()),
        "category": "movieclaw",
        "tags": "",
        "tracker": "https://tracker.example.invalid/announce",
    }


async def refresh_library_stats(db_path: str) -> None:
    """用后端自己的 LibraryRepository.refresh_stats 重算各库统计（与扫描收尾同一实现）。"""
    from movieclaw_db.engine import init_db
    from movieclaw_db.repositories.library_repo import LibraryRepository

    database = init_db(f"sqlite+aiosqlite:///{db_path}")
    async with database.session() as session:
        repo = LibraryRepository(session)
        await repo.refresh_stats([lib.id for lib in await repo.list_all() if lib.id is not None])
        await session.commit()
    await database.dispose()


def summarize(planner: Planner) -> None:
    """按后端的口径粗算三个首页接口会返回多少（真实结果以接口为准）。"""
    today, horizon = planner.today, planner.today + timedelta(days=7)
    week = 0
    recent: set[int] = set()
    cutoff = planner.now - timedelta(days=7)
    for plan in planner.plans:
        for unit in plan.units:
            in_flight = unit.status in (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)
            upcoming = (
                unit.status == WantedStatus.WANTED
                and plan.kind == "tv"
                and plan.sub_status == SubscriptionStatus.ACTIVE
                and unit.air is not None
                and today <= unit.air <= horizon
            )
            if in_flight or upcoming:
                week += 1
            if unit.torrent and unit.torrent.imported_at and unit.torrent.imported_at >= cutoff:
                recent.add(plan.index)
    by = {}
    for plan in planner.plans:
        by.setdefault((plan.kind, plan.sub_status), 0)
        by[(plan.kind, plan.sub_status)] += 1
    print("订阅构成：" + "，".join(f"{k}/{s}={n}" for (k, s), n in sorted(by.items())))
    print(
        f"预估：一周预告 {week} 条；最近 7 天有入库的订阅 {len(recent)} 部"
        f"（未看完的才出卡，接口最多返回 12 张）"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", required=True, help="已迁移并灌好媒体库的 SQLite 文件")
    parser.add_argument(
        "--qbt-json", default=None, help="假 qBittorrent 的任务清单输出路径（可选）"
    )
    args = parser.parse_args()

    started = clock.perf_counter()
    engine = create_engine(f"sqlite:///{args.db}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # 与后端同样开外键：引用写错当场报错
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    rng = random.Random(SEED)
    now = utcnow()
    # expire_on_commit=False：提交后还要读库根路径写假 qBittorrent 清单
    with Session(engine, expire_on_commit=False) as session:
        libraries = {lib.name: lib for lib in session.exec(select(Library)).all()}
        missing = {SHOW_TYPES[t][0] for t in SHOW_TYPES} | {"电影", "4K 电影", "华语电影"}
        if not missing <= set(libraries):
            raise SystemExit(
                f"缺少目标库：{sorted(missing - set(libraries))}——请先跑 "
                "seed_library_dataset.py --profile home"
            )
        planner = Planner(rng, now, libraries)
        planner.plan_tv()
        planner.plan_movies()
        planner.assign_pipeline()
        rule_set_id = ensure_rule_set(session)
        stats = materialize(session, planner, rule_set_id)
        session.commit()
    engine.dispose()
    asyncio.run(refresh_library_stats(args.db))

    print(f"站点日历今天：{planner.today}（UTC now {now:%Y-%m-%d %H:%M}）")
    print("落库：" + "，".join(f"{k}={v:,}" for k, v in stats.items()))
    summarize(planner)
    if args.qbt_json:
        count = write_qbt_json(Path(args.qbt_json), planner)
        print(f"假 qBittorrent 任务清单：{args.qbt_json}（{count} 个任务）")
    print(f"完成，耗时 {clock.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
