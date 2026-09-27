import CoreText
import SwiftUI

/// 欢迎页轮播的一句台词：都出自仰望星空的科幻 / 太空电影，与深空背景（`CosmosBackdrop`）呼应。
///
/// 为什么不用真实海报/剧照：首次打开时还没连上服务器，拿不到用户自己的片库；
/// 把别人的海报打进开源安装包又有版权问题。短句台词注明出处引用即可。
struct WelcomeScene: Identifiable {
    let id: Int
    /// 中文字幕
    let line: String
    /// 外语原句（华语片为空）
    let original: String?
    let film: String
    let year: Int
}

extension WelcomeScene {
    /// 片单与台词：都是仰望星空的电影，《星际穿越》开场也收尾。
    static let all: [WelcomeScene] = [
        WelcomeScene(
            id: 0,
            line: "我们曾经仰望星空，\n思考自己在星辰间的位置。",
            original: "We used to look up at the sky and wonder\nat our place in the stars.",
            film: "星际穿越", year: 2014
        ),
        WelcomeScene(
            id: 1,
            line: "如果宇宙中只有我们，\n那真是太浪费空间了。",
            original: "If it's just us, it seems like an awful waste of space.",
            film: "超时空接触", year: 1997
        ),
        WelcomeScene(
            id: 2,
            line: "所有这些时刻，终将消逝在时光中，\n一如雨中的泪水。",
            original: "All those moments will be lost in time,\nlike tears in rain.",
            film: "银翼杀手", year: 1982
        ),
        WelcomeScene(
            id: 3,
            line: "不管最终结果将人类历史导向何处，\n我们决定，选择希望。",
            original: nil,
            film: "流浪地球", year: 2019
        ),
        WelcomeScene(
            id: 4,
            line: "抱歉，戴夫，恐怕我做不到。",
            original: "I'm sorry, Dave. I'm afraid I can't do that.",
            film: "2001太空漫游", year: 1968
        ),
        WelcomeScene(
            id: 5,
            line: "如果你能从头到尾看清自己的一生，\n你会改变什么吗？",
            original: "If you could see your whole life from start to finish,\nwould you change things?",
            film: "降临", year: 2016
        ),
        WelcomeScene(
            id: 6,
            line: "愿原力与你同在。",
            original: "May the Force be with you.",
            film: "星球大战", year: 1977
        ),
        WelcomeScene(
            id: 7,
            line: "爱是唯一可以超越时间与空间的事物。",
            original: "Love is the one thing we're capable of perceiving\nthat transcends dimensions of time and space.",
            film: "星际穿越", year: 2014
        ),
    ]
}

extension Font {
    /// 欢迎页的宋体：思源宋体（Noto Serif SC）子集，只含欢迎页用到的字，
    /// 生成方法见 `scripts/subset-welcome-font.py`；子集外的字自动回落到系统字体。
    static func welcomeSerif(size: CGFloat) -> Font {
        _ = WelcomeFont.registered
        return .custom(WelcomeFont.postScriptName, size: size)
    }
}

/// 字体不走 Info.plist 的 UIAppFonts：只有欢迎页用，首次取用时再注册进当前进程即可
private enum WelcomeFont {
    static let postScriptName = "MovieClawWelcomeSerif-Regular"
    static let registered: Bool = {
        guard let url = Bundle.main.url(forResource: "WelcomeSerif", withExtension: "otf") else { return false }
        return CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
    }()
}
