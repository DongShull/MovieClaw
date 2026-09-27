import SwiftUI

// 大图（Hero）与详情页头里的操作键统一尺寸：订阅首页的「播放 / 查看订阅」、发现页的「订阅影片」、
// 发现详情的「订阅追踪 / 搜索资源」、影片页的「播放 / 收藏 / 标为已看」（2026-09-27 用户要求统一、改小）。
//
// 尺寸怎么定的：
// - 高度取系统 regular 档（iPhone 与 iPad 实测都是 34pt；small 28、large 50，extraLarge 在手机上同 large）。
//   标杆 Apple TV 首页大图的 Play 用的是 large 档 50pt，约占它大图高度的 7%；我们的大图 480～520pt，
//   按同一比例是 35pt，正好落在 regular 档。原先 46 / 48 / 60pt 占到大图的 9%～12%，压过片名和信息。
// - 可点区域：HIG 默认 44×44、最小 28×28，regular 档就是系统自己的默认按钮，满足要求。
// - 并排的按钮等高、主次靠样式区分（HIG「Use style — not size」）：主键实色（白 / 强调色），次键玻璃。
// - 不写死高度也不改字号：交给系统按钮样式与 Dynamic Type，用户调大字号时按钮跟着长高。
// - 不随屏宽缩放：系统控件在 SE 到 Pro Max 到 iPad 都是同一个 pt 值，适配靠排版而不是放大按钮。
// - 单独一颗的主键给最小宽度 120pt（Apple TV 的 50×175 约 3.5:1，换到 34pt 高约 120），
//   轮播换片时「播放 / 继续播放 / 查看订阅」宽度不跳，也不会缩成一颗短胖的胶囊。
// - 通栏的键（影片页播放）封顶 408pt：最大号 iPhone 440pt 减两侧 16pt 页边距，手机上照旧通栏；
//   iPad 上不再拉成 800pt 宽、34pt 高的细长条（大屏限制按钮宽度，同 HIG / Material 的做法）。

enum HeroAction {
    /// 按钮最小总宽（含系统 regular 档左右各 12pt 内边距）
    static let minWidth: CGFloat = 120
    /// 通栏按钮的最大宽度
    static let maxWideWidth: CGFloat = 408
    /// 系统 regular 档的单侧水平内边距（实测），标签最小宽度 = 总宽 - 两侧内边距
    private static let horizontalPadding: CGFloat = 12
    static var minLabelWidth: CGFloat { minWidth - horizontalPadding * 2 }
    /// 通栏按钮标签的最大宽度（按钮总宽封顶 `maxWideWidth`）
    static var maxWideLabelWidth: CGFloat { maxWideWidth - horizontalPadding * 2 }
}

extension View {
    /// 操作键的标签：半粗（压在剧照上更清楚），并撑到最小宽度。按钮样式与 `.controlSize(.regular)` 由调用处给
    func heroActionLabel() -> some View {
        fontWeight(.semibold)
            .frame(minWidth: HeroAction.minLabelWidth)
    }
}
