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

}
