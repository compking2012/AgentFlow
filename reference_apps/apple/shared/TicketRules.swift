import Foundation
func validTicketTitle(_ value: String) -> Bool {
    let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
    return !text.isEmpty && text.unicodeScalars.count <= 120
}
