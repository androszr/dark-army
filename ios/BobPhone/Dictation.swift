import AVFoundation
import Speech
import SwiftUI
import UIKit

/// Speaking into a field instead of typing into it.
///
/// Three pure pieces and one engine. The pure pieces are here because the iOS
/// app has no test target: everything decidable without a microphone —  how a
/// hypothesis is merged into what is already written, which recogniser a
/// phone's own language resolves to, and what each refusal says — is a
/// `static` function pinned by `host/tests/test_phone_dictation.py`. The
/// engine itself is judged on a real phone.

// MARK: - Merge

/// One writer per field, appending at the end.
///
/// A partial result is the *whole* hypothesis so far, never the new words
/// alone, so the engine always writes `merged(base, hypothesis)` with `base`
/// frozen at the moment listening started. `text += partial` doubles words.
enum DictationMerge {
    static func merged(base: String, hypothesis: String) -> String {
        if hypothesis.isEmpty { return base }
        if base.isEmpty { return hypothesis }
        if let last = base.last, last.isWhitespace || last.isNewline {
            return base + hypothesis
        }
        return base + " " + hypothesis
    }
}

/// One dictation session's running text, across the recogniser's own restarts.
///
/// `merged(base:hypothesis:)` alone is only right while the hypothesis grows.
/// It does not: **the recogniser drops everything it has heard at a pause and
/// starts the transcription over** — Apple's own behaviour, reported widely
/// against iOS 18. A long note dictated into the composer on 8 Sep 2026 lost
/// every word before the last pause, because the fresh hypothesis was written
/// straight over `base`; `test_phone_dictation.py` replays that sequence
/// through this type. Anybody speaking more than a sentence or two pauses, so
/// it is not an edge case; it is what dictating a note *is*.
///
/// So a session keeps three pieces: `base`, frozen at the first tap; `banked`,
/// the utterances the recogniser has already finished; and `live`, the
/// hypothesis it is still revising. Only `live` is ever replaced.
///
/// A restart is recognised two ways, and the second is a net under the first.
/// `advance(hypothesis:utteranceEnded:)` takes the recogniser's own signal —
/// `speechRecognitionMetadata` arrives exactly when an utterance has ended —
/// and banks `live` on it. Where a phone sends no metadata the shrink rule
/// catches it: a hypothesis at most **half** the length of the one before it
/// is a fresh start, never a revision, because a recogniser polishing its
/// guess adds and rewrites words rather than throwing four fifths of them
/// away. The margin is deliberately wide: banking too eagerly repeats words on
/// screen, which a person can see and delete, while banking too late deletes
/// words they have already said, which they cannot get back.
struct DictationSession {
    /// What the field held when listening started. Never rewritten.
    let base: String
    /// Utterances the recogniser has finished with, in the order spoken.
    private(set) var banked = ""
    /// The hypothesis being revised right now.
    private(set) var live = ""

    init(base: String) {
        self.base = base
    }

    /// Fold in one result and return the whole text the field should hold.
    mutating func advance(hypothesis: String, utteranceEnded: Bool) -> String {
        if restarted(hypothesis) {
            banked = DictationMerge.merged(base: banked, hypothesis: live)
            live = hypothesis
        } else {
            live = hypothesis
        }
        let text = DictationMerge.merged(
            base: DictationMerge.merged(base: base, hypothesis: banked),
            hypothesis: live)
        if utteranceEnded {
            banked = DictationMerge.merged(base: banked, hypothesis: live)
            live = ""
        }
        return text
    }

    /// The shrink rule. Nothing to lose while `live` is empty, and a growing
    /// or lightly rewritten hypothesis is the ordinary case.
    private func restarted(_ hypothesis: String) -> Bool {
        if live.isEmpty { return false }
        if hypothesis.isEmpty { return false }
        if hypothesis.hasPrefix(live) || live.hasPrefix(hypothesis) { return false }
        return hypothesis.count * 2 <= live.count
    }
}

// MARK: - Locale

