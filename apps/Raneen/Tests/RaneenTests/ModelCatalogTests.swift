import CryptoKit
import Foundation
import XCTest

@testable import Raneen

/// The catalogue and the checks a downloaded model has to pass.
///
/// Worth testing carefully because the failure this guards against is not a
/// crash. A model that is half-downloaded, or a login page saved under a
/// `.bin` name, loads far enough to produce a ggml error that reads like a
/// broken build — and the previous version of "a setting the core could not
/// use" presented to the user as "dictation is broken".
final class ModelCatalogTests: XCTestCase {

    // MARK: - The catalogue is well formed

    func testFilenamesAreUnique() {
        let names = ModelCatalog.all.map(\.filename)
        XCTAssertEqual(Set(names).count, names.count, "a duplicate filename would shadow a model")
    }

    func testEveryEntryIsANamedGgmlModel() {
        for model in ModelCatalog.all {
            XCTAssertTrue(model.filename.hasPrefix("ggml-"), model.filename)
            XCTAssertTrue(model.filename.hasSuffix(".bin"), model.filename)
            XCTAssertFalse(model.title.isEmpty, model.filename)
            XCTAssertFalse(model.detail.isEmpty, "\(model.filename) has no stated tradeoff")
            XCTAssertGreaterThan(model.bytes, 0, model.filename)
        }
    }

    /// A digest that is the wrong length or the wrong case silently never
    /// matches, so every download of that model would fail verification
    /// with "damaged" — a bug that looks like a network problem.
    func testDigestsAreLowercaseHexOfTheRightLength() {
        for model in ModelCatalog.all {
            XCTAssertEqual(model.sha256.count, 64, model.filename)
            XCTAssertEqual(model.sha256, model.sha256.lowercased(), model.filename)
            XCTAssertTrue(
                model.sha256.allSatisfy { $0.isHexDigit && !$0.isUppercase },
                "\(model.filename): \(model.sha256)")
        }
    }

    func testUrlsAreHttpsAndPointAtTheirOwnFile() {
        for model in ModelCatalog.all {
            XCTAssertEqual(model.url.scheme, "https", model.filename)
            XCTAssertEqual(model.url.host, "huggingface.co", model.filename)
            XCTAssertEqual(model.url.lastPathComponent, model.filename)
        }
    }

    /// **The coupling that produces confident nonsense.** An `.en` model
    /// given other speech does not fail — it transliterates into English
    /// phonemes — so `WhisperModel.isEnglishOnly`, which the language
    /// picker keys off, has to agree with the flag the catalogue advertises.
    /// A "Small" row that quietly cannot do Hindi is the exact mistake the
    /// settings window is designed to make impossible.
    func testMultilingualFlagAgreesWithTheFilename() {
        for model in ModelCatalog.all {
            XCTAssertEqual(
                WhisperModel(path: "/models/\(model.filename)").isEnglishOnly,
                !model.multilingual,
                "\(model.filename) is advertised as "
                    + (model.multilingual ? "multilingual" : "English-only"))
        }
    }

    func testGroupsPartitionTheCatalogue() {
        XCTAssertEqual(
            ModelCatalog.englishOnly.count + ModelCatalog.multilingual.count,
            ModelCatalog.all.count)
        XCTAssertFalse(ModelCatalog.englishOnly.isEmpty)
        XCTAssertFalse(ModelCatalog.multilingual.isEmpty)
    }

    func testLookupByFilename() {
        XCTAssertEqual(ModelCatalog.model(named: "ggml-base.en-q5_1.bin")?.title, "Base · English")
        XCTAssertNil(ModelCatalog.model(named: "ggml-nonexistent.bin"))
    }

    /// The model bundled inside the app has to be in the catalogue under
    /// the name the Makefile ships, or the row for the model already in use
    /// would offer to download it again.
    func testTheBundledModelIsListed() {
        XCTAssertNotNil(ModelCatalog.model(named: "ggml-base.en-q5_1.bin"))
    }

    // MARK: - Digests

    func testSha256MatchesAKnownDigest() throws {
        let file = try write(Data("raneen".utf8))
        XCTAssertEqual(
            try ModelInstall.sha256(of: file),
            "e0b675c3b17f51ddb4f30f32199bbd219a636b82d90bac96e38adbde87d36747")
    }

