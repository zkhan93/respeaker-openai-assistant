import Foundation
import XCTest

@testable import Raneen

/// The filesystem side of the download manager, against a real directory.
///
/// `RANEEN_MODEL_DIR` is what makes this possible — without it these cases
/// would have to write into `~/.cache/raneen/models`, which is the user's
/// own model library, and a test that deletes files there is a test nobody
/// should run twice.
///
/// Nothing here touches the network. What is being pinned is everything
/// *around* the transfer: which files count as installed, which ones this
/// app is allowed to delete, and that a download interrupted by a quit does
/// not leave a gigabyte behind.
final class ModelDownloaderTests: XCTestCase {

    private var directory: URL!

    /// A real catalogue entry, so `refresh()` is looking for a name it
    /// actually knows.
    private let known = "ggml-small.en-q5_1.bin"

    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("raneen-models-\(UUID().uuidString)")
        setenv("RANEEN_MODEL_DIR", directory.path, 1)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() {
        unsetenv("RANEEN_MODEL_DIR")
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    private func plant(_ name: String, bytes: Int = 64) {
        let body = Data(ModelInstall.ggmlMagic) + Data(count: bytes)
        try? body.write(to: directory.appendingPathComponent(name))
    }

    // MARK: - Where things go

    func testTheOverrideDecidesWhereModelsLive() {
        XCTAssertEqual(ModelInstall.directory.path, directory.path)
        XCTAssertEqual(
            ModelInstall.destination(for: known).path, directory.appendingPathComponent(known).path)
    }

    func testCreateDirectoryIsIdempotent() throws {
        try ModelInstall.createDirectory()
        try ModelInstall.createDirectory()
        XCTAssertTrue(FileManager.default.fileExists(atPath: directory.path))
    }

    // MARK: - What counts as installed

    func testAPlantedModelIsFoundWhereItWasPlanted() {
        XCTAssertFalse(ModelInstall.isInstalled(known))
        plant(known)
        XCTAssertEqual(
            ModelInstall.installedPath(for: known), directory.appendingPathComponent(known).path)
    }

    /// `refresh()` runs synchronously when called on the main queue, which
    /// XCTest is — so this asserts on the published value directly rather
    /// than waiting for a hop that is not going to happen.
    func testRefreshReportsCatalogueModelsOnly() {
        plant(known)
        plant("ggml-something-homemade.bin")

        let downloader = ModelDownloader()
        downloader.refresh()

        XCTAssertEqual(
            downloader.installed[known], directory.appendingPathComponent(known).path)
        XCTAssertNil(
            downloader.installed["ggml-something-homemade.bin"],
            "a model the catalogue does not list has no row to mark installed")
    }

    // MARK: - Deleting

    func testDeleteRemovesADownloadedModel() {
        plant(known)
        let downloader = ModelDownloader()
        XCTAssertTrue(downloader.canDelete(known))

        downloader.delete(known)
        XCTAssertFalse(ModelInstall.isInstalled(known))
    }

    /// Nothing installed means nothing deletable. The other half of this —
    /// that the copy inside the app bundle is selectable but *not* deletable
    /// — is `testOnlyDownloadedModelsAreRemovable`, which can state it
    /// without needing a signed bundle to exist.
    func testNothingInstalledMeansNothingToDelete() {
        let downloader = ModelDownloader()
        XCTAssertFalse(downloader.canDelete("ggml-base.en-q5_1.bin"))

        // And the no-op is real: a delete for a model that is not there must
        // not report a failure the user then has to dismiss.
        downloader.delete("ggml-base.en-q5_1.bin")
        XCTAssertNil(downloader.states["ggml-base.en-q5_1.bin"])
    }

    // MARK: - Leftovers

    /// A quit or a crash mid-download leaves a `.part` behind, and nothing
    /// resumes it: `URLSession`'s resume data does not outlive the process.
    /// At up to 3 GB each, letting them accumulate is a real cost.
    func testPartialsAreDiscardedAndFinishedModelsAreNot() {
        plant(known)
        plant("ggml-large-v3-q5_0.bin.part")

        ModelInstall.discardPartials()

        XCTAssertTrue(ModelInstall.isInstalled(known), "a finished model must survive the sweep")
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: directory.appendingPathComponent("ggml-large-v3-q5_0.bin.part").path))
    }

    func testConstructionSweepsPartials() {
        plant("ggml-medium.en-q5_0.bin.part")
        _ = ModelDownloader()
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: directory.appendingPathComponent("ggml-medium.en-q5_0.bin.part").path))
    }

    // MARK: - Starting

    /// Checked before any asynchronous work, so this asserts synchronously:
    /// a model already on disk must not begin a second download of itself.
    func testStartingAnInstalledModelDoesNothing() {
        plant(known)
        let downloader = ModelDownloader()
        downloader.start(ModelCatalog.model(named: known)!)
        XCTAssertTrue(downloader.states.isEmpty)
    }

    /// The pre-flight failure path, end to end. `RANEEN_MODEL_DIR` points
    /// below a regular file here, so `createDirectory` cannot succeed — and
    /// the refusal has to be visible in the same turn of the run loop as the
    /// click that caused it.
    func testAnUnusableDirectoryFailsImmediatelyAndSaysSo() throws {
        let file = directory.appendingPathComponent("not-a-directory")
        try Data("x".utf8).write(to: file)
        setenv("RANEEN_MODEL_DIR", file.appendingPathComponent("models").path, 1)

        let downloader = ModelDownloader()
        downloader.start(ModelCatalog.model(named: known)!)

        guard case .failed(let reason) = downloader.states[known] else {
            let got = String(describing: downloader.states[known])
            return XCTFail("expected an immediate failure, got \(got)")
        }
        XCTAssertTrue(reason.contains("Could not create"), reason)

        // And the row can go back to offering a download, rather than
        // showing that error until the window is closed.
        downloader.clear(known)
        XCTAssertNil(downloader.states[known])
    }
}