/// Which recogniser the phone's own language asks for.
///
/// No language is named in this file: the preferred identifier is whatever
/// `Locale.current` says and the candidates are whatever
/// `SFSpeechRecognizer.supportedLocales()` offers. Exact match first, then any
/// recogniser for the same language (smallest identifier, so the choice is
/// deterministic), then English as a last resort, then nothing — which the
/// engine reports as `.noRecognizer` rather than as silence.
enum DictationLocale {
    static func resolve(preferred: String, available: Set<String>) -> String? {
        let wanted = normalise(preferred)
        if wanted.isEmpty || available.isEmpty { return nil }

        let sorted = available.sorted()
        if let exact = sorted.first(where: { normalise($0) == wanted }) {
            return exact
        }
        let language = wanted.split(separator: "-").first.map(String.init) ?? wanted
        if let sameLanguage = sorted.first(where: {
            let candidate = normalise($0)
            return candidate == language || candidate.hasPrefix(language + "-")
        }) {
            return sameLanguage
        }
        if let english = sorted.first(where: { normalise($0) == "en-us" }) {
            return english
        }
        return nil
    }

    /// An underscore and a hyphen separator are one identifier, and so is
    /// either case. Anything after the region (a `@calendar=` keyword, say)
    /// is not part of the match.
    static func normalise(_ identifier: String) -> String {
        let head = identifier.split(separator: "@").first.map(String.init) ?? identifier
        return head.replacingOccurrences(of: "_", with: "-")
            .trimmingCharacters(in: .whitespaces)
            .lowercased()
    }
}

// MARK: - Refusal

/// A refusal is a visible state, never a button that does nothing.
enum DictationRefusal {
    case consent
    case noRecognizer
    case unavailable
    case audio

    var message: String {
        switch self {
        case .consent:
            return "Dictation needs the microphone and speech recognition. Turn them on for Dark Army in Settings."
        case .noRecognizer:
            return "This phone has no speech recognition for its language."
        case .unavailable:
            return "Speech recognition is not available right now — it needs the network for this language."
        case .audio:
            return "The microphone could not start. Close anything else that is recording and try again."
        }
    }
}

// MARK: - Engine

/// One engine, one listening field, one `stop()`.
///
/// The audio session must never outlive the listening, so every exit — the
/// second tap, giving a field the keyboard, leaving the screen, saving or
/// sending, resigning active, an interruption, the hard cap — lands in the
/// single idempotent `stop()`. A new exit added later must call it too.
@MainActor
final class DictationEngine: ObservableObject {
    static let shared = DictationEngine()

    /// Which field is listening, by the id its `MicButton` was given.
    @Published private(set) var activeField: String?
    /// The refusal on screen, and the field it belongs under.
    @Published private(set) var note = ""
    @Published private(set) var noteField: String?
    /// True only across a system consent alert. `BobPhoneApp` skips its Face
    /// ID lock on this: the alert drives `scenePhase` to `.inactive`, and
    /// locking would unmount the composer and destroy the half-typed card.
    @Published private(set) var requestingConsent = false

    /// Server-based recognition cuts out around a minute anyway; this makes
    /// the ceiling ours and visible. Deliberately no silence timeout — a
    /// pause is thinking, not finishing.
    static let maxListenSeconds: UInt64 = 60

    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var recognizer: SFSpeechRecognizer?
    private var target: Binding<String>?
    /// The running text across the recogniser's restarts. `nil` when nothing
    /// is listening. Not `session`: `start()` has an `AVAudioSession` local by
    /// that name, and the shadow compiled into an assignment at the wrong one.
    private var transcript: DictationSession?
    private var capTask: Task<Void, Never>?

    private init() {
        subscribe()
    }

    private func subscribe() {
        let center = NotificationCenter.default
        center.addObserver(forName: UIApplication.willResignActiveNotification,
                           object: nil, queue: nil) { [weak self] _ in
            Task { @MainActor in self?.stop() }
        }
        center.addObserver(forName: AVAudioSession.interruptionNotification,
                           object: nil, queue: nil) { [weak self] _ in
            Task { @MainActor in self?.stop() }
        }
    }

    func isListening(_ field: String) -> Bool {
        activeField == field
    }

    /// Tap on a field's microphone: stop if it is the one listening, start
    /// otherwise.
    func toggle(field: String, text: Binding<String>) async {
        if activeField == field {
            stop()
        } else {
            await start(field: field, text: text)
        }
    }

