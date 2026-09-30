using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;
using NUnit.Framework;
using System.Diagnostics;
namespace AgentFlow.Reference.Tests;
[TestFixture] public class GuiTests {
    Application? app;
    UIA3Automation? automation;
    [TearDown] public void Cleanup(){app?.Close();app?.Dispose();automation?.Dispose();}
    [Test] public void CreateAndReopen(){
        var endpoint=Environment.GetEnvironmentVariable("AGENTFLOW_API_URL");
        var executable=Environment.GetEnvironmentVariable("AGENTFLOW_APP_PATH");
        Assert.That(endpoint,Is.Not.Null.And.Not.Empty,"A real API is required, not a skipped test");
        Assert.That(executable,Is.Not.Null.And.Not.Empty,"Exact frozen executable path required");
        automation=new UIA3Automation();app=Application.Launch(executable!);
        var window=app.GetMainWindow(automation,TimeSpan.FromSeconds(15));
        Assert.That(window,Is.Not.Null);
        var title="windows-"+Guid.NewGuid().ToString("N");
        Wait(()=>window.FindFirstDescendant(cf=>cf.ByAutomationId("ticket-title"))).AsTextBox().Enter(title);
        Wait(()=>window.FindFirstDescendant(cf=>cf.ByAutomationId("create-ticket"))).AsButton().Invoke();
        Wait(()=>window.FindFirstDescendant(cf=>cf.ByName(title)));
        app.Close();app.Dispose();app=Application.Launch(executable!);window=app.GetMainWindow(automation,TimeSpan.FromSeconds(15));
        Wait(()=>window.FindFirstDescendant(cf=>cf.ByName(title)));
    }
    static AutomationElement Wait(Func<AutomationElement> query){
        var watch=Stopwatch.StartNew();
        while(watch.Elapsed<TimeSpan.FromSeconds(15)){try{var value=query();if(value!=null)return value;}catch{}Thread.Sleep(100);}
        throw new AssertionException("Native control or persisted state was not observed");
    }
}
