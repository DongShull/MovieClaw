import XCTest

final class LocalizationUITests: XCTestCase {
    @MainActor private lazy var app = XCUIApplication()

    override func setUpWithError() throws {
        let env = ProcessInfo.processInfo.environment
        guard env["MC_TEST_I18N"] == "1", let server = env["MC_TEST_SERVER"],
              URL(string: server)?.host == "127.0.0.1" else {
            throw XCTSkip("Requires the isolated i18n fixture")
        }
        continueAfterFailure = false
    }

    @MainActor private func launch(_ language: String) {
        let env = ProcessInfo.processInfo.environment
        app.launchArguments = ["--ui-testing", "-mcServer", env["MC_TEST_SERVER"]!,
                               "-mcUser", "admin", "-mcPass", env["MC_TEST_PASSWORD"]!,
                               "-mcRoute", "/settings", "-AppleLanguages", "(\(language))",
                               "-AppleLocale", "en_US"]
        app.launch()
    }

    @MainActor func testEnglishLanguageRowOpensSystemSettings() {
        launch("en")
        let row = app.buttons["app-language-settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 30))
        XCTAssertTrue(row.label.contains("Language"))
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "settings-language-en"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        row.tap()
        XCTAssertTrue(XCUIApplication(bundleIdentifier: "com.apple.Preferences").wait(for: .runningForeground, timeout: 10))
    }

    @MainActor func testUnsupportedLanguageFallsBackToEnglish() {
        launch("ja")
        let row = app.buttons["app-language-settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 30))
        XCTAssertTrue(row.label.contains("Language"))
    }

    @MainActor func testTraditionalChineseUsesSimplifiedAlias() {
        launch("zh-Hant")
        let row = app.buttons["app-language-settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 30))
        XCTAssertTrue(row.label.contains("语言"))
        XCTAssertTrue(row.label.contains("简体中文"))
    }
}
