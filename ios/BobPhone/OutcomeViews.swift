import SwiftUI

/// Plain text only: evidence is user supplied, never fetched or executed.
struct OutcomeReadView: View {
    let report: OutcomeReport
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !report.available {
                Text("Outcome information unavailable")
            } else if let card = report.card {
                Text(card.statusLabel).font(.headline)
                if !card.objective.beneficiary.isEmpty { Text("For: \(card.objective.beneficiary)") }
                Text("Intended benefit: \(card.objective.intendedBenefit.isEmpty ? "Not stated" : card.objective.intendedBenefit)")
                Text("Success criterion: \(card.objective.successCriterion.isEmpty ? "Not stated" : card.objective.successCriterion)")
                if !card.objective.checkOn.isEmpty { Text(card.objective.checkLabel()) }
                Text("Acceptance records your stated criterion; a future benefit still needs its later check.").font(.caption)
                Text("Rework episodes: \(card.reworkCount)")
                if report.measurementsAvailable {
                    Text(card.waitLabel)
                    ForEach(card.waitCauses.keys.sorted(), id: \.self) { cause in
                        Text("\(cause.replacingOccurrences(of: "_", with: " ")): \(String(format: "%.1f", card.waitCauses[cause]! / 60)) min observed").font(.caption)
                    }
                    Text(card.cost.label)
                } else { Text("Measurements unavailable") }
                if let stamp = card.trackingSince {
                    Text("Tracking since \(OutcomeReadView.date(stamp))").font(.caption)
                }
                Text("Decision history · evidence supplied by you").font(.headline)
                ForEach(report.events) { event in
                    VStack(alignment: .leading, spacing: 3) {
                        Text("\(event.label) · \(OutcomeReadView.date(event.ts))").font(.caption)
                        if !event.evidence.isEmpty { Text(event.evidence).textSelection(.enabled) }
                        if event.kind == "accepted" {
                            Text("Accepted criterion: \(event.objective.successCriterion)").font(.caption)
                        }
                    }
                }
                Text("Outcome evidence and measurements remain in history when cards are deleted.").font(.caption)
            } else { Text("Outcome card unavailable") }
        }
        .font(Theme.mono(12))
        .foregroundStyle(Theme.dim)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
    static func date(_ stamp: Double) -> String {
        let f = DateFormatter()
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = "yyyy-MM-dd HH:mm 'UTC'"
        return f.string(from: Date(timeIntervalSince1970: stamp))
    }
}

struct OutcomeSummaryView: View {
    let report: OutcomeReport
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !report.available { Text("Outcome report unavailable") }
            else if let summary = report.summary {
                Text("\(summary.accepted) accepted outcomes · \(summary.awaiting) awaiting acceptance")
                if let from = report.from, let to = report.to {
                    Text("First accepted in \(OutcomeReadView.date(from)) to \(OutcomeReadView.date(to)) (end excluded)").font(.caption)
                }
                Text("Rework: \(summary.reworked) of \(summary.submitted) submitted cards · \(summary.reworkRate.map { String(format: "%.0f%%", $0 * 100) } ?? "unavailable")")
                Text("Carry-in rework: \(summary.carryIn) cards from earlier submissions").font(.caption)
                if report.measurementsAvailable {
                    if let hours = summary.cardHours {
                        Text("Observed time awaiting people: \(String(format: "%.2f", hours)) card-hours")
                    } else { Text("Observed time awaiting people: unavailable") }
                    Text("\(summary.waitCoverage["complete_since_tracking", default: 0]) complete since tracking · \(summary.waitCoverage["partial", default: 0]) partial · \(summary.waitCoverage["unknown", default: 0]) unknown").font(.caption)
                    Text("Card-hours overlap across cards; this is not human labour time.").font(.caption)
                    ForEach(summary.observedCost.keys.sorted(), id: \.self) { currency in
                        if let cost = summary.observedCost[currency], let amount = cost.perOutcome {
                            Text("\(currency) \(String(format: "%.2f", amount)) per accepted outcome (observed) · \(cost.covered) of \(cost.outcomes) outcomes covered")
                        }
                    }
                    if !summary.observedCost.isEmpty && !summary.observedCostNote.isEmpty {
                        Text(summary.observedCostNote).font(.caption)
                    }
                    ForEach(summary.cost.keys.sorted(), id: \.self) { currency in
                        if let cost = summary.cost[currency], let amount = cost.perOutcome {
                            Text("\(currency) \(String(format: "%.2f", amount)) per accepted outcome (complete) · \(cost.covered) of \(cost.outcomes) outcomes covered")
                        }
                    }
                    // Dark Army's own sentence where it has one; the literal is the
                    // older-daemon fallback and must stay exactly as it was.
                    if summary.cost.isEmpty && summary.observedCost.isEmpty {
                        if summary.costReason.isEmpty {
                            Text("Cost per accepted outcome: unavailable")
                        } else { Text(summary.costReason) }
                    }
                    ForEach(summary.awaitingCost.keys.sorted(), id: \.self) { currency in
                        Text("Awaiting acceptance so far: \(currency) \(String(format: "%.2f", summary.awaitingCost[currency]!)) · \(summary.awaitingCovered) of \(summary.awaiting) cards covered").font(.caption)
                    }
                    ForEach(summary.partialCost.keys.sorted(), id: \.self) { currency in
                        Text("Partial observed total: \(currency) \(String(format: "%.2f", summary.partialCost[currency]!))")
                    }
                } else { Text("Measurements unavailable") }
                if let start = report.trackingSince { Text("Tracking since \(OutcomeReadView.date(start))").font(.caption) }
                Text("Currently accepted cards, including retained history. Reaccepting a card counts once.").font(.caption)
            }
        }
        .font(Theme.mono(12))
        .foregroundStyle(Theme.dim)
        .textSelection(.enabled)
        .padding(10)
    }
}
