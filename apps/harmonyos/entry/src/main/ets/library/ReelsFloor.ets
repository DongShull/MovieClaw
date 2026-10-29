import { Haptics } from '../common/Haptics';
import { RemoteImage } from '../common/RemoteImage';
import { Theme } from '../theme/AppTheme';

/**
 * 媒体库首页的「下拉二楼」（对应 iOS `ReelsFloor.swift` / Android `ReelsFloor.kt`）。
 *
 * 交互与两端同一套：照常下拉是刷新；刷新触发后继续往下拉，内容上方从左侧送进一条 35mm 胶片；
 * 再拉过一条线，压片板合拢、重震一下，松手进片段。
 *
 * 鸿蒙这边刷新用系统 `Refresh`：`refreshOffset` 设成 183vp（与 iPhone 17 Pro 实测的系统刷新点一致），
 * 触发后的 20vp 胶片完全入画、35vp 过线。`Refresh` 负责橡皮筋与回弹，这里只读它给的下拉量，
 * 因此不重复实现 Android 的嵌套滚动曲线。
 */
export const REELS_FLOOR_REFRESH_AT: number = 183;
const REVEAL_SPAN: number = 20;
const ARM_BEYOND: number = 35;

/** 二楼的拖动状态：页面只改它，胶片/暗层两个小组件各自通过 @ObjectLink 订阅，首页主体不逐帧重排 */
@Observed
export class ReelsFloorState {
  /** 顶端被拉下来多少（vp） */
  pull: number = 0;
  /** 这一次拖动里刷新在拉下多少时触发；-1 = 还没触发，这时既不出胶片也不过线 */
  refreshedAt: number = -1;
  /** 拉过线了，松手就进。只在拖动中更新 */
  armed: boolean = false;
  dragging: boolean = false;

  /** 刷新触发后再拉多少，胶片从无到完全入画（0～1） */
  reveal(): number {
    if (this.refreshedAt < 0) {
      return 0;
    }
    return Math.max(0, Math.min(1, (this.pull - this.refreshedAt) / REVEAL_SPAN));
  }

  /** 从刷新触发到过线走了多少（0～1）：放映灯随它变亮、首页随它变暗 */
  progress(): number {
    if (this.refreshedAt < 0) {
      return 0;
    }
    return Math.max(0, Math.min(1, (this.pull - this.refreshedAt) / ARM_BEYOND));
  }

  private pastLine(): boolean {
    return this.refreshedAt >= 0 && this.pull >= this.refreshedAt + ARM_BEYOND;
  }

  /** 新一次拖动：从头判断，不然上次松手留下的「过线」会让下一次普通滑动也进二楼 */
  begin(): void {
    this.dragging = true;
    this.pull = 0;
    this.refreshedAt = -1;
    this.armed = false;
  }

  drag(pull: number): void {
    if (!this.dragging) {
      return;
    }
    this.pull = Math.max(0, pull);
    // 记刷新点本身而不是这一帧拉到的位置：手指一帧走得远时线也不会被推远
    if (this.refreshedAt < 0 && this.pull >= REELS_FLOOR_REFRESH_AT) {
      this.refreshedAt = REELS_FLOOR_REFRESH_AT;
    }
    const past: boolean = this.pastLine();
    if (past !== this.armed) {
      this.armed = past;
    }
  }

  /** 手指抬起；返回 true = 这一下是拉过线后松手 */
  release(): boolean {
    this.dragging = false;
    return this.armed;
  }

  reset(): void {
    this.pull = 0;
    this.refreshedAt = -1;
    this.armed = false;
    this.dragging = false;
  }
}

/** 首页随下拉暗下去（只压内容，不压上面露出来的胶片）；0～0.6 */
@Component
export struct ReelsFloorDim {
  @ObjectLink floor: ReelsFloorState;

