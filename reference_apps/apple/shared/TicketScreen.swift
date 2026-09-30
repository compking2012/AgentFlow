import SwiftUI
import Foundation

struct Ticket: Codable, Identifiable {
    let id: Int
    let title: String
    let owner: String
    let assignee: String?
    let status: String
}
struct TicketList: Codable { let tickets: [Ticket] }
@MainActor final class TicketModel: ObservableObject {
    @Published var title = ""
    @Published var tickets: [Ticket] = []
    @Published var error = ""
    let base = ProcessInfo.processInfo.environment["AGENTFLOW_API_URL"] ?? "http://127.0.0.1:8765"
    let role = ProcessInfo.processInfo.environment["AGENTFLOW_ROLE"] ?? "manager"
    func call(_ path: String, body: [String: String]? = nil) async throws -> Data {
        guard let url = URL(string: base + path) else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        request.setValue("Bearer reference." + role, forHTTPHeaderField: "Authorization")
        if let body = body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let reason = (try? JSONSerialization.jsonObject(with: data) as? [String: String])?["error"] ?? "request_failed"
            throw NSError(domain: "ReferenceAPI", code: 1, userInfo: [NSLocalizedDescriptionKey: reason])
        }
        return data
    }
    func refresh() async {
        do { let data = try await call("/api/tickets"); tickets = try JSONDecoder().decode(TicketList.self, from: data).tickets; error = "" }
        catch { self.error = error.localizedDescription }
    }
    func create() async {
        guard validTicketTitle(title) else { error = "invalid_title"; return }
        do { _ = try await call("/api/tickets", body: ["title": title]); title = ""; await refresh() }
        catch { self.error = error.localizedDescription }
    }
    func assign(_ ticket: Ticket) async {
        do { _ = try await call("/api/tickets/\(ticket.id)/assign", body: ["assignee": "member"]); await refresh() }
        catch { self.error = error.localizedDescription }
    }
}
struct TicketScreen: View {
    @StateObject private var model = TicketModel()
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("AgentFlow Tickets").font(.title).accessibilityIdentifier("app-title")
            TextField("Ticket title", text: $model.title).accessibilityIdentifier("ticket-title")
            HStack {
                Button("Create ticket") { Task { await model.create() } }.accessibilityIdentifier("create-ticket")
                Button("Refresh") { Task { await model.refresh() } }.accessibilityIdentifier("refresh-tickets")
            }
            Text(model.error).accessibilityIdentifier("error-message")
            List(model.tickets) { ticket in
                HStack {
                    Text(ticket.title).accessibilityIdentifier("ticket-\(ticket.id)")
                    Text(ticket.assignee ?? "none")
                    Button("Assign") { Task { await model.assign(ticket) } }.accessibilityIdentifier("assign-\(ticket.id)")
                }
            }.accessibilityIdentifier("ticket-list")
        }.padding().task { await model.refresh() }
    }
}