    func start(field: String, text: Binding<String>) async {
        stop()
        clearNote()

        guard await consent() else { return refuse(.consent, field: field) }

        let available = Set(SFSpeechRecognizer.supportedLocales().map(\.identifier))
        guard let identifier = DictationLocale.resolve(
            preferred: Locale.current.identifier, available: available) else {
            return refuse(.noRecognizer, field: field)
        }
        guard let speech = SFSpeechRecognizer(locale: Locale(identifier: identifier)) else {
            return refuse(.noRecognizer, field: field)
        }
        guard speech.isAvailable else { return refuse(.unavailable, field: field) }

        let audioRequest = SFSpeechAudioBufferRecognitionRequest()
        audioRequest.shouldReportPartialResults = true
        // Where the phone can recognise this language locally, nothing leaves
        // the device. Where it cannot, server recognition is allowed and its
        // absence surfaces as `.unavailable`.
        audioRequest.requiresOnDeviceRecognition = speech.supportsOnDeviceRecognition

        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .measurement, options: .duckOthers)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            return refuse(.audio, field: field)
        }

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            releaseSession()
            return refuse(.audio, field: field)
        }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            // Audio thread. Nothing here touches UI or the binding.
            audioRequest.append(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            releaseSession()
            return refuse(.audio, field: field)
        }

        recognizer = speech
        request = audioRequest
        target = text
        transcript = DictationSession(base: text.wrappedValue)
        activeField = field

        task = speech.recognitionTask(with: audioRequest) { [weak self] result, error in
            // The recogniser's own queue. Re-enter the actor before writing.
            let hypothesis = result?.bestTranscription.formattedString
            // The recogniser says an utterance has ended by attaching
            // metadata to that result — the one signal that a fresh
            // transcription is about to start rather than the same one
            // growing. `DictationSession` banks the words on it.
            let ended = result?.speechRecognitionMetadata != nil
            let finished = result?.isFinal ?? false
            let failed = error != nil
            Task { @MainActor in
                guard let self, self.activeField == field else { return }
                if let hypothesis {
                    self.write(hypothesis, utteranceEnded: ended)
                }
                if finished || failed {
                    self.stop()
                }
            }
        }

        capTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: Self.maxListenSeconds * 1_000_000_000)
            if Task.isCancelled { return }
            await MainActor.run { self?.stop() }
        }
    }

    /// Idempotent, and every exit lands here.
    func stop() {
        capTask?.cancel()
        capTask = nil
        if engine.isRunning {
            engine.stop()
        }
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        request = nil
        task?.cancel()
        task = nil
        recognizer = nil
        target = nil
        transcript = nil
        if activeField != nil {
            activeField = nil
            releaseSession()
        }
    }

    func clearNote() {
        note = ""
        noteField = nil
    }

    private func write(_ hypothesis: String, utteranceEnded: Bool) {
        guard let target, var running = transcript else { return }
        let text = running.advance(hypothesis: hypothesis,
                                   utteranceEnded: utteranceEnded)
        transcript = running
        target.wrappedValue = text
    }

    private func refuse(_ refusal: DictationRefusal, field: String) {
        note = refusal.message
        noteField = field
    }

    private func releaseSession() {
        try? AVAudioSession.sharedInstance()
            .setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Asked at the first tap and never at launch. A prior denial of either
    /// permission goes straight to the refusal without a second alert.
    private func consent() async -> Bool {
        if SFSpeechRecognizer.authorizationStatus() == .notDetermined {
            requestingConsent = true
            let granted: SFSpeechRecognizerAuthorizationStatus =
                await withCheckedContinuation { continuation in
                    SFSpeechRecognizer.requestAuthorization { status in
                        continuation.resume(returning: status)
                    }
                }
            requestingConsent = false
            if granted != .authorized { return false }
        } else if SFSpeechRecognizer.authorizationStatus() != .authorized {
            return false
        }

        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return true
        case .undetermined:
            requestingConsent = true
            let allowed: Bool = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
            requestingConsent = false
            return allowed
        default:
            return false
        }
    }
}

// MARK: - Button

/// The microphone beside a writing field.
///
/// Idle: a quiet `mic` glyph in the picker chrome's square. Listening: a red
/// stop mark *and* the word REC — a glyph change plus a word, never colour
/// alone.
struct MicButton: View {
    let id: String
    @Binding var text: String
    @ObservedObject private var engine = DictationEngine.shared

    private var listening: Bool { engine.isListening(id) }

    var body: some View {
        DecryptButton {
            Task { await engine.toggle(field: id, text: $text) }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: listening ? "stop.fill" : "mic")
                    .font(Theme.mono(12))
                    .foregroundStyle(listening ? Theme.alarm : Theme.faint)
                    // The button below carries the name; the glyph is the
                    // same fact drawn twice.
                    .accessibilityHidden(true)
                if listening {
                    Text("REC")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.alarm)
                }
            }
            .frame(minWidth: 44, minHeight: 44)
            .padding(.horizontal, 6)
            .overlay(Rectangle().stroke(listening ? Theme.alarm : Theme.hair, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(listening ? "Stop dictating" : "Dictate")
    }
}
