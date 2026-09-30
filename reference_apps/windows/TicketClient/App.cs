using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;

namespace AgentFlow.Reference;
public static class TicketRules {
    public static bool ValidTitle(string? value) => value is not null && value.Trim().Length > 0 && value.Trim().EnumerateRunes().Count() <= 120;
}
public class TicketWindow: Window {
    readonly TextBox title = new(); readonly ListBox list = new(); readonly TextBlock error = new();
    readonly HttpClient client = new() { Timeout = TimeSpan.FromSeconds(8) };
    readonly string endpoint = Environment.GetEnvironmentVariable("AGENTFLOW_API_URL") ?? "http://127.0.0.1:8765";
    public TicketWindow() {
        Title="AgentFlow Tickets";Width=720;Height=520;
        var content=new StackPanel { Margin=new Thickness(24) };Content=content;
        content.Children.Add(new TextBlock { Text="AgentFlow Tickets", FontSize=24 });
        AutomationProperties.SetAutomationId(title,"ticket-title");content.Children.Add(title);
        var create=new Button { Content="Create ticket", Margin=new Thickness(0,10,0,10) };AutomationProperties.SetAutomationId(create,"create-ticket");create.Click+=async(_,_)=>await Create();content.Children.Add(create);
        var refresh=new Button { Content="Refresh" };AutomationProperties.SetAutomationId(refresh,"refresh-tickets");refresh.Click+=async(_,_)=>await Refresh();content.Children.Add(refresh);
        AutomationProperties.SetAutomationId(error,"error-message");content.Children.Add(error);AutomationProperties.SetAutomationId(list,"ticket-list");content.Children.Add(list);
        client.DefaultRequestHeaders.Authorization=new("Bearer","reference.manager");Loaded+=async(_,_)=>await Refresh();Closed+=(_,_)=>client.Dispose();
    }
    async Task Refresh() {
        try {var response=await client.GetAsync(endpoint+"/api/tickets");response.EnsureSuccessStatusCode();using var data=JsonDocument.Parse(await response.Content.ReadAsStringAsync());list.Items.Clear();foreach(var ticket in data.RootElement.GetProperty("tickets").EnumerateArray())list.Items.Add(ticket.GetProperty("title").GetString());error.Text="";}
        catch(Exception e){error.Text=e.Message;}
    }
    async Task Create() {
        if(!TicketRules.ValidTitle(title.Text)){error.Text="invalid_title";return;}
        try {var response=await client.PostAsync(endpoint+"/api/tickets",new StringContent(JsonSerializer.Serialize(new { title=title.Text }),Encoding.UTF8,"application/json"));response.EnsureSuccessStatusCode();title.Clear();await Refresh();}
        catch(Exception e){error.Text=e.Message;}
    }
}
public static class Program { [STAThread] public static void Main() => new Application().Run(new TicketWindow()); }
