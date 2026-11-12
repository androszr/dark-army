import SwiftUI

/// What waits to be typed into this agent and what Dark Army could not
/// deliver, drawn under the detail header on both tabs.
///
/// **Draw only.** The daemon composes every word (`message_queue` on the row:
/// the headline, each label, status, excerpt and reason, the "and N more"
/// line); this band picks a colour from the item's `group` and nothing else. It
/// holds no clock, no verb and no button: cancelling or retrying a message is
/// not a thing this list offers.
struct MessageQueueBand: View {
    let queue: MessageQueue

    private func line(_ item: MessageQueue.Item) -> String {
        item.label + " · " + item.status
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(queue.headline)
                .font(Theme.mono(11, weight: .medium))
                .foregroundStyle(queue.hasUndelivered ? Theme.attention : Theme.dim)
            ForEach(Array(queue.items.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 1) {
                    Text(line(item))
                        .font(Theme.mono(11))
                        .foregroundStyle(item.undelivered ? Theme.attention : Theme.text)
                    if !item.excerpt.isEmpty {
                        Text(item.excerpt)
                            .font(Theme.prose(12))
                            .foregroundStyle(Theme.dim)
                    }
                    if !item.why.isEmpty {
                        Text(item.why)
                            .font(Theme.prose(11))
                            .foregroundStyle(Theme.attention)
                    }
                }
            }
            if !queue.moreLine.isEmpty {
                Text(queue.moreLine)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// The same strings, joined for VoiceOver.
    private var spoken: String {
        var parts = [queue.headline]
        for item in queue.items {
            parts.append(line(item))
            parts.append(item.excerpt)
            parts.append(item.why)
        }
        parts.append(queue.moreLine)
        return parts.filter { !$0.isEmpty }.joined(separator: ". ")
    }
}
