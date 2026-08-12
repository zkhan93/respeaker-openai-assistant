import Foundation
import os

/// Logging for both halves of the app.
///
/// ## Three tiers, because they answer different questions
///
/// 1. **Unified logging (`os.Logger`)** — live, structured, and nearly
///    free when nobody is listening. This is what Apple wants apps to
///    use, and what `log stream` / Console.app read. Best for "what is it
///    doing right now".
///
/// 2. **A file in `~/Library/Logs/Raneen/`** — because unified logging is
///    a ring buffer. It gets evicted, it is awkward to read after the
///    fact, and you cannot ask a user to email you a slice of it. When
///    the question is "it crashed an hour ago, what happened", only a
///    file answers.
///
/// 3. **Crash reports** (`~/Library/Logs/DiagnosticReports/*.ips`) —
///    written by the system, no code required, and authoritative for
///    crashes. Worth knowing they already exist: the `NSMenuItem
///    setSubmenu:` abort was diagnosed entirely from one of these.
///
/// ## Why not `print` or `NSLog`
///
/// `print` goes nowhere in a bundled app — there is no terminal attached.
/// `NSLog` does reach unified logging, but it has no levels, no
/// categories, and takes a lock and writes synchronously on every call,
/// which is why it is a genuinely bad idea on an audio path.
///
/// ## The privacy trap
///
/// `os.Logger` redacts interpolated values as `<private>` unless told
/// otherwise — a safe default that silently makes logs useless if you do
/// not know about it. Everything here goes through `String` arguments
/// marked `.public`, because none of it is sensitive.
///
/// **Transcribed text is never logged, at any level.** It is the most
/// sensitive thing this app touches, and a log file is exactly the wrong
/// place for it. Lengths and timings only.
struct Log {

    static let subsystem = Bundle.main.bundleIdentifier ?? "com.nexuscraftlabs.raneen"

    /// Categories, so `log show --predicate 'category == "audio"'` works
    /// and so the file is greppable by area.
    static let app = Log("app")
    static let audio = Log("audio")
    static let devices = Log("devices")
    static let hotkey = Log("hotkey")
    static let helper = Log("helper")

    let category: String
    private let logger: Logger

    private init(_ category: String) {
        self.category = category
        self.logger = Logger(subsystem: Log.subsystem, category: category)
    }

    // MARK: - Levels
    //
    // debug  — per-event detail. Off unless asked for; this is where
    //          anything that fires at frame rate belongs.
    // info   — lifecycle. Started, stopped, switched device.
    // error  — something failed that the user may notice.
    // fault  — a programmer error: a state we believed impossible.

    func debug(_ message: String) {
        logger.debug("\(message, privacy: .public)")
        LogFile.shared.write(level: "DEBUG", category: category, message: message)
    }

    func info(_ message: String) {
        logger.info("\(message, privacy: .public)")
        LogFile.shared.write(level: "INFO", category: category, message: message)
    }

    func error(_ message: String) {
        logger.error("\(message, privacy: .public)")
        LogFile.shared.write(level: "ERROR", category: category, message: message)
    }

    func fault(_ message: String) {
        logger.fault("\(message, privacy: .public)")
        LogFile.shared.write(level: "FAULT", category: category, message: message)
    }

    /// Forward one line of the helper's stderr, preserving its level.
    ///
    /// Python formats as `HH:MM:SS LEVEL   name: message`, so the level is
    /// recoverable. Keeping it means a Python traceback shows up as an
    /// error rather than being flattened into the same stream as routine
    /// chatter — the difference between a searchable log and a wall.
    func helperLine(_ line: String) {
        let fields = line.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
        let level = fields.count >= 2 ? String(fields[1]) : "INFO"
        switch level {
        case "ERROR", "CRITICAL":
            error(line)
        case "WARNING":
            info(line)
        case "DEBUG":
            debug(line)
        default:
            // Unparseable lines are usually tracebacks, which continue a
            // previous ERROR — worth keeping at info rather than dropping.
            info(line)
        }
    }

    // MARK: - Crashes we can catch ourselves

    /// Log uncaught Objective-C exceptions before the process dies.
    ///
    /// The system writes a crash report either way, but that lands in a
    /// separate place, in a format nobody reads casually, and without any
    /// of our own context around it. This puts the reason and the stack in
    /// the same file as everything leading up to it.
    ///
    /// Catches `NSException` only — not Swift `fatalError` or a failed
    /// precondition, which trap rather than throw. That still covers most
    /// AppKit misuse, which is where this class of bug actually comes
    /// from: the submenu abort was an `NSException`.
    static func installCrashHandler() {
        NSSetUncaughtExceptionHandler { exception in
            let stack = exception.callStackSymbols.joined(separator: "\n  ")
            Log.app.fault(
                """
                uncaught exception: \(exception.name.rawValue)
                reason: \(exception.reason ?? "none")
                  \(stack)
                """
            )
            LogFile.shared.flush()
        }
    }
}

/// Appends to `~/Library/Logs/Raneen/raneen.log`, with rotation.
///
/// A plain file rather than anything clever, because its job is to be
/// readable by a person who has just had something go wrong — and, when
/// it goes wrong on someone else's machine, to be attachable to a message.
final class LogFile {

    static let shared = LogFile()

    /// Rotate at 5 MB and keep one previous file. Enough history for a
    /// session or two; bounded so a runaway loop cannot fill a disk. The
    /// device-restart loop wrote several lines a second — exactly the
    /// scenario an unbounded log would have turned into a second bug.
    private static let maxBytes = 5 * 1024 * 1024

    private let queue = DispatchQueue(label: "Raneen.log", qos: .utility)
    private let url: URL?
    private var handle: FileHandle?

    private static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter
    }()

    private init() {
        let base = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask).first?
            .appendingPathComponent("Logs/Raneen", isDirectory: true)
        guard let base else {
            url = nil
            return
        }
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        url = base.appendingPathComponent("raneen.log")
    }

    /// Where to point someone who asks for the log.
    var path: String { url?.path ?? "(unavailable)" }

    func write(level: String, category: String, message: String) {
        guard let url else { return }
        let line = "\(Self.stamp.string(from: Date())) \(level.padding(toLength: 5, withPad: " ", startingAt: 0)) [\(category)] \(message)\n"
        // Off the caller's thread: this is reached from the audio path,
        // and a synchronous file write there would be a dropout.
        queue.async { [weak self] in
            self?.append(line, to: url)
        }
    }

    /// Block until pending writes land. Only for the crash handler, where
    /// the process is about to stop existing.
    func flush() {
        queue.sync {}
        try? handle?.synchronize()
    }

    private func append(_ line: String, to url: URL) {
        if handle == nil {
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: nil)
            }
            handle = try? FileHandle(forWritingTo: url)
            // seekToEnd() returns the new offset and is not
            // @discardableResult, so the `try?` result needs discarding
            // explicitly. Appending, not truncating: the point of this
            // file is that it survives across runs.
            _ = try? handle?.seekToEnd()
        }
        guard let handle, let data = line.data(using: .utf8) else { return }
        try? handle.write(contentsOf: data)

        if let size = try? handle.offset(), size > UInt64(Self.maxBytes) {
            rotate(url)
        }
    }

    private func rotate(_ url: URL) {
        if let handle { try? handle.close() }
        handle = nil

        let previous = url.deletingPathExtension().appendingPathExtension("1.log")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
    }
}
