import AppKit

/// Types a transcript into whatever application has focus.
///
/// Uses `CGEvent.keyboardSetUnicodeString`, which delivers arbitrary text
/// without mapping it to keycodes — so accented characters, punctuation
/// and emoji all arrive intact regardless of the user's keyboard layout.
/// The Python build had to send characters one at a time through pynput
/// and could not do that reliably.
///
/// Three details that are easy to get wrong and produce garbled text:
///
/// **The event source is `.privateState`,** not the combined session
/// state. A synthetic event built from the shared state inherits the
/// modifiers physically held down at that moment — and under
/// hold-to-talk there is a very good chance a modifier *is* down. The
/// target app would then see ⌥-decorated keystrokes instead of text.
///
/// **Text is chunked,** because a single event carrying a long string is
/// silently truncated or dropped outright by some applications. Chunks
/// are split on `Character` boundaries so a surrogate pair (an emoji)
/// never lands half in one event and half in the next.
///
/// **Posting is serialised on its own queue.** Two transcripts arriving
/// close together must not interleave their chunks, and a long paragraph
/// must not block the main thread mid-insert.
final class TextInserter {

    /// UTF-16 units per event. Well under the point where applications
    /// start dropping the string; the cost of a smaller number is a few
    /// more events, which is nothing.
    private static let maxChunkUTF16 = 16

    /// Between chunks. Some apps — Electron and Java ones especially —
    /// drop events posted back to back with no gap at all.
    private static let chunkDelay: TimeInterval = 0.002

    private let source: CGEventSource?
    private let queue = DispatchQueue(label: "Raneen.textinserter")

    /// When false, transcripts are received but not typed. Lets the user
    /// dictate into the menu-bar display without it landing in whatever
    /// they happen to be looking at.
    var isEnabled = true

    /// Separate consecutive utterances. Without it, "hello" and "world"
    /// arrive as "helloworld".
    var appendTrailingSpace = true

    init() {
        // .privateState — see the note above about inherited modifiers.
        source = CGEventSource(stateID: .privateState)
    }

    /// Type `text` at the cursor. Returns immediately; posting happens
    /// on a background queue in the order calls were made.
    func insert(_ text: String) {
        guard isEnabled else { return }
        let payload = appendTrailingSpace ? text + " " : text
        guard !payload.isEmpty else { return }

        queue.async { [weak self] in
            guard let self else { return }
            for chunk in Self.chunks(of: payload) {
                self.post(chunk)
                Thread.sleep(forTimeInterval: Self.chunkDelay)
            }
        }
    }

    // MARK: - Internals

    /// Split into pieces of at most `maxChunkUTF16` UTF-16 units, never
    /// breaking a `Character`.
    ///
    /// Chunking by UTF-16 index directly would be simpler and wrong: it
    /// can cut an emoji's surrogate pair in half, and the two halves are
    /// not valid text in either event.
    static func chunks(of text: String) -> [String] {
        var result: [String] = []
        var current = ""
        var currentCount = 0

        for character in text {
            let size = String(character).utf16.count
            if currentCount + size > maxChunkUTF16, !current.isEmpty {
                result.append(current)
                current = ""
                currentCount = 0
            }
            current.append(character)
            currentCount += size
        }
        if !current.isEmpty {
            result.append(current)
        }
        return result
    }

    private func post(_ chunk: String) {
        let units = Array(chunk.utf16)

        // virtualKey 0 with a unicode string attached: the keycode is
        // irrelevant, the string is what gets delivered.
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
            NSLog("could not create keyboard event for insertion")
            return
        }

        down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
        up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)

        // Belt and braces alongside .privateState: make sure nothing
        // decorates these as ⌘/⌥/⌃ keystrokes.
        down.flags = []
        up.flags = []

        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}
