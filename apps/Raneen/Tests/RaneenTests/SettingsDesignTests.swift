import SwiftUI
import XCTest

@testable import Raneen

/// The parts of the settings window's design that are not a matter of taste.
///
/// A layout can only be judged by looking at it, and none of these tests
/// try to. What they cover is the two places where a redesign can be
/// silently wrong: the strings that translate the core's units into
/// something a person reads, and the arithmetic that has to hold for the
/// window's fixed width to leave room for a label.
final class SettingsDesignTests: XCTestCase {

    /// Only the hosting tests need it, but `SettingsModel` persists on every
    /// edit — so any one built here writes `raneen.*` keys wherever it is
    /// pointed, and the runner's own domain is not somewhere to leave them.
    private static let suite = "raneen.tests.settingsdesign"

    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        defaults = UserDefaults(suiteName: Self.suite)
        defaults.removePersistentDomain(forName: Self.suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: Self.suite)
        defaults = nil
        super.tearDown()
    }

    // MARK: - Frames as time

    /// The values the recommended timings actually produce, since those are
    /// the numbers a user sees the moment the window opens. 8 frames is
    /// push-to-talk's silence, 25 is a wake word's, 3 and 10 are the
    /// pre-rolls.
    func testTheRecommendedTimingsReadAsTime() {
        XCTAssertEqual(SettingsFormat.duration(frames: 8), "640 ms")
        XCTAssertEqual(SettingsFormat.duration(frames: 25), "2.0 s")
        XCTAssertEqual(SettingsFormat.duration(frames: 3), "240 ms")
        XCTAssertEqual(SettingsFormat.duration(frames: 10), "800 ms")
    }

    /// One second is the switch from milliseconds to seconds, and both
    /// sides of it are reachable with the stepper.
    func testTheUnitChangesAtOneSecond() {
        XCTAssertEqual(SettingsFormat.duration(frames: 12), "960 ms")
        XCTAssertEqual(SettingsFormat.duration(frames: 13), "1.0 s")
    }

    /// Pre-roll can be stepped to zero, and "0 ms" invites the question of
    /// whether zero milliseconds of audio is still some audio.
    func testNoPreRollSaysNoneRatherThanZero() {
        XCTAssertEqual(SettingsFormat.duration(frames: 0), "none")
    }

    /// Frames are what the core counts and what the flag carries, so the
    /// caption has to keep them — a window that only spoke in milliseconds
    /// would leave nothing connecting a setting to `--silence-frames` in
    /// the spawn log.
    func testTheCaptionKeepsTheCoreSUnit() {
        XCTAssertEqual(SettingsFormat.frameCount(25), "25 frames of 80 ms")
        XCTAssertEqual(SettingsFormat.frameCount(1), "1 frame of 80 ms")
        XCTAssertEqual(SettingsFormat.frameCount(0), "no frames")
    }

    /// 80 ms is fixed by the audio contract, not a preference. If this ever
    /// changes, every duration in the window is wrong.
    func testAFrameIsEightyMilliseconds() {
        XCTAssertEqual(SettingsFormat.frameMilliseconds, 80)
    }

    // MARK: - Gates and sliders

    /// The difference between "no words are discarded" and "words below
    /// 0.00 are discarded". The default is 0, so this is what almost every
    /// user sees.
    func testAnOpenGateSaysOff() {
        XCTAssertEqual(SettingsFormat.confidence(0), "off")
        XCTAssertEqual(SettingsFormat.confidence(0.5), "0.50")
        XCTAssertEqual(SettingsFormat.confidence(0.05), "0.05")
    }

    /// Two decimals always, so the number does not change width as the
    /// slider moves.
    func testThresholdsKeepTheirWidth() {
        XCTAssertEqual(SettingsFormat.threshold(0.5), "0.50")
        XCTAssertEqual(SettingsFormat.threshold(0.95), "0.95")
    }

    /// The slider is stepped in whole seconds, so a trailing `.0` would be
    /// noise.
    func testSecondsAreWhole() {
        XCTAssertEqual(SettingsFormat.seconds(30), "30 s")
        XCTAssertEqual(SettingsFormat.seconds(120), "120 s")
    }

    // MARK: - Sections

    /// The sidebar is built from `allCases`, so a section added without a
    /// title, a symbol or a summary would appear as a blank row rather than
    /// fail to compile.
    func testEverySectionCanBeDrawn() {
        for section in SettingsSection.allCases {
            XCTAssertFalse(section.title.isEmpty, "\(section) has no title")
            XCTAssertFalse(section.symbol.isEmpty, "\(section) has no symbol")
            XCTAssertFalse(section.summary.isEmpty, "\(section) has no summary")
        }
    }

    /// Each section distinguishable at a glance. A repeated glyph is the
    /// failure that matters: the sidebar is scanned by shape long before it
    /// is read. Asserted against `allCases.count` rather than a literal, so
    /// adding a section cannot make this pass by counting itself — the sets
    /// are what carry the claim.
    func testSectionsAreDistinguishable() {
        let count = SettingsSection.allCases.count
        XCTAssertEqual(Set(SettingsSection.allCases.map(\.symbol)).count, count)
        XCTAssertEqual(Set(SettingsSection.allCases.map(\.title)).count, count)
        XCTAssertEqual(Set(SettingsSection.allCases.map(\.summary)).count, count)
    }

    /// The order is the order of a turn — what starts it, what it becomes,
    /// what ends it, then the optional extras — and Dictation is what the
    /// app is for, so it opens there.
    ///
    /// Models sits next to Transcription because it is where that section's
    /// most consequential choice happens, not at the end with the extras.
    func testDictationComesFirst() {
        XCTAssertEqual(SettingsSection.allCases.first, .dictation)
        XCTAssertEqual(
            SettingsSection.allCases,
            [.dictation, .transcription, .models, .detection, .wakeWord, .recording])
    }

    // MARK: - The window fits

    /// The window is a fixed size and the controls are fixed widths, so a
    /// label gets whatever is left. This asserts that what is left is
    /// enough for the longest label in the window ("Fall back to the local
    /// model on failure", around 235pt at 13pt) plus the widest control
    /// that shares a row with a number.
    ///
    /// Without this, widening a control or the sidebar by twenty points
    /// silently wraps labels onto two lines instead of failing anywhere.
    func testACardLeavesRoomForALabel() {
        let pane =
            SettingsMetric.windowSize.width - SettingsMetric.sidebarWidth
            - 2 * SettingsMetric.gutter
        let cardInterior = pane - 2 * SettingsMetric.cardPadding

        let widestControl = max(
            SettingsMetric.fieldWidth,
            max(
                SettingsMetric.pickerWidth,
                SettingsMetric.sliderWidth + SettingsMetric.valueColumn))

        XCTAssertGreaterThanOrEqual(
            cardInterior - widestControl, 200,
            "a card leaves \(cardInterior - widestControl)pt for its labels")
    }

    /// The sidebar and the pane both start below the traffic lights,
    /// because the window uses `.fullSizeContentView` and nothing else
    /// reserves that space. A titlebar inset smaller than the buttons puts
    /// the first sidebar row under them.
    func testTheContentClearsTheTrafficLights() {
        XCTAssertGreaterThanOrEqual(SettingsMetric.titlebarInset, 28)
    }

    /// The window resolves its own layout, at the size it claims.
    ///
    /// The only automated evidence a redesign can offer, and worth having:
    /// a SwiftUI layout that cannot resolve — a `Spacer` inside a
    /// `fixedSize`, a fixed frame fighting a `ScrollView` — compiles
    /// perfectly and then fails in front of the user. Hosted rather than
    /// snapshotted on purpose; what it looks like is a matter for eyes.
    func testTheWindowLaysOutAtItsDesignedSize() {
        let view = NSHostingView(rootView: SettingsView(model: SettingsModel(defaults: defaults)))
        view.layoutSubtreeIfNeeded()
        XCTAssertEqual(view.fittingSize.width, SettingsMetric.windowSize.width)
        XCTAssertEqual(view.fittingSize.height, SettingsMetric.windowSize.height)
    }

    /// Every section can be selected and laid out, not just the one the
    /// window opens on. A pane that crashes or collapses is otherwise found
    /// by clicking on it.
    func testEverySectionLaysOut() {
        for section in SettingsSection.allCases {
            let view = NSHostingView(
                rootView: SettingsView(
                    model: SettingsModel(defaults: defaults), initialSection: section))
            view.layoutSubtreeIfNeeded()
            XCTAssertEqual(
                view.fittingSize.height, SettingsMetric.windowSize.height,
                "\(section) does not lay out at the designed height")
        }
    }

    /// A pending status is the one that has to carry the explanation: the
    /// core is genuinely running something other than what is on screen,
    /// and a disabled button cannot say so.
    func testAPendingStatusKeepsItsSentence() {
        let status = SettingsStatus.pending("The core is still running the previous settings.")
        XCTAssertEqual(status.text, "The core is still running the previous settings.")
        XCTAssertEqual(SettingsStatus.settled("Running these settings.").text, "Running these settings.")
    }
}
