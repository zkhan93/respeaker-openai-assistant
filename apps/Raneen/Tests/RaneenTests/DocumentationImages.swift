import AppKit
import ImageIO
import SwiftUI
import UniformTypeIdentifiers
import XCTest

@testable import Raneen

/// Generates the images the README uses, from the real views.
///
/// **Skipped unless `RANEEN_DOC_IMAGES` names an output directory**, so CI
/// never writes files and no ordinary test run depends on a screenshot
/// matching. Run it with:
///
///     make -C apps/Raneen screenshots
///
/// **Why it lives in the test target.** It needs the `Raneen` module, and the
/// alternatives were worse: a second executable product would ship a binary
/// nobody installs, and a separate package would duplicate the target. The
/// cost is that a generator sits among assertions, which the name and the
/// skip are there to make obvious.
///
/// What it cannot do, and what the README therefore still needs a human for:
/// dictation itself. Text arriving in another application is the product, and
/// capturing it means running the app, holding the key and speaking — none of
/// which happens in a test process. The one thing an offscreen render also
/// loses is vibrancy: `NSVisualEffectView` is composited by the window
/// server, so the sidebar here is its flat fallback colour rather than the
/// translucency you see in the app.
final class DocumentationImages: XCTestCase {

    private var out: String!
    private var defaults: UserDefaults!
    private var suite: String!

    override func setUpWithError() throws {
        guard let directory = ProcessInfo.processInfo.environment["RANEEN_DOC_IMAGES"] else {
            throw XCTSkip("set RANEEN_DOC_IMAGES to generate the README images")
        }
        out = directory
        try FileManager.default.createDirectory(
            atPath: directory, withIntermediateDirectories: true)

        // An isolated domain, removed again in teardown: generating images
        // must not write into the domain the app reads, and a suite left
        // behind is a preference file per run.
        suite = "raneen.docimages.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
    }

    override func tearDown() {
        if let suite { UserDefaults.standard.removePersistentDomain(forName: suite) }
        super.tearDown()
    }

    // MARK: - The window

    /// Exactly the panes the README shows, and no others.
    ///
    /// Generating every pane in both appearances was the obvious thing and the
    /// wrong one: it left untracked files in `docs/images` after every run, so
    /// regenerating the screenshots dirtied the tree. Add a section here when
    /// the README starts using it.
    private static let published: [(SettingsSection, NSAppearance.Name, String)] = [
        (.models, .darkAqua, "settings-models.png"),
        (.dictation, .darkAqua, "settings-dictation.png"),
        (.wakeWord, .darkAqua, "settings-wakeWord.png"),
        (.recording, .darkAqua, "settings-recording.png"),
    ]

    func testSettingsPanes() throws {
        for (section, appearance, name) in Self.published {
            let model = SettingsModel(defaults: defaults)
            Self.configure(model, for: section)

            let view = NSHostingView(
                rootView: SettingsView(model: model, initialSection: section))
            view.frame = NSRect(origin: .zero, size: SettingsMetric.windowSize)
            view.appearance = NSAppearance(named: appearance)
            view.layoutSubtreeIfNeeded()

            try write(view, to: name)
        }

        // A crude assertion, but not a pointless one: a pane that failed to
        // lay out writes a blank image, and nothing else here would notice.
        let path = "\(out!)/settings-models.png"
        let size = try FileManager.default.attributesOfItem(atPath: path)[.size] as? Int64 ?? 0
        XCTAssertGreaterThan(size, 20_000, "settings-models.png looks empty")
    }

    /// Turn on what a pane is about, where the default is off.
    ///
    /// Not dressing the screenshots up: two panes are deliberately almost
    /// empty until the feature they configure is enabled, so a shot of the
    /// defaults documents the absence rather than the feature. These are
    /// settings a user would actually choose, set through the same published
    /// properties the window itself writes.
    private static func configure(_ model: SettingsModel, for section: SettingsSection) {
        switch section {
        case .recording:
            model.config.broadcast = .network
        case .wakeWord:
            model.addWakeWord(
                WakeWordLibrary.searchPaths.last!.appendingPathComponent("alexa_v0.1.onnx").path)
        default:
            break
        }
    }

    // MARK: - The indicator

