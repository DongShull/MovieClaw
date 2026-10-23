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
        let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
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

    @MainActor func testFreshSignInUsesEnglishAndChineseMessages() {
        let env = ProcessInfo.processInfo.environment
        for (language, connect, wrongPassword) in [
            ("en", "Connect to server", "Incorrect username or password"),
            ("zh-Hans", "连接服务器", "用户名或密码错误"),
        ] {
            app.launchArguments = ["--reset-state", "--ui-testing", "-AppleLanguages", "(\(language))",
                                   "-AppleLocale", "en_US"]
            app.launch()
            let start = app.buttons["welcome-start"]
            XCTAssertTrue(start.waitForExistence(timeout: 20))
            XCTAssertEqual(start.label, connect)
            start.tap()
            let address = app.textFields["server-address"]
            XCTAssertTrue(address.waitForExistence(timeout: 10))
            address.tap()
            address.typeText(env["MC_TEST_SERVER"]!)
            let username = app.textFields["login-username"]
            username.tap()
            username.typeText("admin")
            let password = app.secureTextFields["login-password"]
            password.tap()
            password.typeText("wrong-password")
            app.buttons["login-submit"].tap()
            let error = app.staticTexts["login-error"]
            XCTAssertTrue(error.waitForExistence(timeout: 15))
            XCTAssertEqual(error.label, wrongPassword)
            XCTAssertTrue(app.buttons["login-submit"].isEnabled)
            XCTAssertEqual(address.value as? String, env["MC_TEST_SERVER"])
            let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            screenshot.name = "sign-in-error-\(language)"
            screenshot.lifetime = .keepAlways
            add(screenshot)
            app.terminate()
        }
    }

    @MainActor func testPlaybackMenusUseTheEffectiveLanguage() throws {
        let env = ProcessInfo.processInfo.environment
        let item = try XCTUnwrap(env["MC_TEST_PLAYER_ITEM"])
        for (language, paused, quality, subtitles, diagnostics) in [
            ("en", "Pause", "Quality", "Subtitles", "Playback diagnostics"),
            ("zh-Hans", "暂停", "画质", "字幕", "播放诊断"),
        ] {
            app.launchArguments = ["--reset-state", "--ui-testing", "-mcServer", env["MC_TEST_SERVER"]!,
                                   "-mcUser", "admin", "-mcPass", env["MC_TEST_PASSWORD"]!,
                                   "-mcRoute", "/play/\(item)/s00e00?t=0", "-mcPlayerDiagnostics", "YES",
                                   "-mcPlayerPinChrome", "YES", "-AppleLanguages", "(\(language))",
                                   "-AppleLocale", "en_US"]
            app.launch()
            XCTAssertTrue(app.otherElements["player-screen"].waitForExistence(timeout: 30))
            let time = app.staticTexts["player-time"]
            let advanced = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in
                Int(time.value as? String ?? "0").map { $0 >= 2 } ?? false
            }, object: time)
            XCTAssertEqual(XCTWaiter.wait(for: [advanced], timeout: 60), .completed)
            let pause = app.buttons["player-play-pause"]
            XCTAssertEqual(pause.label, paused)
            pause.tap()
            XCTAssertTrue(app.staticTexts[diagnostics].exists)
            if language == "en" {
                let panel = app.otherElements["player-diagnostics"]
                let text = panel.staticTexts.allElementsBoundByIndex.map(\.label).joined()
                XCTAssertNil(text.range(of: "[\\u3400-\\u9fff]", options: .regularExpression), text)
            }
            let shot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            shot.name = "playback-diagnostics-\(language)"
            shot.lifetime = .keepAlways
            add(shot)
            app.buttons["player-设置"].tap()
            XCTAssertTrue(app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", quality)).firstMatch.waitForExistence(timeout: 5))
            app.coordinate(withNormalizedOffset: CGVector(dx: 0.8, dy: 0.35)).tap()
            app.buttons["player-字幕"].tap()
            XCTAssertTrue(app.staticTexts[subtitles].waitForExistence(timeout: 5))
            XCTAssertEqual(app.buttons["subtitle-off"].label, language == "en" ? "Off" : "关闭")
            app.buttons["subtitle-off"].tap()
            app.terminate()
        }
    }

}
