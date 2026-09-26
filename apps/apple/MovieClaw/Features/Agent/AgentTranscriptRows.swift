import SwiftUI

// 会话消息列的「行」：懒加载列表（LazyVStack）的最小单位。
//
// 为什么不按「一轮」懒加载：一轮对话常有几千 pt 高（长回答、表格、代码），懒加载只能整行进行，
// 进页面定位到底部时要把最后一两轮整块构建、排版——模拟器实测主线程一口气冻住 0.4～0.7 秒，
// 录屏里这段时间一帧都没有（2026-09-27，4 个最长会话点开到看到字 0.8～1.3 秒，约 75% 耗在这里）。
// 拆成段落级的行（提问气泡 / 处理过程 / 卡片组 / 每个 Markdown 块 / 页脚）之后，屏幕外的段落
// 既不构建也不排版，进页面只处理最底下的一屏。
//
// 行由轮次派生、按轮缓存（`AgentTranscriptRowCache`）：流式输出时只有正在生成的那一轮重新拆行，
// 历史轮次原样复用；每行的视图是 Equatable 的，没变的行不重绘。
// 视觉间距与原来「轮 → 段 → Markdown 块」的嵌套 VStack 完全一致（轮与轮之间 32、提问到回答 12、
// 段与段 10、段内 Markdown 块之间 0.75em、标题前再多 0.65em），摊到每行的 `spacing` 上。

/// 消息列的一行
struct AgentTranscriptRow: Identifiable, Equatable {
    /// 提问气泡要的字段（不整轮携带：流式时这一轮在变，气泡本身没变，不该跟着重绘）
    struct Question: Equatable {
        var messageId: String?
        var input: String
        var images: [AgentTurnImage]
    }

    enum Content: Equatable {
        /// 从旧会话续开的来源卡片（整列第一行）
        case handoff(sourceId: String, sourceTitle: String?)
        case question(Question)
        case process(items: [AgentProcessItem], active: Bool)
        /// 生成式 UI 的卡片组，紧跟在所属处理过程块之后常显
        case mediaCards(AgentMediaCardGroup)
        case markdown(AgentMarkdownBlock)
        /// 正在生成的正文末尾的呼吸光标
        case cursor
        case compaction(summary: String, tokensBefore: Int?, tokensAfter: Int?)
        case error(String)
        case footer(AgentTurn)
    }

    let id: String
    let content: Content
    /// 与上一行之间的间距
    var spacing: CGFloat

    var isQuestion: Bool {
        if case .question = content { return true }
        return false
    }
}

/// 轮次 → 行的拆分规则（纯函数，单元测试见 AgentLogicTests）
enum AgentTranscriptLayout {
    /// 回答正文字号（同 AgentMarkdownView 的阅读档）
    static let textSize: CGFloat = 17
    static let turnSpacing: CGFloat = 32
    static let answerSpacing: CGFloat = 12
    static let segmentSpacing: CGFloat = 10

    /// 一轮拆成行。首行（提问气泡）的间距按「与上一轮之间」给，整列第一行由调用方清零。
    /// 行 id = 轮 id + 段序号 + 块序号：流式追加时已有的行 id 不变，只有末尾的行内容在变。
    static func rows(for turn: AgentTurn) -> [AgentTranscriptRow] {
        var rows = [AgentTranscriptRow(
            id: "\(turn.id).q",
            content: .question(.init(messageId: turn.messageId, input: turn.input, images: turn.images)),
            spacing: turnSpacing
        )]
        var gap = answerSpacing
        func add(_ suffix: String, _ content: AgentTranscriptRow.Content) {
            rows.append(AgentTranscriptRow(id: "\(turn.id).\(suffix)", content: content, spacing: gap))
            gap = segmentSpacing
        }

        for (index, segment) in turn.segments.enumerated() {
            let active = turn.isRunning && index == turn.segments.count - 1
            switch segment {
            case let .process(items):
                if !AgentProcessBlock.visibleItems(items).isEmpty {
                    add("\(index)p", .process(items: items, active: active))
                }
                for entry in AgentMediaCards.groups(in: items) {
                    add("\(index)m\(entry.id)", .mediaCards(entry.group))
                }
            case let .text(text):
                let blocks = AgentMarkdownParser.parse(text)
                for (b, block) in blocks.enumerated() {
                    if b > 0 { gap = textSize * 0.75 + (block.isHeading ? textSize * 0.65 : 0) }
                    add("\(index)b\(b)", .markdown(block))
                }
                if active {
                    // 光标紧贴正文（原版与正文同处一个 spacing 0 的 VStack，自带 4pt 上边距）
                    if !blocks.isEmpty { gap = 0 }
                    add("\(index)c", .cursor)
                }
            case let .compaction(summary, before, after):
                add("\(index)z", .compaction(summary: summary, tokensBefore: before, tokensAfter: after))
            }
        }

        if turn.status == .error, let error = turn.error {
            add("e", .error(error))
        }
        if AgentTurnFooter.hasContent(turn) {
            add("f", .footer(turn))
        }
        return rows
    }
}

