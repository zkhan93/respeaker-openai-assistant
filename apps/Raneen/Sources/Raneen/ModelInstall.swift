import CryptoKit
import Foundation

/// The filesystem half of downloading a model: where it goes, whether it
/// arrived intact, and whether there is room for it.
///
/// Split out from `ModelDownloader` because all of it is testable without a
/// network, a session or a run loop — and because these are the checks that
/// decide whether a bad download becomes a bad *model*. The last time a
/// setting reached the core in a shape it could not use, it presented as
/// "dictation is broken" rather than as a bad file.
enum ModelInstall {

    /// Where downloads land.
    ///
    /// `~/.cache/raneen/models`, which is the second place the core looks
    /// for its default model and the directory the `tools/` scripts and the
    /// Makefile already use. A separate download location would work — the
    /// chosen path is passed to the core explicitly — but it would mean two
    /// answers to "where are my models".
    static var directory: URL { ModelLibrary.userDirectory }

    static func destination(for filename: String) -> URL {
        directory.appendingPathComponent(filename)
    }

    /// What the core loads when the settings name no model at all.
    ///
    /// Mirrors `default_model()` in the core's `main.rs`, which looks beside
    /// its own executable and then in the user cache for exactly this name.
    /// Duplicated rather than asked for because the core has no "which model
    /// would you pick" mode — and the settings window has to be able to show
    /// a row as selected when nothing is explicitly chosen.
    static let defaultFilename = "ggml-base.en-q5_1.bin"

    /// The partly-written file.
    ///
    /// **A download is never visible under its real name until it has been
    /// verified.** Half a model loads as far as the ggml header and then
    /// fails with a message that reads like a broken build rather than an
    /// interrupted download; `tools/fetch-wakeword-models.sh` carries the
    /// same `.part` dance for the same reason.
    static func partial(for filename: String) -> URL {
        destination(for: filename).appendingPathExtension("part")
    }

    /// Where a catalogue model already is, if anywhere.
    ///
    /// **Searches the bundle too, not only the download directory.** The app
    /// ships `base.en-q5_1` inside itself, and a row offering to download a
    /// model that is already in the application would be both wrong and
    /// slightly insulting. Same order the core resolves in.
    static func installedPath(for filename: String, using fs: FileManager = .default) -> String? {
        for directory in ModelLibrary.searchPaths {
            let candidate = directory.appendingPathComponent(filename).path
            if fs.fileExists(atPath: candidate) { return candidate }
        }
        return nil
    }

    static func isInstalled(_ filename: String, using fs: FileManager = .default) -> Bool {
        installedPath(for: filename, using: fs) != nil
    }

    static func createDirectory(using fs: FileManager = .default) throws {
        try fs.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    // MARK: - Verification

    /// The first four bytes of every ggml model file.
    ///
    /// `0x67676d6c` — "ggml" — written little-endian, so on disk it reads
    /// "lmgg". Checked because the interesting failure is not a truncated
    /// model but an *untruncated* one: a captive portal or a moved URL
    /// answers with a complete, correct-looking HTML page, and without this
    /// the first sign of trouble is whisper failing to parse a login form.
    static let ggmlMagic: [UInt8] = [0x6c, 0x6d, 0x67, 0x67]

    static func hasGgmlMagic(at url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        guard let head = try? handle.read(upToCount: ggmlMagic.count) else { return false }
        return Array(head) == ggmlMagic
    }

    /// SHA-256 of a file, read in chunks.
    ///
    /// Streamed rather than `Data(contentsOf:)`: the largest model here is
    /// 3.1 GB, and hashing it by loading it would briefly double the app's
    /// footprint at exactly the moment the user is about to load a model
    /// that size into the core.
    static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }

