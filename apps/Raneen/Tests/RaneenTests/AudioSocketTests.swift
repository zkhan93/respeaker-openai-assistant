import Foundation
import XCTest

@testable import Raneen

/// The AF_UNIX channel carrying frames to the helper.
///
/// The helper end is stood in for by a plain socket here, so these prove
/// the transport without spawning Python or loading a model.
final class AudioSocketTests: XCTestCase {

    private var sockets: [AudioSocket] = []

    override func tearDown() {
        sockets.forEach { $0.close() }
        sockets = []
        super.tearDown()
    }

    private func makeSocket(path: String = "/tmp/raneen-test-\(getpid())-\(UUID().uuidString.prefix(8)).sock")
        -> AudioSocket
    {
        let socket = AudioSocket(path: path)
        sockets.append(socket)
        return socket
    }

    /// Stand in for the helper: connect and read.
    private func connectClient(to path: String) -> Int32 {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let size = MemoryLayout.size(ofValue: addr.sun_path)
        withUnsafeMutablePointer(to: &addr.sun_path) { raw in
            raw.withMemoryRebound(to: CChar.self, capacity: size) { dst in
                _ = strncpy(dst, path, size - 1)
            }
        }
        let connected = withUnsafePointer(to: &addr) { raw in
            raw.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connected != 0 {
            Darwin.close(fd)
            return -1
        }
        return fd
    }

    // MARK: - Path constraints

    /// sun_path is 104 bytes on macOS, and the resulting failure says
    /// nothing about audio — so it is caught here where it can.
    func testAnOverlongPathIsRejectedWithAUsefulMessage() {
        let socket = AudioSocket(path: "/tmp/" + String(repeating: "x", count: 200) + ".sock")
        XCTAssertThrowsError(try socket.listen()) { error in
            XCTAssertTrue(
                "\(error)".contains("AF_UNIX allows"),
                "the error should name the real constraint, got: \(error)"
            )
        }
    }

    func testTheDefaultPathFitsComfortably() {
        XCTAssertLessThanOrEqual(
            AudioSocket.defaultPath().utf8.count, AudioSocket.maxPathLength)
    }

    func testTheDefaultPathIsUniquePerProcess() {
        // Two Raneens running during development must not fight over one
        // socket; a stale file from a crashed run must not be adopted.
        XCTAssertTrue(AudioSocket.defaultPath().contains("\(getpid())"))
    }

    // MARK: - Lifecycle

    func testListeningCreatesTheSocketFile() throws {
        let socket = makeSocket()
        try socket.listen()
        XCTAssertTrue(FileManager.default.fileExists(atPath: socket.path))
    }

    func testListeningTwiceOverAStaleFileSucceeds() throws {
        // A crashed run leaves the file behind; bind() would fail with
        // EADDRINUSE if it were not unlinked first.
        let path = "/tmp/raneen-stale-\(getpid()).sock"
        let first = makeSocket(path: path)
        try first.listen()
        first.close()

        FileManager.default.createFile(atPath: path, contents: Data())
        let second = makeSocket(path: path)
        XCTAssertNoThrow(try second.listen())
    }

    func testClosingRemovesTheSocketFile() throws {
        let socket = makeSocket()
        try socket.listen()
        let path = socket.path
        socket.close()
        XCTAssertFalse(FileManager.default.fileExists(atPath: path))
    }

    // MARK: - Carrying audio

    func testNothingIsSentBeforeTheHelperConnects() throws {
        let socket = makeSocket()
        try socket.listen()
        XCTAssertFalse(socket.isConnected)
        // Dropping is correct: frames arrive every 80 ms regardless, and
        // blocking Core Audio's thread to wait for a reader would be worse
        // than losing audio nobody is listening to.
        XCTAssertFalse(socket.send(Data([1, 2, 3, 4])))
    }

    func testFramesReachAConnectedHelperIntact() throws {
        let socket = makeSocket()
        try socket.listen()
        socket.acceptInBackground()

        let client = connectClient(to: socket.path)
        XCTAssertGreaterThanOrEqual(client, 0, "could not connect to the socket")
        defer { Darwin.close(client) }

        // accept() lands on a background queue.
        let connected = expectation(description: "helper connected")
        DispatchQueue.global().async {
            while !socket.isConnected { usleep(1000) }
            connected.fulfill()
        }
        wait(for: [connected], timeout: 5)

        let payload = Data((0..<2560).map { UInt8($0 % 256) })
        XCTAssertTrue(socket.send(payload))

        var received = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while received.count < payload.count {
            let n = read(client, &buffer, buffer.count)
            if n <= 0 { break }
            received.append(contentsOf: buffer[0..<n])
        }
        XCTAssertEqual(received, payload, "audio changed crossing the socket")
    }

    func testSendingToADepartedHelperFailsAndDisconnects() throws {
        let socket = makeSocket()
        try socket.listen()
        socket.acceptInBackground()

        let client = connectClient(to: socket.path)
        XCTAssertGreaterThanOrEqual(client, 0)

        let connected = expectation(description: "helper connected")
        DispatchQueue.global().async {
            while !socket.isConnected { usleep(1000) }
            connected.fulfill()
        }
        wait(for: [connected], timeout: 5)

        Darwin.close(client)  // the helper dies

        // The first write may land in the kernel buffer; the socket must
        // notice by the second rather than writing into the void forever.
        // Crucially this must not raise SIGPIPE and kill the app.
        var stillConnected = true
        for _ in 0..<50 where stillConnected {
            stillConnected = socket.send(Data(repeating: 0, count: 2560))
            usleep(2000)
        }
        XCTAssertFalse(stillConnected, "a dead helper was never noticed")
        XCTAssertFalse(socket.isConnected)
    }
}
