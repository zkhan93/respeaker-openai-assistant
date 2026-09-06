import XCTest

@testable import Raneen

/// When the shell may hold the microphone open.
///
/// The policy is what stands between "hold a key to dictate" and a live
/// microphone all day, so every branch is pinned: a reason that stops
/// appearing means the window stops warning, and a reason that appears
/// wrongly means the device stays open for nothing.
final class MicrophonePolicyTests: XCTestCase {

    /// The default configuration is hold-to-talk with nothing else on, and
    /// that must never keep the device open between presses.
    func testTheDefaultConfigurationClosesTheMicrophoneBetweenTurns() {
        XCTAssertEqual(MicrophonePolicy(for: HelperConfig()), .whileArmed)
        XCTAssertFalse(MicrophonePolicy(for: HelperConfig()).isContinuous)
    }

    func testToggleWithNothingElseOnAlsoClosesIt() {
        var config = HelperConfig()
        config.trigger = .toggle
        XCTAssertEqual(MicrophonePolicy(for: config), .whileArmed)
    }

    /// Speech opening a turn is the definition of an always-open microphone.
    func testASpeechTriggerNeedsTheRoom() {
        var config = HelperConfig()
        config.trigger = .vad
        XCTAssertEqual(MicrophonePolicy(for: config), .continuous([.speechTrigger]))
    }

    /// The wake word is one reason, not two, when it is also the trigger.
    func testAWakeWordTriggerIsOneReason() {
        var config = HelperConfig()
        config.trigger = .wakeword
        config.wakeWords = ["/tmp/alexa.onnx"]
        XCTAssertEqual(MicrophonePolicy(for: config), .continuous([.wakeWordTrigger]))
    }

    /// A wake word armed for reporting under hold-to-talk still scores
    /// every frame — the reason a hotkey user would otherwise not expect.
    func testAnArmedWakeWordKeepsItOpenEvenUnderAKey() {
        var config = HelperConfig()
        config.trigger = .hold
        config.wakeWords = ["/tmp/alexa.onnx"]
        XCTAssertEqual(MicrophonePolicy(for: config), .continuous([.wakeWordArmed]))
    }

    /// Both publishing modes record, and recording has to hear the room.
    func testPublishingKeepsItOpenInEitherMode() {
        for mode in [BroadcastMode.loopback, .network] {
            var config = HelperConfig()
            config.broadcast = mode
            XCTAssertEqual(MicrophonePolicy(for: config), .continuous([.recording]), "\(mode)")
        }
    }

    func testSpeakerIdentificationKeepsItOpen() {
        var config = HelperConfig()
        config.identifySpeakers = true
        XCTAssertEqual(MicrophonePolicy(for: config), .continuous([.speakerIdentification]))
    }

    /// Reasons accumulate in a fixed order — trigger first, then the two
    /// consumers — so the sentence reads the same way every time.
    func testReasonsAccumulateInAStableOrder() {
        var config = HelperConfig()
        config.trigger = .hold
        config.wakeWords = ["/tmp/alexa.onnx"]
        config.broadcast = .loopback
        config.identifySpeakers = true
        XCTAssertEqual(
            MicrophonePolicy(for: config).reasons,
            [.wakeWordArmed, .recording, .speakerIdentification])
    }

    // MARK: - The way back

    /// The button under the callout must actually get the user to
    /// `whileArmed`, whatever combination of the three reasons was on —
    /// and leave the trigger, which is a different decision, untouched.
    func testClosingBetweenTurnsRemovesEveryKeyedReason() {
        let suite = "raneen.tests.micpolicy"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }

        let model = SettingsModel(defaults: defaults)
        model.config.trigger = .hold
        model.addWakeWord("/tmp/alexa.onnx")
        model.config.broadcast = .network
        model.config.identifySpeakers = true
        XCTAssertTrue(MicrophonePolicy(for: model.config).isContinuous)

        model.closeMicrophoneBetweenTurns()

        XCTAssertEqual(MicrophonePolicy(for: model.config), .whileArmed)
        XCTAssertEqual(model.config.trigger, .hold)
    }

    /// The same action under a wake-word trigger cannot reach `whileArmed`
    /// — the trigger is the reason — and must not silently change the
    /// trigger to get there.
    func testClosingBetweenTurnsLeavesTheTriggerAlone() {
        let suite = "raneen.tests.micpolicy.trigger"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        defer { defaults.removePersistentDomain(forName: suite) }

        let model = SettingsModel(defaults: defaults)
        model.config.trigger = .wakeword
        model.addWakeWord("/tmp/alexa.onnx")
        model.closeMicrophoneBetweenTurns()
        XCTAssertEqual(model.config.trigger, .wakeword)
        XCTAssertEqual(MicrophonePolicy(for: model.config), .continuous([.wakeWordTrigger]))
    }

    // MARK: - Words

    /// Every reason has a label, since each one is read after "because".
    func testEveryReasonReads() {
        for reason in MicrophonePolicy.Reason.allCases {
            XCTAssertFalse(reason.label.isEmpty, "\(reason) has no label")
        }
    }

    func testTheClauseIsGrammaticalAtEveryLength() {
        XCTAssertEqual(MicrophonePolicy.whileArmed.reasonClause, "")
        XCTAssertEqual(
            MicrophonePolicy.continuous([.recording]).reasonClause, "recording is on")
        XCTAssertEqual(
            MicrophonePolicy.continuous([.wakeWordArmed, .recording]).reasonClause,
            "a wake word is armed and recording is on")
        XCTAssertEqual(
            MicrophonePolicy.continuous([.wakeWordArmed, .recording, .speakerIdentification])
                .reasonClause,
            "a wake word is armed, recording is on and speaker identification is on")
    }

    /// The menu-bar line names the actual key, and says why when it cannot
    /// close — the two facts a person glancing at the menu wants.
    func testTheSummaryNamesTheKeyOrTheReason() {
        XCTAssertEqual(
            MicrophonePolicy.whileArmed.summary(key: "Right Option (⌥)"),
            "Mic opens only while you hold Right Option (⌥)")
        XCTAssertEqual(
            MicrophonePolicy.continuous([.recording]).summary(key: "Right Option (⌥)"),
            "Mic stays open: recording is on")
    }
}
