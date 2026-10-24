import Foundation
import XCTest
@testable import MovieClaw

nonisolated final class LocalizationTests: XCTestCase {
    func testBundleLanguageNormalization() {
        XCTAssertEqual(AppLanguage.normalize("zh-Hant"), "zh-Hans")
        XCTAssertEqual(AppLanguage.normalize("zh-TW"), "zh-Hans")
        XCTAssertEqual(AppLanguage.normalize("en-GB"), "en")
        XCTAssertEqual(AppLanguage.normalize("ja"), "en")
    }

    func testEnglishFormatting() {
        let locale = Locale(identifier: "en_US")
        XCTAssertEqual(AppFormatting.number(12345, locale: locale), "12,345")
        XCTAssertTrue(AppFormatting.duration(seconds: 4980, locale: locale).contains("hr"))
        let now = Date(timeIntervalSince1970: 1_790_000_000)
        XCTAssertEqual(AppFormatting.relative(now.addingTimeInterval(-180), now: now, locale: locale), "3 minutes ago")
    }

    func testEnglishPluralVariations() {
        XCTAssertEqual(String(localized: .commonInputMaxLength(1)), "Up to 1 character. Shorten the text to continue.")
        XCTAssertEqual(String(localized: .commonInputMaxLength(2)), "Up to 2 characters. Shorten the text to continue.")
    }

    @MainActor func testEnglishPlaybackLabelsAndParameters() {
        XCTAssertEqual(PlayerDisplayMode.fit.label, "Fit")
        XCTAssertEqual(QualityOption.all.first?.label, "Original")
        XCTAssertEqual(LanguageLabel.of("jpn"), "Japanese")
        XCTAssertEqual(PlaybackSpeed.label(1), "Normal")
        XCTAssertEqual(SkipSegments.label(.init(type: "intro", startMs: 0, endMs: 1000, toEnd: false)), "Skip intro")
        XCTAssertEqual(String(localized: .playerTracksChannels(1)), "1 channel")
        XCTAssertEqual(String(localized: .playerTracksChannels(3)), "3 channels")
        XCTAssertEqual(String(localized: .playerDiagnosticsProduced("seg00001", "seg00000", 1)), "Produced through seg00001 · Head seg00000 · 1 segment total")
        XCTAssertEqual(String(localized: .playerDiagnosticsBuffer(1.26)), "Buffered 1.3s")
        XCTAssertEqual(String(localized: .playerSubtitlesSignedOffset("+", 0.5)), "+0.5s")
    }

    func testEnglishNetworkAndValidationErrors() {
        XCTAssertEqual(ServerAddress.ParseError.empty.errorDescription, "Enter a server address")
        let refused = APIClient.networkMessage(URLError(.cannotConnectToHost))
        XCTAssertTrue(refused.contains("refused the connection"))
        XCTAssertTrue(refused.contains("error -1004"))
        let body = APIErrorBody(message: nil, code: nil, details: nil)
        XCTAssertEqual(body.userMessage(status: 503), "Request failed (HTTP 503)")
        let legacy = APIErrorBody(message: "Legacy server message", code: "LEGACY", details: nil)
        XCTAssertEqual(legacy.userMessage(status: 400), "Legacy server message")
    }

    func testCalibrationReferencesDecodeFromOldAndNewServers() throws {
        let legacy = try JSONDecoder().decode(API.CalibrateResultView.self, from: Data(#"{"ok":true,"message":"Legacy calibration"}"#.utf8))
        XCTAssertEqual(legacy.message, "Legacy calibration")
        XCTAssertNil(legacy.messageKey)
        XCTAssertNil(legacy.params)
        let keyed = try JSONDecoder().decode(API.CalibrateResultView.self, from: Data(#"{"ok":true,"message":"Calibrated","message_key":"library.subtitleCalibration.completed","params":{"reference":"Fixture","scale":1,"offset":1}}"#.utf8))
        XCTAssertEqual(keyed.messageKey, "library.subtitleCalibration.completed")
        XCTAssertEqual(keyed.params?["offset"], .int(1))
    }

    func testRuleAndSearchProvenanceRemainsOptionalAndPreservesRawNames() throws {
        let legacyRule = #"{"id":1,"name":"默认规则组","is_default":true,"spec":{},"match_rules":[],"reference_count":0}"#
        let legacy = try JSONDecoder().decode(API.RuleSetView.self, from: Data(legacyRule.utf8))
        XCTAssertEqual(legacy.name, "默认规则组")
        XCTAssertNil(legacy.nameKey)
        let keyedRule = legacyRule.replacingOccurrences(of: #""id":1"#, with: #""id":1,"name_key":"subscription.rules.defaultName""#)
        let keyed = try JSONDecoder().decode(API.RuleSetView.self, from: Data(keyedRule.utf8))
        XCTAssertEqual(keyed.name, legacy.name)
        XCTAssertEqual(keyed.nameKey, "subscription.rules.defaultName")
        let legacyHistory = #"{"id":1,"keyword":"原始片名","vertical":"torrents","label":"电影","categories":["movies"],"site_ids":[],"poster_mode":false,"search_count":1,"last_searched_at":"2026-10-24T00:00:00Z","has_snapshot":true}"#
        let history = try JSONDecoder().decode(API.SearchHistoryItem.self, from: Data(legacyHistory.utf8))
        XCTAssertNil(history.labelKey)
        let keyedHistory = legacyHistory.replacingOccurrences(of: #""id":1"#, with: #""id":1,"label_key":"search.categories.movie""#)
        let translated = try JSONDecoder().decode(API.SearchHistoryItem.self, from: Data(keyedHistory.utf8))
        XCTAssertEqual(translated.keyword, history.keyword)
        XCTAssertEqual(translated.labelKey, "search.categories.movie")
    }

    func testReaderSpecificPreviewsKeepLegacyDecodingAndStructuredQuestions() throws {
        let route = try JSONDecoder().decode(API.DispatchPreviewView.self, from: Data(#"{"mode":"inplace","ok":false,"warning":"Legacy warning"}"#.utf8))
        XCTAssertEqual(route.warning, "Legacy warning")
        XCTAssertNil(route.warningKey)
        XCTAssertNil(route.routeReasonParams)
        let match = try JSONDecoder().decode(API.LibrarySearchMatch.self, from: Data(#"{"type":"alias","source_field":"aliases","matched_name":"原始别名","label":"Name matched","label_key":"search.match.aliasReason","label_params":{"reason":"原始别名"}}"#.utf8))
        XCTAssertEqual(match.matchedName, "原始别名")
        XCTAssertEqual(match.labelParams?["reason"], .string("原始别名"))
        let oldPlan = #"{"configured":true,"profile_revision":1,"memory_revision":2,"scope_options":[],"can_edit":true}"#
        let legacy = try JSONDecoder().decode(API.SmartPlanView.self, from: Data(oldPlan.utf8))
        XCTAssertNil(legacy.subtitleEditQuestion)
        let planWithQuestion = oldPlan.replacingOccurrences(of: #""can_edit":true"#, with: #""can_edit":true,"subtitle_edit_question":{"language":"en","prompt":"Do you understand English?","subtitles_label":"Require Chinese subtitles","understood_label":"I understand English"}"#)
        let next = try JSONDecoder().decode(API.SmartPlanView.self, from: Data(planWithQuestion.utf8))
        XCTAssertEqual(next.profileRevision, legacy.profileRevision)
        XCTAssertEqual(next.subtitleEditQuestion?.language, "en")
        XCTAssertEqual(next.subtitleEditQuestion?.prompt, "Do you understand English?")
    }

    func testNetworkProbeReferencesRemainOptional() throws {
        let legacy = try JSONDecoder().decode(API.NetworkTestResult.self, from: Data(#"{"ok":true,"latency_ms":12,"message":"Legacy probe"}"#.utf8))
        XCTAssertNil(legacy.messageKey)
        XCTAssertNil(legacy.messageParams)
        let keyed = try JSONDecoder().decode(API.NetworkTestResult.self, from: Data(#"{"ok":true,"latency_ms":12,"message":"Connected (HTTP 503)","message_key":"settings.network.responses.http","message_params":{"status":503,"raw":"原始错误"}}"#.utf8))
        XCTAssertEqual(keyed.ok, legacy.ok)
        XCTAssertEqual(keyed.latencyMs, legacy.latencyMs)
        XCTAssertEqual(keyed.messageKey, "settings.network.responses.http")
        XCTAssertEqual(keyed.messageParams?["status"], .int(503))
        XCTAssertEqual(keyed.messageParams?["raw"], .string("原始错误"))
    }

    func testPortResetTargetPreservesLegacyDefault() throws {
        let oldJSON = #"{"external_url":"","web_port":18881,"web_port_source":"setting","web_port_default":3000,"web_port_rejected":null,"web_port_configurable":true}"#
        let legacy = try JSONDecoder().decode(API.AppConfigView.self, from: Data(oldJSON.utf8))
        XCTAssertNil(legacy.webPortResetTarget)
        let nextJSON = oldJSON.replacingOccurrences(of: #""web_port_default":3000"#, with: #""web_port_default":3000,"web_port_reset_target":18880"#)
        let next = try JSONDecoder().decode(API.AppConfigView.self, from: Data(nextJSON.utf8))
        XCTAssertEqual(next.webPortDefault, legacy.webPortDefault)
        XCTAssertEqual(next.webPort, legacy.webPort)
        XCTAssertEqual(next.webPortResetTarget, 18880)
    }

    func testEnglishStorageMessagesPreserveCountsAndOriginalNames() {
        XCTAssertEqual(String(localized: .settingsStorageUiOrphansTitle("原始目录")), "Clean orphan entries in “原始目录”?")
        XCTAssertEqual(String(localized: .settingsStorageUiClearBytes("2 KB")), "Clear and free 2 KB")
        XCTAssertEqual(String(localized: .settingsStorageCleaned("2 KB", "1")), "Freed 2 KB; entries removed: 1")
        XCTAssertEqual(String(localized: .settingsStorageCleanedSkipped("0 B", "0", "1")), "Freed 0 B; entries removed: 0; entries skipped while in use: 1")
        XCTAssertEqual(String(localized: .settingsStorageComputedUpdating("a few seconds ago")), "Calculated a few seconds ago · Updating")
    }

    @MainActor func testEnglishScheduleMessagesAndRawCronValues() {
        func task(_ seconds: Int, cron: String? = nil) -> API.ScheduledTaskView {
            .init(key: "raw", title: "Raw task", description: "Raw description", enabled: true,
                  triggerType: cron == nil ? "interval" : "cron",
                  intervalSeconds: cron == nil ? seconds : nil, cronExpr: cron)
        }
        XCTAssertEqual(SettingsSchedule.describe(task(1)), "Every 1 second")
        XCTAssertEqual(SettingsSchedule.describe(task(2)), "Every 2 seconds")
        XCTAssertEqual(SettingsSchedule.describe(task(60)), "Every 1 minute")
        XCTAssertEqual(SettingsSchedule.describe(task(120)), "Every 2 minutes")
        XCTAssertEqual(SettingsSchedule.describe(task(3600)), "Every 1 hour")
        XCTAssertEqual(SettingsSchedule.describe(task(7200)), "Every 2 hours")
        XCTAssertEqual(SettingsSchedule.describe(task(3_600_000)), "Every 1,000 hours")
        XCTAssertEqual(SettingsSchedule.describe(task(0)), "Interval not set")
        let daily = task(0, cron: "5 4 * * *")
        XCTAssertEqual(SettingsSchedule.describe(daily), "Daily at 04:05")
        XCTAssertEqual(daily.cronExpr, "5 4 * * *")
        XCTAssertEqual(SettingsSchedule.describe(task(0, cron: "0 4 * * 1")), "cron: 0 4 * * 1")
    }

}
