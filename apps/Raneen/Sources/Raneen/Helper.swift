import Foundation

/// The Python core, running as a child process.
///
/// Commands go down its stdin, events come back up its stdout, one JSON
/// object per line each way. See `voice_desktop/sidecar.py` for the
/// protocol and for why this is a pipe rather than a socket.
///
/// Everything here is `@MainActor`-free on purpose: reads happen on a
/// background queue and are hopped to the main actor by the caller, so a
/// slow UI update can never stall the pipe.
final class Helper {

    enum Event {
        case ready(engine: String, model: String)
        case state(pattern: String)
        case transcript(text: String)
        /// `blocks` are 20 ms loudness readings, so four arrive at once
        /// every 80 ms. `peak` is kept because it is the honest answer to
        /// "is the microphone hearing anything", which is a different
        /// question from what the meter draws.
        case level(peak: Int, blocks: [Int])
        case error(message: String)
        case pong(armed: Bool)
        /// Someone is speaking. Arrives repeatedly while they talk —
        /// `settled` marks the final answer for a stretch of speech, so a
        /// list that wants one row per person filters on it.
        /// `startedAt`/`endedAt` are seconds of audio since the core
        /// began ingesting — the span of the run of speech, which is
        /// what lets text be attributed to a person rather than merely
        /// noting that they are in the room.
        case speakerIdentified(
            id: String, name: String?, score: Double, settled: Bool,
            startedAt: Double, endedAt: Double)
        /// The roster, in reply to `speakers()`.
        case speakers([SpeakerProfile])
        case exited(status: Int32)
        case unknown(raw: String)
    }

    /// One enrolled voice, as the settings window shows it.
    struct SpeakerProfile: Identifiable, Equatable {
        let id: String
        var name: String?
        /// Voiceprints folded into this profile. One is a first guess;
        /// the more there are, the better it recognises them.
        let samples: Int
        /// A few seconds of this person, kept by the core when it first
        /// heard them. **The only thing in this list a human can act on**
        /// — nobody recognises `speaker_3` from an id and a count, and
        /// naming the wrong person is worse than leaving them unnamed.
        let clip: URL?
    }

    private let executable: URL
    private let arguments: [String]
    private var process: Process?
    private var stdin: FileHandle?

    /// Serialises writes. `arm` and `disarm` can arrive from the event
    /// tap thread while a `quit` comes from the main thread.
    private let writeQueue = DispatchQueue(label: "Raneen.helper.write")

    /// Called for every event. Invoked on an arbitrary queue.
    var onEvent: ((Event) -> Void)?

    init(executable: URL, arguments: [String] = []) {
        self.executable = executable
        self.arguments = arguments
    }

    var isRunning: Bool { process?.isRunning ?? false }

    // MARK: - Lifecycle

    func start() throws {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments

        let toHelper = Pipe()
        let fromHelper = Pipe()
        let helperLog = Pipe()
        process.standardInput = toHelper
        process.standardOutput = fromHelper
        process.standardError = helperLog

        // The helper inherits our environment; force unbuffered Python so
        // events arrive as they happen rather than in 4KB blocks. Without
        // this the level meter stutters and `ready` can arrive seconds late.
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        process.terminationHandler = { [weak self] proc in
            self?.onEvent?(.exited(status: proc.terminationStatus))
        }

        try process.run()

        self.process = process
        self.stdin = toHelper.fileHandleForWriting

        readLines(from: fromHelper.fileHandleForReading) { [weak self] line in
            self?.dispatch(line)
        }
        // The helper logs to stderr. Forward it rather than dropping it —
        // this is where a Python traceback appears, and a silently dead
        // helper is the worst thing to debug.
        readLines(from: helperLog.fileHandleForReading) { line in
            Log.helper.helperLine(line)
        }
    }

    /// Ask the helper to quit, then wait briefly for it to go.
    ///
    /// Closing stdin alone would be enough — the helper treats EOF as a
    /// shutdown — but sending `quit` first gives it a chance to flush a
    /// transcript that is still being decoded.
    func stop(timeout: TimeInterval = 5.0) {
        guard let process, process.isRunning else { return }
        send(["cmd": "quit"])
        writeQueue.sync { try? stdin?.close() }

        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning {
            // It is holding the microphone. Taking that away from the
            // user is worse than an unclean exit.
            Log.helper.error("helper did not exit within \(timeout)s — terminating")
            process.terminate()
        }
    }

    // MARK: - Commands

    func arm() { send(["cmd": "arm"]) }
    func disarm() { send(["cmd": "disarm"]) }
    func ping() { send(["cmd": "ping"]) }

