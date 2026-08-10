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
        case level(peak: Int)
        case error(message: String)
        case pong(armed: Bool)
        case exited(status: Int32)
        case unknown(raw: String)
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
            FileHandle.standardError.write("[helper] \(line)\n".data(using: .utf8)!)
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
            NSLog("helper did not exit within %.1fs — terminating", timeout)
            process.terminate()
        }
    }

    // MARK: - Commands

    func arm() { send(["cmd": "arm"]) }
    func disarm() { send(["cmd": "disarm"]) }
    func ping() { send(["cmd": "ping"]) }

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
                NSLog("could not write to helper: %@", error.localizedDescription)
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
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }  // EOF
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
            onEvent?(.level(peak: object["peak"] as? Int ?? 0))
        case "error":
            onEvent?(.error(message: object["message"] as? String ?? "unknown error"))
        case "pong":
            onEvent?(.pong(armed: object["armed"] as? Bool ?? false))
        case "bye":
            break
        default:
            onEvent?(.unknown(raw: line))
        }
    }
}