    /// The three listening animations, side by side, fed the same synthetic
    /// sentence — the same signal `IndicatorPreview` uses in the settings
    /// window, which exists precisely because these three names mean nothing
    /// unseen.
    ///
    /// A GIF rather than a still: two of the three keep moving in silence,
    /// and that is the whole point of them. A still frame of `swarm` is a
    /// handful of dots.
    func testIndicatorAnimation() throws {
        let styles = IndicatorStyle.allCases
        let cell = NSSize(width: 108, height: 104)
        let canvas = NSSize(width: cell.width * CGFloat(styles.count), height: cell.height)

        let container = NSView(frame: NSRect(origin: .zero, size: canvas))
        container.wantsLayer = true
        container.layer?.backgroundColor =
            NSColor(calibratedWhite: 0.11, alpha: 1).cgColor

        var previews: [IndicatorPreviewView] = []
        for (index, style) in styles.enumerated() {
            let preview = IndicatorPreviewView(style: style)
            preview.frame = NSRect(
                x: CGFloat(index) * cell.width + (cell.width - style.panelSize.width) / 2,
                y: cell.height - 18 - style.panelSize.height
                    - (74 - style.panelSize.height) / 2,
                width: style.panelSize.width, height: style.panelSize.height)
            container.addSubview(preview)
            previews.append(preview)

            let label = NSTextField(labelWithString: style.label)
            label.font = .systemFont(ofSize: 10, weight: .medium)
            label.textColor = NSColor(calibratedWhite: 0.62, alpha: 1)
            label.alignment = .center
            label.frame = NSRect(
                x: CGFloat(index) * cell.width, y: 5, width: cell.width, height: 14)
            container.addSubview(label)
        }

        // Hosted in a window so the previews' own feed starts — they run only
        // while on screen, which is right for the app and something this has
        // to work around.
        let window = NSWindow(
            contentRect: NSRect(origin: NSPoint(x: -4000, y: -4000), size: canvas),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        // Setting `contentView` is what starts them: `IndicatorPreviewView`
        // feeds itself only while it has a window, so that a preview behind a
        // closed settings window is not an animation nobody is watching.
        window.contentView = container
        window.orderBack(nil)
        XCTAssertTrue(previews.allSatisfy { $0.window != nil })

        var frames: [CGImage] = []
        // 3.2 seconds at 12.5 fps. Long enough for the synthetic phrase to
        // rise, fall and pause; short enough to stay a README-sized file.
        for _ in 0..<40 {
            RunLoop.current.run(until: Date().addingTimeInterval(0.08))
            container.displayIfNeeded()
            if let image = snapshot(container) { frames.append(image) }
        }
        window.contentView = nil
        window.orderOut(nil)

        XCTAssertEqual(frames.count, 40)
        try gif(frames, delay: 0.08, to: "\(out!)/indicator.gif")

        // Two frames of an animation must not be identical, or the recording
        // caught a stopped clock — which is what a missing run loop looks
        // like, and it would produce a still image with a .gif extension.
        let a = try png(frames[8]), b = try png(frames[20])
        XCTAssertNotEqual(a, b, "the indicator never moved")
    }

    // MARK: - Writing

    private func write(_ view: NSView, to name: String) throws {
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            return XCTFail("no bitmap for \(name)")
        }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else {
            return XCTFail("no png for \(name)")
        }
        try data.write(to: URL(fileURLWithPath: "\(out!)/\(name)"))
    }

    private func snapshot(_ view: NSView) -> CGImage? {
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return nil }
        view.cacheDisplay(in: view.bounds, to: rep)
        return rep.cgImage
    }

    private func png(_ image: CGImage) throws -> Data {
        let rep = NSBitmapImageRep(cgImage: image)
        return try XCTUnwrap(rep.representation(using: .png, properties: [:]))
    }

    private func gif(_ frames: [CGImage], delay: Double, to path: String) throws {
        let url = URL(fileURLWithPath: path)
        let destination = try XCTUnwrap(
            CGImageDestinationCreateWithURL(
                url as CFURL, UTType.gif.identifier as CFString, frames.count, nil))

        CGImageDestinationSetProperties(
            destination,
            [kCGImagePropertyGIFDictionary: [kCGImagePropertyGIFLoopCount: 0]] as CFDictionary)
        for frame in frames {
            CGImageDestinationAddImage(
                destination, frame,
                [kCGImagePropertyGIFDictionary: [kCGImagePropertyGIFDelayTime: delay]]
                    as CFDictionary)
        }
        XCTAssertTrue(CGImageDestinationFinalize(destination), "could not write \(path)")
    }
}
