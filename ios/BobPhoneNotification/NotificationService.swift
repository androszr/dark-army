import Foundation
import Intents
import UserNotifications

/// Adjusts one banner before it is shown. One portrait, no secrets, no
/// network, and nothing written down about the title or the slug.
final class NotificationService: UNNotificationServiceExtension {
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var bestAttempt: UNNotificationContent?

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        self.contentHandler = contentHandler
        let original = request.content
        guard let content = original.mutableCopy() as? UNMutableNotificationContent else {
            finish(original)
            return
        }
        bestAttempt = content
        let slug = (original.userInfo["face"] as? String) ?? ""
        guard
            let directory = Bundle.main.url(forResource: "portraits", withExtension: nil),
            let file = NotificationFace.portraitURL(portraitsDirectory: directory, slug: slug)
        else {
            finish(original)
            return
        }
        do {
            let data = try Data(contentsOf: file)
            let image = INImage(imageData: data)
            let sender = NotificationFace.senderIdentifier(slug: slug, portrait: data)
            let handle = INPersonHandle(value: sender, type: .unknown)
            let person = INPerson(
                personHandle: handle,
                nameComponents: nil,
                displayName: slug,
                image: image,
                contactIdentifier: nil,
                customIdentifier: sender,
                isMe: false,
                suggestionType: .none
            )
            let intent = INSendMessageIntent(
                recipients: nil,
                outgoingMessageType: .outgoingMessageText,
                content: original.body,
                speakableGroupName: nil,
                conversationIdentifier: sender,
                serviceName: nil,
                sender: person,
                attachments: nil
            )
            let updated = try original.updating(from: intent)
            guard let mutable = updated.mutableCopy() as? UNMutableNotificationContent else {
                finish(try portraitAttachment(on: content, file: file))
                return
            }
            if mutable.title != original.title
                || mutable.subtitle != original.subtitle
                || mutable.body != original.body {
                mutable.title = original.title
                mutable.subtitle = original.subtitle
                mutable.body = original.body
            }
            finish(mutable)
        } catch {
            do {
                finish(try portraitAttachment(on: content, file: file))
            } catch {
                finish(original)
            }
        }
    }

    override func serviceExtensionTimeWillExpire() {
        guard let handler = contentHandler else { return }
        contentHandler = nil
        handler(bestAttempt ?? UNMutableNotificationContent())
    }

    private func finish(_ content: UNNotificationContent) {
        bestAttempt = content
        guard let handler = contentHandler else { return }
        contentHandler = nil
        handler(content)
    }

    /// A copy: the attachment store moves the file it is given.
    private func portraitAttachment(
        on content: UNMutableNotificationContent,
        file: URL
    ) throws -> UNNotificationContent {
        let copy = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
            .appendingPathExtension("png")
        try FileManager.default.copyItem(at: file, to: copy)
        let attachment = try UNNotificationAttachment(
            identifier: "portrait",
            url: copy,
            options: nil
        )
        content.attachments = [attachment]
        return content
    }
}
