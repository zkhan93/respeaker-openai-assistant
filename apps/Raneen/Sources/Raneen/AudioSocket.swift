import Foundation

/// AF_UNIX rendezvous carrying audio frames down to the helper.
///
/// ## Why a socket rather than a descriptor
///
/// Foundation's `Process` exposes only stdin, stdout and stderr. Handing
/// a child an arbitrary fd means dropping to `posix_spawn` and
/// reimplementing process lifecycle, which is a lot to give up for one
/// channel.
///
/// A named FIFO looks like the obvious alternative and is a trap: opening
/// one blocking waits for the peer, and opening it non-blocking reports
/// EOF before the writer arrives — which is indistinguishable from the
/// disconnect EOF is *supposed* to signal. AF_UNIX has neither problem,
/// and unlike a TCP port it triggers no macOS firewall prompt, the same
/// reason AD-15 chose a pipe over ZMQ.
///
/// We listen and the helper connects, in that order — so the socket
/// exists before the helper is spawned and its connect never races.
final class AudioSocket {

    /// `sun_path` is 104 bytes on macOS. A path that overflows fails with
    /// "AF_UNIX path too long", which says nothing whatsoever about audio,
    /// so the limit is enforced here where the message can be useful.
    static let maxPathLength = 100

    enum Failure: Error, CustomStringConvertible {
        case pathTooLong(String)
        case cannotCreate(String)

        var description: String {
            switch self {
            case .pathTooLong(let path):
                return "audio socket path is \(path.utf8.count) bytes, over the "
                    + "\(AudioSocket.maxPathLength) AF_UNIX allows: \(path)"
            case .cannotCreate(let reason):
                return "could not create the audio socket: \(reason)"
            }
        }
    }

    let path: String
    private var listenFD: Int32 = -1
    private var clientFD: Int32 = -1
    private let lock = NSLock()
    private var accepting = false

    /// Short by construction — see `maxPathLength`. The pid keeps two
    /// instances from colliding, which matters during development when a
    /// crashed run may not have cleaned up.
    static func defaultPath() -> String {
        "/tmp/raneen-\(getpid()).sock"
    }

    init(path: String = AudioSocket.defaultPath()) {
        self.path = path
    }

    /// Bind and listen. Call **before** spawning the helper.
    func listen() throws {
        guard path.utf8.count <= Self.maxPathLength else { throw Failure.pathTooLong(path) }

        unlink(path)  // a previous run may have died without cleaning up

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.cannotCreate("socket() failed: \(errno)") }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let size = MemoryLayout.size(ofValue: addr.sun_path)
        withUnsafeMutablePointer(to: &addr.sun_path) { raw in
            raw.withMemoryRebound(to: CChar.self, capacity: size) { dst in
                _ = strncpy(dst, path, size - 1)
            }
        }

        let bound = withUnsafePointer(to: &addr) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else {
            Darwin.close(fd)
            throw Failure.cannotCreate("bind() failed: \(errno)")
        }
        // Only our own helper ever connects, so nobody else needs access.
        chmod(path, 0o600)

        guard Darwin.listen(fd, 1) == 0 else {
            Darwin.close(fd)
            unlink(path)
            throw Failure.cannotCreate("listen() failed: \(errno)")
        }

        listenFD = fd
    }

    /// Accept the helper's connection on a background queue.
    ///
    /// `accept` blocks, and the helper cannot connect until it has
    /// finished loading a Whisper model — which is seconds. Doing this on
    /// the main queue would freeze the menu bar for the whole of startup.
    func acceptInBackground() {
        lock.lock()
        guard listenFD >= 0, !accepting else {
            lock.unlock()
            return
        }
        accepting = true
        let fd = listenFD
        lock.unlock()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let client = accept(fd, nil, nil)
            guard let self else {
                if client >= 0 { Darwin.close(client) }
                return
            }
            if client >= 0 {
                // Without this, writing to a socket the helper has closed
                // raises SIGPIPE and kills the whole app — and the helper
                // dying is an ordinary event, not a fatal one.
                var on: Int32 = 1
                setsockopt(
                    client, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size)
                )
            } else {
                Log.audio.error("audio socket accept failed: errno \(errno)")
            }
            self.lock.lock()
            self.clientFD = client
            self.lock.unlock()
            // The path only had to survive until the helper connected;
            // unlinking now means nothing is left behind if we crash.
            unlink(self.path)
        }
    }

    var isConnected: Bool {
        lock.lock()
        defer { lock.unlock() }
        return clientFD >= 0
    }

    /// Write one converted buffer. Silently drops if nobody is connected.
    ///
    /// Dropping is right for audio: frames arrive every 80 ms whether or
    /// not the far end is ready, and blocking the capture callback to wait
    /// for a reader would stall Core Audio's real-time thread. If the
    /// helper is not listening yet there is nothing worth saying anyway.
    @discardableResult
    func send(_ data: Data) -> Bool {
        lock.lock()
        let fd = clientFD
        lock.unlock()
        guard fd >= 0 else { return false }

        var sent = 0
        let ok = data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return false }
            while sent < data.count {
                // Safe against a vanished helper because SO_NOSIGPIPE was
                // set on this socket at accept time; write() returns EPIPE
                // instead of killing the process.
                let n = write(fd, base.advanced(by: sent), data.count - sent)
                if n > 0 {
                    sent += n
                    continue
                }
                if n < 0 && errno == EINTR { continue }
                return false
            }
            return true
        }

        if !ok {
            // The helper went away. Close so `isConnected` reports the
            // truth rather than us writing into a dead descriptor forever.
            lock.lock()
            if clientFD >= 0 {
                Darwin.close(clientFD)
                clientFD = -1
            }
            lock.unlock()
        }
        return ok
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        if clientFD >= 0 {
            Darwin.close(clientFD)
            clientFD = -1
        }
        if listenFD >= 0 {
            Darwin.close(listenFD)
            listenFD = -1
        }
        accepting = false
        unlink(path)
    }

    deinit { close() }
}
