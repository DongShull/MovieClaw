import { PhonePlaybackHost } from '../playback/PhonePlaybackHost';
import { AppModel } from '../common/AppModel';
import { Theme } from '../theme/AppTheme';
import { DeviceVitals, DeviceVitalsSnapshot } from './DeviceVitals';
import { NetBench, NetBenchResult } from './NetBench';

/**
 * 播放诊断的「设备状况 + 网络测速」两块（对齐 iOS `DeviceVitals` / `NetBench`）。
 * 播放会话判定、执行、供片那几块卡片 PlayerPage 的 StatsSheet 已有，不重复采集——
 * 本组件嵌在它末尾。
 */
@ComponentV2
export struct PlayerDiagnosticsPage {
  @Param host: PhonePlaybackHost = new PhonePlaybackHost();
  @Local deviceLines: string[] = [];
  @Local benchRunning: boolean = false;
  @Local bench: NetBenchResult | null = null;

  aboutToAppear(): void {
    DeviceVitals.snapshot().then((s: DeviceVitalsSnapshot): void => {
      this.deviceLines = s.lines();
    }).catch((): void => {
      this.deviceLines = ['设备信息读取失败'];
    });
  }

  private runBench(): void {
    if (this.benchRunning) {
      return;
    }
    const client = AppModel.shared.client;
    const origin: string = client !== null ? client.origin : '';
    const token: string = AppModel.shared.token ?? '';
    const streamUrl: string = this.host.session?.stream_url ?? '';
    this.benchRunning = true;
    NetBench.run(origin, token, streamUrl, 4000).then((r: NetBenchResult): void => {
      this.bench = r;
      this.benchRunning = false;
    }).catch((): void => {
      this.benchRunning = false;
    });
  }

  build() {
    Column({ space: 12 }) {
      this.DeviceCard()
      this.BenchCard()
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
  }

  @Builder
  Card(title: string, content: () => void) {
    Column({ space: 7 }) {
      Text(title)
        .fontSize(13)
        .fontWeight(FontWeight.Medium)
        .fontColor(Theme.textMuted)
      content()
    }
    .width('100%')
    .alignItems(HorizontalAlign.Start)
    .padding(16)
    .backgroundColor(Theme.surface)
    .borderRadius(Theme.cardRadius)
    .border({ width: 1, color: Theme.line })
  }

  @Builder
  DeviceCard() {
    this.Card('设备状况', (): void => {
      if (this.deviceLines.length === 0) {
        Text('正在读取设备信息…')
          .fontSize(13)
          .fontColor(Theme.textMuted)
          .width('100%')
      }
      ForEach(this.deviceLines, (line: string, index: number) => {
        Text(line)
          .id(`player-diag-device-${index}`)
          .fontSize(13)
          .fontColor(Theme.text)
          .width('100%')
      }, (line: string): string => line)
    })
  }

  @Builder
  BenchCard() {
    this.Card('网络测速', (): void => {
      Text(this.host.session?.stream_url
        ? '对当前服务器的播放流测一次往返延迟和下载吞吐（约 4 秒）'
        : '本机副本播放不经过服务器，无需测速')
        .fontSize(12)
        .fontColor(Theme.textMuted)
        .width('100%')
      Text(this.benchRunning ? '测速中…' : (this.bench === null ? '开始测速' : '再测一次'))
        .fontSize(14)
        .fontWeight(600)
        .fontColor(this.benchRunning ? Theme.textMuted : '#000000')
        .height(38)
        .width('100%')
        .textAlign(TextAlign.Center)
        .backgroundColor(this.benchRunning ? Theme.glassFill : '#ffffff')
        .borderRadius(19)
        .id('player-diag-bench-run')
        .onClick((): void => {
          this.runBench();
        })
      if (this.bench !== null) {
        ForEach(this.bench.lines(), (line: string, index: number) => {
          Text(line)
            .id(`player-diag-bench-${index}`)
            .fontSize(13)
            .fontColor(index === 0 && this.bench?.failed === true ? Theme.danger : Theme.text)
            .width('100%')
        }, (line: string): string => line)
      }
    })
  }
}
