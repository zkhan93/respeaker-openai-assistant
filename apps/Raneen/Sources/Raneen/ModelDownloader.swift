import Foundation

/// What a model row is doing right now.
///
/// Deliberately does not include "installed": that is a fact about the
/// filesystem, and a copy of it kept here would be the thing that goes
/// stale when a file is removed from underneath us.
enum DownloadState: Equatable {

    /// The task exists and the first bytes have not arrived. A separate
    /// state from `downloading(0, n)` because a stalled connection sits
    /// here, and "0 %" on a progress bar looks like a broken download
    /// rather than one still opening.
    case waiting

    case downloading(received: Int64, total: Int64)

    /// On disk, being hashed. Visible because it is not instant on a
    /// multi-gigabyte model, and an unexplained pause at 100 % reads as a
    /// hang.
    case verifying

    case failed(String)

    var isBusy: Bool {
        switch self {
        case .waiting, .downloading, .verifying: return true
        case .failed: return false
        }
    }

    /// `nil` when there is nothing honest to show — an indeterminate bar is
    /// the right answer for a server that sent no `content-length`.
    var fraction: Double? {
        guard case .downloading(let received, let total) = self, total > 0 else { return nil }
        return min(1, Double(received) / Double(total))
    }

    var detail: String {
        switch self {
        case .waiting:
            return "Starting…"
        case .downloading(let received, let total):
            let got = ByteCountFormatter.string(fromByteCount: received, countStyle: .file)
            guard total > 0 else { return got }
            let all = ByteCountFormatter.string(fromByteCount: total, countStyle: .file)
            return "\(got) of \(all)"
        case .verifying:
            return "Checking…"
        case .failed(let reason):
            return reason
        }
    }
}

/// Fetches whisper models into `~/.cache/raneen/models`.
///
/// **The point is that nobody has to visit Finder.** Both model caches live
/// under `~/.cache`, which is hidden, so the previous flow was: read a
/// README, run a shell script from a repo the user may not have, then find
/// a hidden directory in an open panel. The open panel stays as an escape
/// hatch for a model from somewhere else; it is no longer the only way in.
///
/// One `URLSession`, several concurrent tasks, all bookkeeping on a private
/// serial queue with publishing hopped to main. The alternative — a
/// `@MainActor` type — would put a 3 GB hash and thousands of progress
/// callbacks a second on the queue that draws the window.
final class ModelDownloader: NSObject, ObservableObject {

    @Published private(set) var states: [String: DownloadState] = [:]

    /// Catalogue filenames already on disk, mapped to where they are.
    ///
    /// Published rather than checked in `body`, because SwiftUI re-evaluates
    /// a view many times and this would be a directory scan on each pass.
    /// The *path* and not just the name, because a model can be the one
    /// inside the app bundle — selectable, but not ours to delete.
    @Published private(set) var installed: [String: String] = [:]

    /// Called on the main queue when a model has arrived, been verified and
    /// been given its real name. `AppDelegate` wires this to the settings
    /// so the new model is selected — someone who downloads a model wants
    /// to use it, and making them then find it in a picker is a step with
    /// no decision in it.
    var onInstalled: ((CatalogModel, URL) -> Void)?

    private let queue = DispatchQueue(label: "raneen.model-downloader")
    private var tasks: [String: URLSessionDownloadTask] = [:]

    /// Last byte count published per download, so progress is not
    /// republished for every 16 KB `URLSession` hands over. At 100 MB/s
    /// that callback fires thousands of times a second, and every one of
    /// them would be a main-queue hop and a SwiftUI invalidation.
    private var published: [String: Int64] = [:]

    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.default
        // Idle timeout, not total: a large model legitimately takes an
        // hour on a slow line, and the default 7-day resource timeout is
        // the right shape here. What must not happen is a dead connection
        // sitting at 40 % forever.
        configuration.timeoutIntervalForRequest = 60
        configuration.waitsForConnectivity = true

