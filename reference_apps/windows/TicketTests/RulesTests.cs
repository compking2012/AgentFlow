using AgentFlow.Reference;
using NUnit.Framework;
namespace AgentFlow.Reference.Tests;
[TestFixture] public class RulesTests {
    [Test] public void RequiredTitle() { Assert.That(TicketRules.ValidTitle("  "),Is.False);Assert.That(TicketRules.ValidTitle("valid"),Is.True); }
    [Test] public void LengthBoundary() { Assert.That(TicketRules.ValidTitle(new string('x',120)),Is.True);Assert.That(TicketRules.ValidTitle(new string('x',121)),Is.False); }
}