    /// The chunk loop, across its own boundary.
    ///
    /// `sha256(of:)` reads 1 MB at a time so that hashing a 3 GB model does
    /// not load it — and a chunked hash that drops or repeats a block still
    /// produces a perfectly plausible digest. Compared against the one-shot
    /// implementation over the same bytes, which is the only reference that
    /// would catch that.
    func testSha256IsChunkBoundaryCorrect() throws {
        // 2.5 MB: two full chunks and a short one.
        let bytes = Data((0..<2_500_000).map { UInt8($0 % 251) })

        let expected = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let file = try write(bytes)
        XCTAssertEqual(try ModelInstall.sha256(of: file), expected)
    }

    func testSha256OfAnEmptyFile() throws {
        let file = try write(Data())
        XCTAssertEqual(
            try ModelInstall.sha256(of: file),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    }

    // MARK: - The magic bytes

    func testMagicAcceptsAGgmlHeader() throws {
        let file = try write(Data(ModelInstall.ggmlMagic) + Data(count: 64))
        XCTAssertTrue(ModelInstall.hasGgmlMagic(at: file))
    }

    /// The download that matters most. A captive portal, an expired signed
    /// URL or a moved file all answer with a complete, valid HTML document
    /// — a *successful* transfer of the wrong thing.
    func testMagicRejectsAnHtmlPage() throws {
        let file = try write(Data("<!DOCTYPE html><html><head><title>Sign in</title>".utf8))
        XCTAssertFalse(ModelInstall.hasGgmlMagic(at: file))
    }

    func testMagicRejectsAFileTooShortToHaveOne() throws {
        XCTAssertFalse(ModelInstall.hasGgmlMagic(at: try write(Data([0x6c, 0x6d]))))
    }

    func testMagicRejectsAMissingFile() {
        XCTAssertFalse(
            ModelInstall.hasGgmlMagic(at: URL(fileURLWithPath: "/nonexistent/ggml-x.bin")))
    }

    // MARK: - Inspection

    /// Built for a real temporary file, so the accept case is the whole
    /// chain — size, magic and digest — rather than three separate checks
    /// that have never been run together.
    private func catalogEntry(for file: URL) throws -> CatalogModel {
        let size = try FileManager.default.attributesOfItem(atPath: file.path)[.size] as? Int64
        return CatalogModel(
            filename: "ggml-test.bin",
            title: "Test",
            bytes: size ?? 0,
            sha256: try ModelInstall.sha256(of: file),
            multilingual: false,
            detail: "for tests")
    }

    func testInspectAcceptsAnIntactModel() throws {
        let file = try write(Data(ModelInstall.ggmlMagic) + Data(count: 4096))
        XCTAssertNil(ModelInstall.inspect(file, against: try catalogEntry(for: file)))
    }

    func testInspectRejectsTheWrongSize() throws {
        let file = try write(Data(ModelInstall.ggmlMagic) + Data(count: 4096))
        let actual = try catalogEntry(for: file)
        let model = CatalogModel(
            filename: actual.filename, title: actual.title, bytes: actual.bytes + 1,
            sha256: actual.sha256, multilingual: false, detail: actual.detail)

        XCTAssertEqual(
            ModelInstall.inspect(file, against: model),
            .wrongSize(expected: actual.bytes + 1, got: actual.bytes))
    }

    func testInspectRejectsTheWrongDigest() throws {
        let file = try write(Data(ModelInstall.ggmlMagic) + Data(count: 4096))
        let actual = try catalogEntry(for: file)
        let model = CatalogModel(
            filename: actual.filename, title: actual.title, bytes: actual.bytes,
            sha256: String(repeating: "0", count: 64), multilingual: false, detail: actual.detail)

        XCTAssertEqual(ModelInstall.inspect(file, against: model), .wrongDigest)
    }

    /// Size and digest are checked *before* the file is renamed, so this is
    /// the state a truncated download is actually in — same name, fewer
    /// bytes, still a valid ggml header.
    func testInspectRejectsATruncatedModel() throws {
        let full = Data(ModelInstall.ggmlMagic) + Data(count: 4096)
        let file = try write(full)
        let model = try catalogEntry(for: file)

        let truncated = try write(full.prefix(2048))
        guard case .wrongSize = ModelInstall.inspect(truncated, against: model) else {
            return XCTFail("a truncated model was not rejected on size")
        }
    }

    func testRejectionMessagesAreForHumans() {
        for rejection: ModelInstall.Rejection in [
            .wrongSize(expected: 1024, got: 512), .notAModel, .wrongDigest,
        ] {
            XCTAssertFalse(rejection.message.isEmpty)
            XCTAssertTrue(
                rejection.message.hasSuffix("."), "\(rejection.message) is not a sentence")
        }
    }

    // MARK: - Room on disk

    func testRoomNeedsTheModelPlusHeadroom() {
        let size: Int64 = 1_000_000_000
        XCTAssertTrue(
            ModelInstall.hasRoom(for: size, available: size + ModelInstall.freeSpaceMargin))
        XCTAssertFalse(
            ModelInstall.hasRoom(for: size, available: size + ModelInstall.freeSpaceMargin - 1))
        XCTAssertFalse(ModelInstall.hasRoom(for: size, available: size))
    }

    /// A volume that will not report its free space is not a reason to
    /// refuse — trying and failing is better than blocking on a number we
    /// could not read.
    func testRoomIsAssumedWhenTheVolumeWillNotSay() {
        XCTAssertTrue(ModelInstall.hasRoom(for: 3_000_000_000, available: nil))
    }

    // MARK: - Paths and removal

    func testPartialSitsBesideItsDestinationWithAPartSuffix() {
        let destination = ModelInstall.destination(for: "ggml-base.en-q5_1.bin")
        let partial = ModelInstall.partial(for: "ggml-base.en-q5_1.bin")

        XCTAssertEqual(partial.path, destination.path + ".part")
        XCTAssertEqual(partial.deletingLastPathComponent(), destination.deletingLastPathComponent())
    }

    func testDestinationIsInTheDownloadDirectory() {
        for model in ModelCatalog.all {
            XCTAssertEqual(
                ModelInstall.destination(for: model.filename).deletingLastPathComponent()
                    .standardizedFileURL,
                ModelInstall.directory.standardizedFileURL)
        }
    }

    /// Removal resolves a filename against the download directory, so no
    /// argument can reach outside it. Checked with a traversal because
    /// "delete the model" and "delete a file" must not be the same call.
    func testRemovalRefusesToLeaveTheDownloadDirectory() {
        XCTAssertThrowsError(try ModelInstall.remove("../../../etc/hosts")) { error in
            XCTAssertEqual(error as? ModelInstall.RemovalError, .notRemovable)
        }
    }

    /// A model inside the app bundle is signed and read-only, and the user
    /// did not put it there — so the UI must not offer to delete it.
    func testOnlyDownloadedModelsAreRemovable() {
        XCTAssertTrue(
            ModelInstall.isRemovable(
                path: ModelInstall.destination(for: "ggml-small.en-q5_1.bin").path))
        XCTAssertFalse(
            ModelInstall.isRemovable(
                path: "/Applications/Raneen.app/Contents/Resources/helper/ggml-base.en-q5_1.bin"))
        XCTAssertFalse(ModelInstall.isRemovable(path: "/tmp/ggml-base.en-q5_1.bin"))
    }

    // MARK: - What a row shows

    func testProgressIsIndeterminateWithoutATotal() {
        XCTAssertNil(DownloadState.waiting.fraction)
        XCTAssertNil(DownloadState.verifying.fraction)
        // `content-length` absent: `URLSession` reports -1, and a bar drawn
        // from that would run backwards.
        XCTAssertNil(DownloadState.downloading(received: 500, total: -1).fraction)
        XCTAssertNil(DownloadState.downloading(received: 500, total: 0).fraction)
    }

    func testProgressIsAFractionAndNeverExceedsOne() {
        XCTAssertEqual(DownloadState.downloading(received: 50, total: 200).fraction, 0.25)
        XCTAssertEqual(DownloadState.downloading(received: 300, total: 200).fraction, 1)
    }

    func testOnlyAFailedStateIsNotBusy() {
        XCTAssertTrue(DownloadState.waiting.isBusy)
        XCTAssertTrue(DownloadState.downloading(received: 1, total: 2).isBusy)
        XCTAssertTrue(DownloadState.verifying.isBusy)
        XCTAssertFalse(DownloadState.failed("nope").isBusy)
    }

    func testDetailNamesBothSidesOfTheTransfer() {
        let detail = DownloadState.downloading(received: 1_000_000, total: 4_000_000).detail
        XCTAssertTrue(detail.contains("of"), detail)
        XCTAssertEqual(DownloadState.failed("Disk full.").detail, "Disk full.")
    }

    // MARK: -

    private func write(_ bytes: Data) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("raneen-model-test-\(UUID().uuidString)")
        try bytes.write(to: url)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