/// 按轮缓存拆好的行：轮次没变就原样复用，流式时只有正在生成的那一轮重新拆
final class AgentTranscriptRowCache {
    private var cache: [String: (turn: AgentTurn, rows: [AgentTranscriptRow])] = [:]

    func rows(turns: [AgentTurn], handoff: (sourceId: String, sourceTitle: String?)?) -> [AgentTranscriptRow] {
        var result: [AgentTranscriptRow] = []
        if let handoff {
            result.append(AgentTranscriptRow(id: "handoff", content: .handoff(sourceId: handoff.sourceId, sourceTitle: handoff.sourceTitle), spacing: 0))
        }
        for turn in turns {
            if let hit = cache[turn.id], hit.turn == turn {
                result.append(contentsOf: hit.rows)
            } else {
                let rows = AgentTranscriptLayout.rows(for: turn)
                cache[turn.id] = (turn, rows)
                result.append(contentsOf: rows)
            }
        }
        // 改写重问会整段替换后面的轮次：清掉已不存在的轮，免得缓存越积越多
        if cache.count > turns.count {
            let alive = Set(turns.map(\.id))
            cache = cache.filter { alive.contains($0.key) }
        }
        if !result.isEmpty { result[0].spacing = 0 }
        return result
    }
}

/// 一行的视图。Equatable：没变的行跳过重绘（流式时只有末尾几行在变）
struct AgentTranscriptRowView: View, Equatable {
    let row: AgentTranscriptRow
    let sessionId: String
    /// 已知技能名（小写）；nil = 名单未就绪。只有提问气泡用得到
    let knownSkills: Set<String>?
    /// 改写重问（messageId, 原文）；nil = 运行中不给入口。只有提问气泡用得到
    let onEdit: ((String, String) -> Void)?

    static func == (lhs: Self, rhs: Self) -> Bool {
        guard lhs.row == rhs.row, lhs.sessionId == rhs.sessionId else { return false }
        // 技能名单晚到、运行状态切换只影响提问气泡，其余行不跟着整列重绘
        guard lhs.row.isQuestion else { return true }
        return lhs.knownSkills == rhs.knownSkills && (lhs.onEdit == nil) == (rhs.onEdit == nil)
    }

    var body: some View {
        switch row.content {
        case let .handoff(sourceId, sourceTitle):
            AgentHandoffCard(sourceId: sourceId, sourceTitle: sourceTitle)
        case let .question(question):
            AgentUserBubble(
                text: question.input,
                images: question.images,
                sessionId: sessionId,
                knownSkills: knownSkills,
                onEdit: onEdit.flatMap { edit in question.messageId.map { id in { edit(id, question.input) } } }
            )
        case let .process(items, active):
            AgentProcessBlock(items: items, active: active)
        case let .mediaCards(group):
            AgentMediaCardsBlock(group: group)
        case let .markdown(block):
            AgentMarkdownBlockView(block: block, size: AgentTranscriptLayout.textSize)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .cursor:
            AgentStreamingCursor()
        case let .compaction(summary, before, after):
            AgentCompactionCard(summary: summary, tokensBefore: before, tokensAfter: after)
        case let .error(message):
            AgentTurnErrorView(message: message)
        case let .footer(turn):
            AgentTurnFooter(turn: turn)
        }
    }
}