        let operations = OperationQueue()
        operations.maxConcurrentOperationCount = 1
        operations.underlyingQueue = queue
        return URLSession(configuration: configuration, delegate: self, delegateQueue: operations)
    }()

    override init() {
        super.init()
        // Anything left by a quit mid-download is dead weight — nothing
        // resumes it — and models are large enough that leaving them to
        // accumulate is a real cost.
        ModelInstall.discardPartials()
        refresh()
    }

    /// Re-read what is on disk. A couple of `stat`s per catalogue entry.
    func refresh() {
        var present: [String: String] = [:]
        for model in ModelCatalog.all {
            if let path = ModelInstall.installedPath(for: model.filename) {
                present[model.filename] = path
            }
        }
        if Thread.isMainThread {
            installed = present
        } else {
            DispatchQueue.main.async { self.installed = present }
        }
    }

    // MARK: - Commands

    func start(_ model: CatalogModel) {
        guard !ModelInstall.isInstalled(model.filename) else { return }

        do {
            try ModelInstall.createDirectory()
        } catch {
            publish(.failed("Could not create \(ModelInstall.directory.path)."), for: model.filename)
            return
        }

        // Checked before starting rather than discovered at 90 %: the
        // failure at that point is an out-of-space error from the OS, on a
        // volume we just filled.
        let available = ModelInstall.availableBytes(at: ModelInstall.directory)
        guard ModelInstall.hasRoom(for: model.bytes, available: available) else {
            let free = ByteCountFormatter.string(
                fromByteCount: available ?? 0, countStyle: .file)
            publish(
                .failed("Not enough disk space — \(model.sizeDescription) needed, \(free) free."),
                for: model.filename)
            return
        }

        queue.async {
            guard self.tasks[model.filename] == nil else { return }
            let task = self.session.downloadTask(with: model.url)
            // The only handle a delegate callback gets back to which model
            // this is. Keyed by filename because that is unique in the
            // catalogue and is also the destination.
            task.taskDescription = model.filename
            self.tasks[model.filename] = task
            self.published[model.filename] = 0
            self.publish(.waiting, for: model.filename)
            task.resume()
            Log.app.info("downloading \(model.filename) (\(model.sizeDescription))")
        }
    }

    /// Cancel and discard. There is no resume: `URLSession`'s resume data
    /// does not survive the process, and a Resume button that silently
    /// restarts from zero is worse than no button.
    func cancel(_ filename: String) {
        queue.async {
            self.tasks[filename]?.cancel()
        }
    }

    /// Whether this app put the file there, and so may take it away.
    ///
    /// Asked of the download directory specifically, not of wherever the
    /// model was found. A catalogue model present only inside the app bundle
    /// answers false: deleting it would break the code signature, and nobody
    /// chose to install it.
    func canDelete(_ filename: String) -> Bool {
        FileManager.default.fileExists(atPath: ModelInstall.destination(for: filename).path)
    }

    func delete(_ filename: String) {
        guard canDelete(filename) else { return }
        do {
            try ModelInstall.remove(filename)
            Log.app.info("removed \(filename)")
        } catch {
            publish(.failed("Could not delete this model."), for: filename)
            return
        }
        clear(filename)
        refresh()
    }

    /// Forget a `failed` state, so a row goes back to offering a download.
    func clear(_ filename: String) {
        set(nil, for: filename)
    }

    // MARK: - Publishing

    private func publish(_ state: DownloadState, for filename: String) {
        set(state, for: filename)
    }

    /// Applied immediately when already on the main queue, hopped otherwise.
    ///
    /// The immediate path is not an optimisation: a pre-flight refusal —
    /// no disk space, no writable directory — is raised while handling the
    /// click, and a button that has to wait for a turn of the run loop to
    /// say "no" reads as a button that did nothing.
    private func set(_ state: DownloadState?, for filename: String) {
        if Thread.isMainThread {
            states[filename] = state
        } else {
            DispatchQueue.main.async { self.states[filename] = state }
        }
    }
}

// MARK: - URLSessionDownloadDelegate

extension ModelDownloader: URLSessionDownloadDelegate {

    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64
    ) {
        guard let filename = downloadTask.taskDescription else { return }

        // Roughly every half percent, plus the first byte. Enough to look
        // continuous, few enough not to matter.
        let step = max(totalBytesExpectedToWrite / 200, 1 << 20)
        let last = published[filename] ?? 0
        guard totalBytesWritten - last >= step || last == 0 else { return }
        published[filename] = totalBytesWritten

        publish(
            .downloading(received: totalBytesWritten, total: totalBytesExpectedToWrite),
            for: filename)
    }

    func urlSession(
        _ session: URLSession, downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        guard let filename = downloadTask.taskDescription,
            let model = ModelCatalog.model(named: filename)
        else { return }

        // A 404 or an HTML error page is a *successful* download of the
        // wrong thing, so the status has to be read explicitly.
        if let response = downloadTask.response as? HTTPURLResponse, response.statusCode != 200 {
            tasks[filename] = nil
            publish(.failed("The server answered \(response.statusCode)."), for: filename)
            return
        }

        // **Synchronously, here.** `URLSession` deletes `location` the
        // moment this method returns, so anything asynchronous would race
        // it and lose — occasionally, and only on fast disks.
        let partial = ModelInstall.partial(for: filename)
        try? FileManager.default.removeItem(at: partial)
        do {
            try FileManager.default.moveItem(at: location, to: partial)
        } catch {
            tasks[filename] = nil
            publish(.failed("Could not write to \(ModelInstall.directory.path)."), for: filename)
            return
        }

        publish(.verifying, for: filename)

        // Off the delegate queue: hashing 3 GB takes seconds, and holding
        // this queue would freeze every other download's progress.
        DispatchQueue.global(qos: .utility).async {
            let rejection = ModelInstall.inspect(partial, against: model)
            self.queue.async {
                self.tasks[filename] = nil
                if let rejection {
                    try? FileManager.default.removeItem(at: partial)
                    Log.app.error("rejected \(filename): \(rejection.message)")
                    self.publish(.failed(rejection.message), for: filename)
                    return
                }

                let destination = ModelInstall.destination(for: filename)
                do {
                    try? FileManager.default.removeItem(at: destination)
                    try FileManager.default.moveItem(at: partial, to: destination)
                } catch {
                    self.publish(.failed("Could not install this model."), for: filename)
                    return
                }

                Log.app.info("installed \(filename)")
                DispatchQueue.main.async {
                    self.states[filename] = nil
                    self.installed[filename] = destination.path
                    self.onInstalled?(model, destination)
                }
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?)
    {
        guard let filename = task.taskDescription else { return }
        // Success is owned by `didFinishDownloadingTo`, which has already
        // run by now — clearing state here would undo its verdict.
        guard let error else { return }

        tasks[filename] = nil
        published[filename] = nil

        if (error as NSError).code == NSURLErrorCancelled {
            clear(filename)
            return
        }
        Log.app.error("download failed \(filename): \(error.localizedDescription)")
        publish(.failed(error.localizedDescription), for: filename)
    }
}