  build() {
    Column()
      .width('100%')
      .height('100%')
      .backgroundColor('#000000')
      .opacity(this.floor.reveal() * 0.3 + this.floor.progress() * 0.3)
      .hitTestBehavior(HitTestMode.None)
  }
}

/**
 * 下拉露出的那一截：贴着标题栏下沿，高度就是下拉量。
 *
 * 首页 List 的第一个 `ListItem` 是 `AppNavBar.contentTop` 的透明占位；整体被 Refresh 下移 pull 后，
 * 真正的内容从 `pull + contentTop` 开始，而标题栏占 `0～contentTop`——中间露出的空白正好是
 * `contentTop～pull+contentTop`，高度 = pull。胶片挂在这一截底边，因此和 iOS 一样不会伸到导航栏底下。
 */
@Component
export struct ReelsFloorGap {
  @ObjectLink floor: ReelsFloorState;
  @Prop stills: string[] = [];
  @Prop topInset: number = 0;

  build() {
    if (this.floor.pull > 0.5 && this.floor.reveal() > 0) {
      Column() {
        ReelsFilmStrip({
          pull: this.floor.pull,
          reveal: this.floor.reveal(),
          progress: this.floor.progress(),
          armed: this.floor.armed,
          stills: this.stills
        })
      }
      .width('100%')
      .height(Math.min(this.floor.pull, 420))
      .margin({ top: this.topInset })
      .alignItems(HorizontalAlign.Center)
      .justifyContent(FlexAlign.End)
      .clip(true)
      .hitTestBehavior(HitTestMode.None)
      .opacity(this.floor.reveal())
    }
  }
}

const FRAME_W: number = 112;
const FRAME_H: number = 70;
/** 相邻两格的间距、整条胶片的宽（含上下齿孔带） */
const STEP: number = 124;
const BAND: number = 104;
/** 手指拉一点，胶片走几点 */
const TRAVEL: number = 2.6;
/** 两侧各画几格 */
const SIDE: number = 3;
const HOLES: number[] = [0, 1, 2, 3, 4, 5, 6, 7];

/**
 * 二楼的「走片」：一条 35mm 胶片随手指穿过片门（与 iOS `ReelsFilmStrip` 同一版）。
 *
 * 冷银配色：石墨片基、冷白放映灯，光透过齿孔；片门里那格全彩，两侧褪成黑白；
 * 胶片绕着看不见的过片轮：中间拱起、两头沉下并略微转开，两头隐进黑里；
 * 胶片位置只由下拉量决定（往回推就倒片），每过一格轻震一下；
 * 过线时两块拉丝钢压片板合拢、那一格闪一下白光、重震一下。
 */
@Component
export struct ReelsFilmStrip {
  @Prop @Watch('onPullChanged') pull: number = 0;
  @Prop reveal: number = 0;
  @Prop progress: number = 0;
  @Prop @Watch('onArmedChanged') armed: boolean = false;
  @Prop stills: string[] = [];

  @State private flash: number = 0;
  private lastTick: number = 0;

  /** 过线后对齐到最近一格，让它卡进片门 */
  private filmPosition(): number {
    const raw: number = this.pull * TRAVEL;
    return this.armed ? Math.round(raw / STEP) * STEP : raw;
  }

  private base(): number {
    return Math.floor(this.filmPosition() / STEP);
  }

  private frac(): number {
    return this.filmPosition() - this.base() * STEP;
  }

  private cellX(k: number): number {
    return (k - this.base()) * STEP - this.frac();
  }

  private cellC(k: number): number {
    return this.cellX(k) / 180;
  }

  private cellNear(k: number): number {
    return Math.max(0, 1 - Math.abs(this.cellC(k)) * 1.35);
  }

  private cellLight(k: number): number {
    return Math.min(1.15, this.cellNear(k) * (0.35 + 0.65 * this.progress) + this.flash * this.cellNear(k));
  }

