import XCTest

@testable import Raneen

/// Chunking is the part of text insertion that can silently corrupt
/// output. Everything else either works or visibly does nothing.
final class TextInserterTests: XCTestCase {

    private let limit = 16

    private func utf16Counts(_ chunks: [String]) -> [Int] {
        chunks.map { $0.utf16.count }
    }

    func testChunksReassembleToTheOriginal() {
        let text = "The quick brown fox jumps over the lazy dog, repeatedly and at length."
        XCTAssertEqual(TextInserter.chunks(of: text).joined(), text)
    }

    func testShortTextIsASingleChunk() {
        XCTAssertEqual(TextInserter.chunks(of: "hello"), ["hello"])
    }

    func testEmptyTextProducesNoChunks() {
        XCTAssertEqual(TextInserter.chunks(of: ""), [])
    }

    func testNoChunkExceedsTheLimit() {
        let text = String(repeating: "abcdefghij", count: 20)
        for count in utf16Counts(TextInserter.chunks(of: text)) {
            XCTAssertLessThanOrEqual(count, limit)
        }
    }

    /// The bug this guards: chunking by UTF-16 index would cut an emoji's
    /// surrogate pair in half, and neither half is valid text.
    func testEmojiSurrogatePairsAreNeverSplit() {
        let text = "ok 👍🏽 done 🎉 and 👨‍👩‍👧‍👦 family"
        let chunks = TextInserter.chunks(of: text)

        XCTAssertEqual(chunks.joined(), text)
        for chunk in chunks {
            // A split surrogate pair round-trips through UTF-8 as U+FFFD.
            XCTAssertFalse(chunk.unicodeScalars.contains { $0 == "\u{FFFD}" },
                           "chunk contains a replacement character: \(chunk)")
        }
    }

    func testAGraphemeLargerThanTheLimitStillSurvives() {
        // The family emoji is 11 UTF-16 units; a flag sequence can be more.
        // A chunk may exceed the limit rather than break a character —
        // splitting is never the right answer.
        let text = "👨‍👩‍👧‍👦"
        let chunks = TextInserter.chunks(of: text)
        XCTAssertEqual(chunks.joined(), text)
        XCTAssertEqual(chunks.count, 1)
    }

    func testNonAsciiTextIsPreserved() {
        let text = "café — naïve — 日本語のテキストです"
        XCTAssertEqual(TextInserter.chunks(of: text).joined(), text)
    }

    func testNewlinesAndPunctuationSurvive() {
        let text = "First line.\nSecond line — with an em dash, \"quotes\", and 'apostrophes'."
        XCTAssertEqual(TextInserter.chunks(of: text).joined(), text)
    }

    func testTextExactlyAtTheLimitIsOneChunk() {
        let text = String(repeating: "a", count: limit)
        XCTAssertEqual(TextInserter.chunks(of: text), [text])
    }

    func testTextOneOverTheLimitSplits() {
        let text = String(repeating: "a", count: limit + 1)
        let chunks = TextInserter.chunks(of: text)
        XCTAssertEqual(chunks.count, 2)
        XCTAssertEqual(chunks.joined(), text)
    }

    // MARK: - Behaviour flags

    func testTrailingSpaceIsOnByDefault() {
        // Consecutive utterances otherwise arrive as "helloworld".
        XCTAssertTrue(TextInserter().appendTrailingSpace)
    }

    func testTypingIsOnByDefault() {
        XCTAssertTrue(TextInserter().isEnabled)
    }
}
