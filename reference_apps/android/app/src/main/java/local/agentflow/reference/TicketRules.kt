package local.agentflow.reference
object TicketRules {
    fun validTitle(value: String): Boolean = value.trim().let { it.isNotEmpty() && it.codePointCount(0, it.length) <= 120 }
}