  private cellColor(k: number): number {
    return Math.min(1, this.cellNear(k) * (0.4 + 0.6 * this.progress));
  }

  private cellIndices(): number[] {
    const out: number[] = [];
    for (let i: number = -SIDE; i <= SIDE; i++) {
      out.push(this.base() + i);
    }
    return out;
  }

  private stillFor(k: number): string {
    if (this.stills.length === 0) {
      return '';
    }
    const index: number = ((k % this.stills.length) + this.stills.length) % this.stills.length;
    return this.stills[index];
  }

  private holeColor(k: number): string {
    return `rgba(227, 235, 247, ${(0.08 + 0.72 * Math.min(1, this.cellLight(k))).toFixed(3)})`;
  }

  private onPullChanged(): void {
    if (this.pull <= 0) {
      this.lastTick = 0;
      return;
    }
    if (this.armed) {
      return;
    }
    const index: number = Math.round(this.filmPosition() / STEP);
    if (index !== this.lastTick) {
      this.lastTick = index;
      Haptics.buzz(7);
    }
  }

  private onArmedChanged(): void {
    if (!this.armed) {
      this.flash = 0;
      return;
    }
    Haptics.buzz(42);
    this.flash = 1;
    this.getUIContext().animateTo({ duration: 550, curve: Curve.EaseOut }, (): void => {
      this.flash = 0;
    });
  }

  build() {
    Column({ space: 10 }) {
      Text(this.armed ? '松手，开始欣赏' : '再拉一点，开始欣赏片段')
        .fontSize(13)
        .fontWeight(FontWeight.Medium)
        .fontColor(this.armed ? Theme.accentStrong : Theme.textMuted)
        .letterSpacing(1.2)
        .maxLines(1)

      Stack({ alignContent: Alignment.Center }) {
        // 放映灯：冷白，随过线进度变亮
        Column()
          .width(300)
          .height(BAND)
          .radialGradient({
            center: [150, 52],
            radius: 150,
            colors: [
              [`rgba(204, 219, 255, ${(0.1 + 0.26 * this.progress + 0.36 * this.flash).toFixed(3)})`, 0],
              ['rgba(204, 219, 255, 0)', 1]
            ]
          })
          .hitTestBehavior(HitTestMode.None)

        ForEach(this.cellIndices(), (k: number) => {
          this.FilmCell(k)
        }, (k: number): string => k.toString())

        this.Gate()
      }
      .width('100%')
      .height(BAND)
      .clip(true)
    }
    .width('100%')
    .padding({ bottom: 10 })
    .alignItems(HorizontalAlign.Center)
    .justifyContent(FlexAlign.End)
    .translate({ x: -(1 - this.reveal) * 120 })
    .opacity(this.reveal)
  }

  /** 一格胶片：片基、上下两排透光的齿孔、中间的画格 */
  @Builder
  FilmCell(k: number) {
    Stack({ alignContent: Alignment.Center }) {
      Column()
        .width('100%')
        .height('100%')
        .backgroundColor('rgba(17, 19, 22, 0.97)')

      Column() {
        this.SprocketRow(k)
        Blank()
        this.SprocketRow(k)
      }
      .width('100%')
      .height('100%')
      .padding({ top: 4, bottom: 4 })

      RemoteImage({ url: this.stillFor(k), placeholderSymbol: $r('sys.symbol.film') })
        .width(FRAME_W)
        .height(FRAME_H)
        .borderRadius(3)
        .clip(true)
    }
    .width(STEP + 8)
    .height(BAND)
    .scale({ x: 1 - 0.16 * this.cellC(k) * this.cellC(k), y: 1 - 0.16 * this.cellC(k) * this.cellC(k) })
    .rotate({ angle: this.cellC(k) * 6.3 })
    .translate({ x: this.cellX(k), y: 18 * this.cellC(k) * this.cellC(k) })
    .zIndex(Math.round((1 - Math.abs(this.cellC(k))) * 100))
    .grayscale(1 - this.cellColor(k))
    .brightness(0.48 + 0.52 * Math.min(1, this.cellLight(k)))
  }