        var hasher = SHA256()
        while let chunk = try handle.read(upToCount: 1 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// Why a downloaded file was rejected, in the words the user sees.
    enum Rejection: Equatable {
        case wrongSize(expected: Int64, got: Int64)
        case notAModel
        case wrongDigest

        var message: String {
            switch self {
            case .wrongSize(let expected, let got):
                let e = ByteCountFormatter.string(fromByteCount: expected, countStyle: .file)
                let g = ByteCountFormatter.string(fromByteCount: got, countStyle: .file)
                return "The download is \(g); it should be \(e). Try again."
            case .notAModel:
                return "What arrived is not a model file. The download may have been intercepted."
            case .wrongDigest:
                return "The download is damaged and has been discarded. Try again."
            }
        }
    }

    /// Whether a finished file is the model it claims to be.
    ///
    /// Ordered cheapest-first on purpose: size is a stat, the magic is four
    /// bytes, and the digest reads 3 GB. All three run before the file is
    /// renamed, so a rejected download leaves nothing behind that the core
    /// could later pick up.
    static func inspect(_ url: URL, against model: CatalogModel) -> Rejection? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        if let size = attributes?[.size] as? Int64, size != model.bytes {
            return .wrongSize(expected: model.bytes, got: size)
        }
        guard hasGgmlMagic(at: url) else { return .notAModel }
        guard let digest = try? sha256(of: url), digest == model.sha256 else { return .wrongDigest }
        return nil
    }

    // MARK: - Room

    /// Headroom kept free beyond the model itself.
    ///
    /// macOS on a nearly-full disk stops being able to swap or write
    /// snapshots, and a download that succeeds by filling the volume is not
    /// a success. Half a gigabyte is arbitrary but generous enough that the
    /// failure is ours to report rather than the OS's to suffer.
    static let freeSpaceMargin: Int64 = 512 * 1024 * 1024

    /// Pure so the arithmetic is testable; `available == nil` means the
    /// volume would not say, and refusing to download because we could not
    /// measure would be worse than trying.
    static func hasRoom(for bytes: Int64, available: Int64?) -> Bool {
        guard let available else { return true }
        return available >= bytes + freeSpaceMargin
    }

    static func availableBytes(at url: URL) -> Int64? {
        let values = try? url.resourceValues(forKeys: [
            .volumeAvailableCapacityForImportantUsageKey
        ])
        return values?.volumeAvailableCapacityForImportantUsage
    }

    // MARK: - Removal

    enum RemovalError: Error, Equatable {
        /// Refused because the file is not in the download directory —
        /// which is what a model inside the app bundle is. Deleting it
        /// would break the signature, and the user did not put it there.
        case notRemovable
    }

    /// Delete a downloaded model.
    ///
    /// Resolved against the download directory rather than taking a path,
    /// so there is no arrangement of arguments that deletes something else.
    static func remove(_ filename: String, using fs: FileManager = .default) throws {
        let target = destination(for: filename).standardizedFileURL
        guard target.deletingLastPathComponent() == directory.standardizedFileURL else {
            throw RemovalError.notRemovable
        }
        try fs.removeItem(at: target)
    }

    /// Whether a model on disk is one this app is allowed to delete.
    ///
    /// Compared as paths, not as URLs. `deletingLastPathComponent()` on a
    /// file URL keeps a trailing slash on macOS 15's Foundation and drops
    /// it on later ones, and `URL ==` treats `…/models/` and `…/models` as
    /// different places — so on macOS 15 this returned `false` for every
    /// model the app itself had downloaded, and the trash button never
    /// appeared. `path` is the same string either way. Caught by CI, which
    /// runs on macOS 15 while development happens on newer.
    static func isRemovable(path: String) -> Bool {
        URL(fileURLWithPath: path).standardizedFileURL.deletingLastPathComponent().path
            == directory.standardizedFileURL.path
    }

    /// Discard leftover `.part` files.
    ///
    /// A download interrupted by a quit or a crash leaves one behind, and
    /// they are not small. Nothing resumes them: `URLSession`'s resume data
    /// does not outlive the process, so a stale partial is only ever
    /// wasted disk.
    static func discardPartials(using fs: FileManager = .default) {
        let contents = (try? fs.contentsOfDirectory(atPath: directory.path)) ?? []
        for name in contents where name.hasSuffix(".part") {
            try? fs.removeItem(at: directory.appendingPathComponent(name))
        }
    }
}
