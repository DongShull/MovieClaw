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