  @Builder
  SprocketRow(k: number) {
    Row({ space: 6 }) {
      ForEach(HOLES, (hole: number) => {
        Column()
          .width(7)
          .height(5)
          .borderRadius(2)
          .backgroundColor(this.holeColor(k))
      }, (hole: number): string => hole.toString())
    }
    .width('100%')
    .justifyContent(FlexAlign.Center)
  }

  /** 片门：两块拉丝钢压片板，过线时合拢 */
  @Builder
  Gate() {
    Stack({ alignContent: Alignment.Center }) {
      Column()
        .width(FRAME_W + 6)
        .height(FRAME_H + 6)
        .border({
          width: 1,
          color: this.armed ? 'rgba(205, 214, 230, 0.85)' : 'rgba(205, 214, 230, 0.25)'
        })
        .borderRadius(4)

      Column()
        .width(6)
        .height(FRAME_H + 22)
        .borderRadius(3)
        .linearGradient({
          angle: 180,
          colors: [
            ['#303337', 0],
            [this.steelLight(), 0.5],
            ['#26292d', 1]
          ]
        })
        .translate({ x: -(FRAME_W / 2 + 9 + (this.armed ? 0 : 14)) })

      Column()
        .width(6)
        .height(FRAME_H + 22)
        .borderRadius(3)
        .linearGradient({
          angle: 180,
          colors: [
            ['#303337', 0],
            [this.steelLight(), 0.5],
            ['#26292d', 1]
          ]
        })
        .translate({ x: FRAME_W / 2 + 9 + (this.armed ? 0 : 14) })

      Column()
        .width(180)
        .height(180)
        .radialGradient({
          center: [90, 90],
          radius: 90,
          colors: [
            [`rgba(255, 255, 255, ${(0.9 * this.flash).toFixed(3)})`, 0],
            ['rgba(255, 255, 255, 0)', 1]
          ]
        })
        .opacity(this.flash)
    }
    .zIndex(200)
    .hitTestBehavior(HitTestMode.None)
  }

  private steelLight(): string {
    return `rgb(${Math.round(48 + 18 * this.progress)}, ${Math.round(51 + 16 * this.progress)}, ` +
      `${Math.round(56 + 14 * this.progress)})`;
  }
}

/** 首页顶部的使用提示卡（docs/design/tips.md `library.reels-floor`，版式同 iOS / Android） */
@Component
export struct ReelsFloorTipCard {
  onClose: () => void = (): void => {
  };

  build() {
    Row({ space: 14 }) {
      SymbolGlyph($r('sys.symbol.arrow_down'))
        .fontSize(24)
        .fontColor([Theme.accent])
        .width(30)
        .height(28)

      Column({ space: 4 }) {
        Text('下拉，开始欣赏片段')
          .fontSize(17)
          .fontWeight(FontWeight.Medium)
          .fontColor(Theme.text)
          .width('100%')
        Text('在这里一直往下拉，胶片走起来、片门合上时松手，就能开始欣赏片段。')
          .fontSize(15)
          .lineHeight(20)
          .fontColor(Theme.textMuted)
          .width('100%')
      }
      .layoutWeight(1)
      .alignItems(HorizontalAlign.Start)

      SymbolGlyph($r('sys.symbol.xmark'))
        .fontSize(16)
        .fontColor([Theme.textFaint])
        .width(28)
        .height(28)
        .accessibilityText('关闭提示')
        .onClick(() => {
          this.onClose();
        })
    }
    .width('100%')
    .padding(16)
    .alignItems(VerticalAlign.Top)
    .backgroundColor('rgba(255, 255, 255, 0.06)')
    .borderRadius(22)
    .border({ width: 1, color: 'rgba(255, 255, 255, 0.12)' })
  }
}
