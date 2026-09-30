package local.agentflow.reference
import org.junit.Assert.*
import org.junit.Test
class TicketRulesTest {
    @Test fun requiredTitle() { assertFalse(TicketRules.validTitle("  "));assertTrue(TicketRules.validTitle("valid")) }
    @Test fun lengthBoundary() { assertTrue(TicketRules.validTitle("x".repeat(120)));assertFalse(TicketRules.validTitle("x".repeat(121))) }
}
