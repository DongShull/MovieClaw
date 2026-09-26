import Foundation
import UIKit
import UniformTypeIdentifiers
import Testing
@testable import MovieClaw

/// AI 会话模块的纯逻辑：轨迹回放、事件归约、技能 token、Markdown 块级解析、媒体卡片参数
@MainActor
struct AgentLogicTests {
    private func decode<T: Decodable>(_ json: String, as type: T.Type = T.self) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    @Test func transcriptReplayBuildsTurnsAndInterruptedFlag() throws {
        let entries: [AgentEntry] = try decode(#"""
        [
          {"type":"message","message_id":"u1","timestamp":"2026-09-11T15:02:41+00:00","thinking_level":"high","model":null,
           "message":{"role":"user","content":[{"type":"text","text":"这个电影怎么样"},{"type":"image","attachment_id":"a1","name":"图.jpg"}]}},
          {"type":"message","message_id":"m2","timestamp":"2026-09-11T15:02:46+00:00",
           "message":{"role":"assistant","content":[{"type":"thinking","text":"想一想"}],"tool_calls":[{"id":"c1","name":"bash","arguments":{"command":"ls  -la"}}]}},
          {"type":"message","message_id":"m3","timestamp":"2026-09-11T15:02:47+00:00","message":{"role":"tool","tool_call_id":"c1","content":"ok"}},
          {"type":"message","message_id":"m4","timestamp":"2026-09-11T15:02:50+00:00","message":{"role":"assistant","content":"答案"}},
          {"type":"compaction","compaction_id":"k","timestamp":"2026-09-11T15:03:00+00:00","summary":"摘要","tokens_before":9000,"tokens_after":800,"replacement_history":[]},
          {"type":"message","message_id":"u2","timestamp":"2026-09-11T15:05:00+00:00","message":{"role":"user","content":"再来"}},
          {"type":"message","message_id":"m5","timestamp":"2026-09-11T15:05:02+00:00","message":{"role":"assistant","content":[],"tool_calls":[{"id":"c2","name":"mclaw","arguments":{"args":"status"}}]}}
        ]
        """#)
        let turns = AgentTimeline.turns(from: entries)
        #expect(turns.count == 2)
        #expect(turns[0].images == [AgentTurnImage(attachmentId: "a1", name: "图.jpg")])
        #expect(turns[0].thinkingLevel == .some("high"))
        #expect(turns[0].interrupted == false)
        #expect(turns[0].answerText == "答案")
        guard case let .process(items) = turns[0].segments.first, case let .tool(tool) = items.last else {
            Issue.record("首段应是处理过程块")
            return
        }
        #expect(tool.output == "ok")
        #expect(tool.summary == "ls -la")
        #expect(AgentTimeline.processSummary(items) == "已思考，执行 1 次命令")
        if case .compaction = turns[0].segments.last {} else { Issue.record("压缩卡片应并入上一轮") }
        // 第二轮没有以无工具调用的正文收尾：已中断
        #expect(turns[1].interrupted)
    }

    @Test func streamEventsReduceIntoTimeline() throws {
        var turn = AgentTurn(id: "t", input: "q", status: .running, startedAt: .now)
        let events: [AgentStreamEvent] = try decode(#"""
        [
          {"type":"thinking_delta","delta":"想"},
          {"type":"tool_call_start","tool_call":{"id":"c1","name":"mclaw"}},
          {"type":"tool_call_delta","tool_call_id":"c1","delta":"{\"args\":"},
          {"type":"tool_call","tool_call":{"id":"c1","name":"mclaw","arguments":{"args":"library list"}}},
          {"type":"tool_result","tool_result":{"tool_call_id":"c1","name":"mclaw","output":"[]","is_error":false,"elapsed_ms":3}},
          {"type":"text_delta","delta":"你好"},
          {"type":"text_delta","delta":"世界"},
          {"type":"agent_done","result":{"elapsed_ms":1234,"steps":2,"usage":{"prompt_tokens":1,"completion_tokens":2}}}
        ]
        """#)
        for event in events { AgentTimeline.apply(event, to: &turn) }
        #expect(turn.status == .done)
        #expect(turn.answerText == "你好世界")
        #expect(turn.result?.elapsedMs == 1234)
        guard case let .process(items) = turn.segments.first, case let .tool(tool) = items.last else {
            Issue.record("首段应是处理过程块")
            return
        }
        #expect(tool.label == #"mclaw({"args":"library list"})"#)
        #expect(tool.argsDone == true)
        #expect(tool.output == "[]")
        var cancelled = AgentTurn(id: "x", input: "q", status: .running, startedAt: .now)
        AgentTimeline.apply(try decode(#"{"type":"agent_cancelled"}"#), to: &cancelled)
        #expect(cancelled.stopped && cancelled.status == .done)
    }

    @Test func skillTokensRoundTrip() {
        let expanded = "<skill name=\"diagnose\" location=\"/x/SKILL.md\">\n正文\n</skill>\n\n帮我看看"
        #expect(AgentSkillText.toTokenForm(expanded) == "/skill:diagnose 帮我看看")
        let parsed = AgentSkillText.parseTokens("/skill:diagnose /skill:typo 帮我看看", allow: ["diagnose"])
        #expect(parsed.names == ["diagnose"])
        #expect(parsed.text == "/skill:typo 帮我看看")
        #expect(AgentSkillText.slashQuery(in: "你好 /dia")?.query == "dia")
        #expect(AgentSkillText.slashQuery(in: "a/b") == nil)
        var draft = AgentDraft()
        draft.load("/skill:diagnose 帮我看看")
        #expect(draft.skills == ["diagnose"] && draft.text == "帮我看看")
        #expect(draft.message == "/skill:diagnose 帮我看看")
    }

    @Test func markdownBlocks() {
        let blocks = AgentMarkdownParser.parse("""
        ## 标题
        第一行
        第二行

        - 项一
          - 嵌套
        - [x] 完成

        | 剧集 | 评分 |
        |---|---|
        | 猎犬 | 8.5 |

        ```bash
        ls -la
        ```
        > 引用
        ---
        """)
        #expect(blocks.count == 7)
        #expect(blocks[0] == .heading(level: 2, text: "标题"))
        #expect(blocks[1] == .paragraph("第一行 第二行"))
        if case let .list(ordered, _, items) = blocks[2] {
            #expect(!ordered && items.count == 2)
            #expect(items[1].checked == true)
            if case .list = items[0].blocks.last {} else { Issue.record("嵌套列表应归入第一项") }
        } else { Issue.record("应解析出列表") }
        #expect(blocks[3] == .table(header: ["剧集", "评分"], rows: [["猎犬", "8.5"]]))
        #expect(blocks[4] == .code(language: "bash", text: "ls -la"))
        #expect(blocks[5] == .quote([.paragraph("引用")]))
        #expect(blocks[6] == .rule)
    }

    @Test func markdownImagesBecomeBlocks() {
        let blocks = AgentMarkdownParser.parse("""
        海报如下：![沙丘](https://image.tmdb.org/t/p/w500/a.jpg "标题") 请查看

        ![](/images/assets/1.jpg)
        """)
        #expect(blocks == [
            .paragraph("海报如下："),
            .image(alt: "沙丘", url: "https://image.tmdb.org/t/p/w500/a.jpg"),
            .paragraph("请查看"),
            .image(alt: "", url: "/images/assets/1.jpg"),
        ])
        #expect(AgentMarkdownParser.parse("普通 [链接](https://a.b) 文本") == [.paragraph("普通 [链接](https://a.b) 文本")])
    }

    @Test func attachmentNamesFollowWeb() throws {
        // 压缩/转码后换 .jpg，没有原名兜底「图片」（同 Web compressImage）
        #expect(AgentImageCompressor.jpegName("IMG_0001.HEIC") == "IMG_0001.jpg")
        #expect(AgentImageCompressor.jpegName("截图.png") == "截图.jpg")
        #expect(AgentImageCompressor.jpegName(nil) == "图片.jpg")
        // 小图原样上传保留原名
        let png = try #require(UIGraphicsImageRenderer(size: CGSize(width: 8, height: 8)).image { _ in }.pngData())
        #expect(try AgentImageCompressor.prepare(png, contentType: .png, filename: "a.png").filename == "a.png")
        #expect(try AgentImageCompressor.prepare(png, contentType: .png).filename == "图片.png")
        #expect(try AgentImageCompressor.prepare(png, contentType: .heic, filename: "IMG_1.HEIC").filename == "IMG_1.jpg")
    }

    @Test func mediaCardArgs() throws {
        let args: AgentJSONObject = try decode(#"{"component":"title","title":"你说的这部","items":[{"title_ref":"tmdb:tv:232766"},{"tmdb_id":5,"media_type":"movie"},{"title_ref":"bad"},{"title_ref":"tmdb:tv:232766"}]}"#)
        let group = try #require(AgentMediaCards.parse(name: "show_media_cards_v1", args: args))
        #expect(group.title == "你说的这部")
        #expect(group.cards == [.title(titleRef: "tmdb:tv:232766"), .title(titleRef: "tmdb:movie:5")])
        #expect(AgentMediaCards.parse(name: "show_media_cards_v2", args: args) == nil)
    }

    // MARK: 消息列拆行（段落级懒加载，见 AgentTranscriptRows）

    private func turn(_ id: String, _ segments: [AgentSegment], status: AgentTurn.Status = .done) -> AgentTurn {
        var turn = AgentTurn(id: id, messageId: id, input: "问\(id)", status: status, startedAt: .now)
        turn.segments = segments
        if status == .done { turn.endedAt = .now }
        return turn
    }

    @Test func transcriptRowsKeepNestedSpacing() {
        let rows = AgentTranscriptLayout.rows(for: turn("t", [
            .process([.thinking("想"), .tool(AgentToolCall(id: "c", name: "bash", label: "bash"))]),
            .text("# 标题\n\n段落一\n\n## 小标题"),
            .compaction(summary: "摘要", tokensBefore: 9000, tokensAfter: 800),
        ]))
        // 提问（与上一轮 32）→ 处理过程（提问到回答 12）→ 标题（段间 10）→ 段落（块间 0.75em）
        // → 小标题（块间 0.75em + 标题前 0.65em）→ 压缩卡片（段间 10）→ 页脚（段间 10）
        #expect(rows.map(\.id) == ["t.q", "t.0p", "t.1b0", "t.1b1", "t.1b2", "t.2z", "t.f"])
        #expect(rows.map(\.spacing) == [32, 12, 10, 12.75, 12.75 + 17 * 0.65, 10, 10])
    }

    @Test func runningTurnRowsCarryCursorAndActiveProcess() {
        let tool = AgentProcessItem.tool(AgentToolCall(id: "c", name: "bash", label: "bash"))
        // 正文是最后一段：光标紧贴正文（间距 0），前面的处理过程不是进行中
        let writing = AgentTranscriptLayout.rows(for: turn("r", [.process([tool]), .text("写到一半")], status: .running))
        #expect(writing.map(\.id) == ["r.q", "r.0p", "r.1b0", "r.1c", "r.f"])
        #expect(writing[1].content == .process(items: [tool], active: false))
        #expect(writing[3].spacing == 0)
        // 处理过程是最后一段：它是进行中的，没有光标；正文还没出现时光标直接跟在上一段后面
        let working = AgentTranscriptLayout.rows(for: turn("w", [.process([tool])], status: .running))
        #expect(working[1].content == .process(items: [tool], active: true))
        #expect(!working.contains { $0.content == .cursor })
        let empty = AgentTranscriptLayout.rows(for: turn("e", [.process([tool]), .text("")], status: .running))
        #expect(empty.map(\.id) == ["e.q", "e.0p", "e.1c", "e.f"])
        #expect(empty[2].spacing == 10)
    }

    @Test func transcriptRowsFollowProcessAndFooterRules() throws {
        let args: AgentJSONObject = try decode(#"{"component":"title","items":[{"title_ref":"tmdb:tv:232766"}]}"#)
        let cards = AgentProcessItem.tool(AgentToolCall(id: "m", name: "show_media_cards_v1", label: "", args: args))
        // 只有卡片绘制调用的处理过程：不出折叠头，卡片组直接常显
        let rows = AgentTranscriptLayout.rows(for: turn("c", [.process([cards]), .text("看看这些")]))
        #expect(rows.map(\.id) == ["c.q", "c.0mm", "c.1b0", "c.f"])
        #expect(rows[1].spacing == 12)
        // 以错误收尾：红色提示，不出页脚
        var failed = turn("x", [], status: .error)
        failed.error = "模型报错"
        #expect(AgentTranscriptLayout.rows(for: failed).map(\.content) == [
            .question(.init(messageId: "x", input: "问x", images: [])), .error("模型报错"),
        ])
    }

    @Test func rowCacheReusesUnchangedTurnsAndZeroesFirstSpacing() {
        let cache = AgentTranscriptRowCache()
        let done = turn("a", [.text("答")])
        var second = turn("b", [.text("第一段")], status: .running)
        let first = cache.rows(turns: [done, second], handoff: nil)
        #expect(first[0].spacing == 0)
        #expect(Set(first.map(\.id)).count == first.count)
        // 流式追加只改第二轮：第一轮的行原样复用，第二轮多出一个段落
        second.segments = [.text("第一段\n\n第二段")]
        let next = cache.rows(turns: [done, second], handoff: nil)
        #expect(Array(next.prefix(3)) == Array(first.prefix(3)))
        #expect(next.contains { $0.id == "b.0b1" })
        // 有续接来源时来源卡片在最前，第一轮与它相距一个轮间距
        let handed = cache.rows(turns: [second], handoff: (sourceId: "s", sourceTitle: "原会话"))
        #expect(handed.map(\.id).prefix(2) == ["handoff", "b.q"])
        #expect(handed[0].spacing == 0 && handed[1].spacing == 32)
    }
}
