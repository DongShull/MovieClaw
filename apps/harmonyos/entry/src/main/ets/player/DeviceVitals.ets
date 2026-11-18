import { deviceInfo } from '@kit.BasicServicesKit';
import { connection } from '@kit.NetworkKit';
import { hidebug } from '@kit.PerformanceAnalysisKit';

/**
 * 设备体征读数（对齐 iOS `DeviceVitals.swift` 的信息组织）：
 * 型号 / 系统版本与 API 级别 / 本机堆内存 / 网络类型与下行带宽。
 *
 * 口径说明：鸿蒙侧没有 iOS phys_footprint 的等价读数，这里用 hidebug 的
 * 本机堆（native heap）占用作内存口径，够诊断「越播越卡是不是内存问题」用。
 */

export class DeviceVitalsSnapshot {
  model: string = '';
  osVersion: string = '';
  apiLevel: number = 0;
  /** 本机堆已用 / 总量（MB） */
  heapUsedMb: number = 0;
  heapTotalMb: number = 0;
  netType: string = '未知';
  /** 系统上报的下行链路带宽（kbps，信号状态的粗读数） */
  netDownKbps: number = 0;

  /** 界面逐行展示的读数 */
  lines(): string[] {
    const net: string = this.netDownKbps > 0
      ? `网络：${this.netType} · 下行约 ${this.netDownKbps} kbps`
      : `网络：${this.netType}`;
    return [
      `设备：${this.model} · HarmonyOS ${this.osVersion}（API ${this.apiLevel}）`,
      `内存：本机堆 ${Math.round(this.heapUsedMb)} MB / ${Math.round(this.heapTotalMb)} MB`,
      net
    ];
  }
}

export class DeviceVitals {
  /** 纯函数：按有无 Wi-Fi / 有线 / 蜂窝拼网络类型文案（单测覆盖） */
  static labelFromFlags(wifi: boolean, ethernet: boolean, cellular: boolean): string {
    const parts: string[] = [];
    if (wifi) {
      parts.push('Wi-Fi');
    }
    if (ethernet) {
      parts.push('有线');
    }
    if (cellular) {
      parts.push('蜂窝');
    }
    return parts.length > 0 ? parts.join(' · ') : '无网络';
  }

  /** 采一份设备读数；网络读不到（飞行模式等）时置「无网络」，不抛错 */
  static async snapshot(): Promise<DeviceVitalsSnapshot> {
    const s: DeviceVitalsSnapshot = new DeviceVitalsSnapshot();
    s.model = deviceInfo.productModel;
    s.osVersion = deviceInfo.osFullName;
    s.apiLevel = deviceInfo.sdkApiVersion;
    s.heapUsedMb = hidebug.getNativeHeapAllocatedSize() / 1048576;
    s.heapTotalMb = hidebug.getNativeHeapSize() / 1048576;
    try {
      const handle: connection.NetHandle = connection.getDefaultNetSync();
      const caps: connection.NetCapabilities = connection.getNetCapabilitiesSync(handle);
      const bearers: connection.NetBearType[] = caps.bearerTypes;
      s.netType = DeviceVitals.labelFromFlags(
        bearers.includes(connection.NetBearType.BEARER_WIFI),
        bearers.includes(connection.NetBearType.BEARER_ETHERNET),
        bearers.includes(connection.NetBearType.BEARER_CELLULAR));
      s.netDownKbps = caps.linkDownstreamBandwidthKbps;
    } catch (error) {
      s.netType = '无网络';
    }
    return s;
  }
}
