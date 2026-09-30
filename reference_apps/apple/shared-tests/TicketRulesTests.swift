import XCTest
#if os(iOS)
@testable import TicketIOS
#else
@testable import TicketMac
#endif
final class TicketRulesTests: XCTestCase {
    func testRequiredTitle() { XCTAssertFalse(validTicketTitle("  ")); XCTAssertTrue(validTicketTitle("valid")) }
    func testLengthLimit() { XCTAssertTrue(validTicketTitle(String(repeating: "x", count: 120))); XCTAssertFalse(validTicketTitle(String(repeating: "x", count: 121))) }
}
