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
}
