import XCTest
final class TicketUITests: XCTestCase {
    private var app: XCUIApplication!
    override func tearDown() { app?.terminate(); super.tearDown() }
    func testCreateAndReopen() {
        guard let endpoint = ProcessInfo.processInfo.environment["AGENTFLOW_API_URL"], !endpoint.isEmpty else {
            XCTFail("A real reference API endpoint is required; this is not a skipped test"); return
        }
        app = XCUIApplication()
        app.launchEnvironment["AGENTFLOW_API_URL"] = endpoint
        app.launch()
        let field = app.textFields["ticket-title"]
        XCTAssertTrue(field.waitForExistence(timeout: 15))
        let title = "native-" + UUID().uuidString
        field.clickOrTap()
        field.typeText(title)
        app.buttons["create-ticket"].clickOrTap()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: 15))
        #if os(iOS)
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: 10))
        #endif
        app.terminate()
        app.launch()
        XCTAssertTrue(app.staticTexts[title].waitForExistence(timeout: 15))
    }
}
extension XCUIElement {
    func clickOrTap() {
        #if os(macOS)
        click()
        #else
        tap()
        #endif
    }
}
