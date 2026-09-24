import Foundation

extension DaemonClient {
    func outcomeReport(cardId: String? = nil, root: String? = nil, offset: Int = 0) async -> OutcomeReport? {
        let value = (cardId ?? root ?? "").addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        var req = request("/api/outcomes?\(cardId == nil ? "root" : "card")=\(value)&offset=\(offset)")
        req.timeoutInterval = 10
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(OutcomeReport.self, from: data)
    }

    func outcomeWrite(action: String, cardId: String, fields: [String: String]) async -> OutcomeActionReply {
        var body = fields
        body["action"] = action
        body["card_id"] = cardId
        var req = request("/api/action") // carries X-Bob-Token
        req.httpMethod = "POST"
        req.timeoutInterval = 15
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let reply = try? JSONDecoder().decode(OutcomeActionReply.self, from: data)
        else { return OutcomeActionReply() }
        return reply
    }
}