    /// Ask for the speaker roster. Answered by a `speakers` event.
    func requestSpeakers() { send(["cmd": "speakers"]) }

    /// Bind a name to a voice the core discovered.
    func enroll(speaker: String, name: String) {
        send(["cmd": "enroll", "speaker": speaker, "name": name])
    }

    /// Forget a voice. Their id is never reused.
    func forget(speaker: String) { send(["cmd": "forget", "speaker": speaker]) }

    /// Attach the next few seconds of speech to this name.
    ///
    /// **The only way anybody enters the registry.** The core does not
    /// invent profiles for voices it fails to recognise — a failed match
    /// is as often a poor recording of somebody known as it is a new
    /// person, and guessing filled the registry with fragments of one
    /// voice. Repeating this with the same name improves that profile
    /// rather than adding a second.
    func learn(name: String) { send(["cmd": "learn", "name": name]) }

    /// Cancel a pending `learn`, so a dismissed sheet does not leave the
    /// microphone armed to enrol whoever speaks next.
    func cancelLearning() { send(["cmd": "learn"]) }

    private func send(_ command: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: command),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"

        writeQueue.async { [weak self] in
            guard let handle = self?.stdin, let bytes = line.data(using: .utf8) else { return }
            do {
                try handle.write(contentsOf: bytes)
            } catch {
                // Broken pipe: the helper died. The termination handler
                // reports it; nothing useful to do here.
                Log.helper.error("could not write to helper: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Reading

    /// Read newline-delimited text off a handle until EOF.
    ///
    /// `availableData` hands back arbitrary chunks, not lines, so a
    /// partial line has to be carried across reads — otherwise a JSON
    /// object split across a buffer boundary is silently dropped, which
    /// shows up as an occasional missing transcript.
    private func readLines(from handle: FileHandle, _ onLine: @escaping (String) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            var buffer = Data()
            var open = true
            while open {
                // Each pass must drain its own autorelease pool. This block
                // never returns while the helper lives, and GCD only drains
                // a thread's pool when a block completes — so without this,
                // every `availableData` NSData, every decoded String, and
                // every JSON object graph built downstream in `onLine` is
                // pinned until the app quits. Measured: 2.8 GB of dirty
                // MALLOC_SMALL after four hours of level events.
                autoreleasepool {
                    let chunk = handle.availableData
                    if chunk.isEmpty {  // EOF
                        open = false
                        return
                    }
                    buffer.append(chunk)

                    while let newline = buffer.firstIndex(of: 0x0A) {
                        let lineData = buffer[buffer.startIndex..<newline]
                        buffer.removeSubrange(buffer.startIndex...newline)
                        if let line = String(data: lineData, encoding: .utf8),
                           !line.trimmingCharacters(in: .whitespaces).isEmpty {
                            onLine(line)
                        }
                    }
                }
            }
        }
    }

    private func dispatch(_ line: String) {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let name = object["event"] as? String else {
            onEvent?(.unknown(raw: line))
            return
        }

        switch name {
        case "ready":
            onEvent?(.ready(
                engine: object["engine"] as? String ?? "?",
                model: object["model"] as? String ?? "?"
            ))
        case "state":
            onEvent?(.state(pattern: object["pattern"] as? String ?? "?"))
        case "transcript":
            onEvent?(.transcript(text: object["text"] as? String ?? ""))
        case "level":
            onEvent?(
                .level(
                    peak: object["peak"] as? Int ?? 0,
                    blocks: object["rms"] as? [Int] ?? []))
        case "error":
            onEvent?(.error(message: object["message"] as? String ?? "unknown error"))
        case "pong":
            onEvent?(.pong(armed: object["armed"] as? Bool ?? false))
        case "speaker_identified":
            onEvent?(.speakerIdentified(
                id: object["speaker"] as? String ?? "?",
                name: object["name"] as? String,
                score: object["score"] as? Double ?? 0,
                settled: object["settled"] as? Bool ?? false,
                startedAt: object["started_at"] as? Double ?? 0,
                endedAt: object["ended_at"] as? Double ?? 0
            ))
        case "speakers":
            let listed = (object["speakers"] as? [[String: Any]] ?? []).map {
                SpeakerProfile(
                    id: $0["id"] as? String ?? "?",
                    name: $0["name"] as? String,
                    samples: $0["samples"] as? Int ?? 0,
                    clip: ($0["clip"] as? String).map(URL.init(fileURLWithPath:))
                )
            }
            onEvent?(.speakers(listed))
        case "bye":
            break
        default:
            onEvent?(.unknown(raw: line))
        }
    }
}
