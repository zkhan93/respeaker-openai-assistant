import AppKit
import SwiftUI

/// The chosen animation, running, inside the settings window.
///
/// **Three words in a list are not a choice.** "Bloom" and "Swarm" mean
/// nothing until you have seen them, and the alternative to a preview is
/// picking one, dismissing the window, holding the key and looking at the
/// bottom of the screen — for each of them in turn. So this draws the real
/// indicator view, in the real black capsule, at the real size.
///
/// It is fed synthetic speech rather than the microphone. Opening Settings
/// must not start recording: the app holds the input device only while
/// dictation is armed, and a preview that took the microphone would put a
/// recording indicator in the menu bar for as long as the window was open.
struct IndicatorPreview: NSViewRepresentable {

    let style: IndicatorStyle

    func makeNSView(context: Context) -> IndicatorPreviewView {
        IndicatorPreviewView(style: style)
    }

    func updateNSView(_ view: IndicatorPreviewView, context: Context) {
        view.restyle(style)
    }
}

/// The capsule and the fake voice behind `IndicatorPreview`.
final class IndicatorPreviewView: NSView {

    private var style: IndicatorStyle
    private var indicator: IndicatorView
    private let backdrop = NSView()
    private var feed: Timer?
    private var elapsed: TimeInterval = 0

    /// Loud enough to fill the shape against `LevelStream.minimumCeiling`,
    /// which is what the meter scales against until something louder
    /// arrives. A quieter figure previews a permanently timid animation.
    private static let peakLevel: Double = 5200

    init(style: IndicatorStyle) {
        self.style = style
        self.indicator = style.makeView()
        super.init(frame: NSRect(origin: .zero, size: style.panelSize))
        backdrop.wantsLayer = true
        addSubview(backdrop)
        backdrop.addSubview(indicator)
        layoutIndicator()
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    /// Sized by the style, so the preview is the panel rather than an
    /// impression of it — the styles differ in size and hiding that would
    /// misrepresent two of the three.
    override var intrinsicContentSize: NSSize { style.panelSize }

    func restyle(_ next: IndicatorStyle) {
        guard next != style else { return }
        indicator.stopAnimating()
        indicator.removeFromSuperview()
        style = next
        indicator = next.makeView()
        backdrop.addSubview(indicator)
        invalidateIntrinsicContentSize()
        layoutIndicator()
        indicator.reset()
        indicator.startAnimating()
    }

    override func layout() {
        super.layout()
        layoutIndicator()
    }

    private func layoutIndicator() {
        backdrop.frame = NSRect(
            x: (bounds.width - style.panelSize.width) / 2,
            y: (bounds.height - style.panelSize.height) / 2,
            width: style.panelSize.width,
            height: style.panelSize.height)
        // The same backdrop rule the panel applies, so the preview shows
        // what will actually float over your work — including that two of
        // the three have no box around them at all.
        backdrop.layer?.backgroundColor =
            style.hasBackdrop ? NSColor.black.cgColor : NSColor.clear.cgColor
        backdrop.layer?.cornerRadius = style.hasBackdrop ? style.cornerRadius : 0
        backdrop.layer?.masksToBounds = style.hasBackdrop
        indicator.frame = backdrop.bounds.insetBy(
            dx: style.contentInset.width, dy: style.contentInset.height)
    }

    // MARK: - Lifecycle

    /// Runs only while on screen. A timer left running behind a closed
    /// settings window is a preview nobody is watching.
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        window == nil ? stop() : start()
    }

    private func start() {
        guard feed == nil else { return }
        indicator.reset()
        indicator.startAnimating()
        let timer = Timer(
            timeInterval: LevelStream.updateInterval, repeats: true
        ) { [weak self] _ in
            guard let self else { return }
            self.elapsed += LevelStream.updateInterval
            self.indicator.append(blocks: [Self.level(at: self.elapsed)])
        }
        RunLoop.main.add(timer, forMode: .common)
        feed = timer
    }

    private func stop() {
        feed?.invalidate()
        feed = nil
        indicator.stopAnimating()
    }

    deinit {
        feed?.invalidate()
    }

    // MARK: - Synthetic speech

    /// Something that rises and falls like a sentence.
    ///
    /// Three incommensurable rates: syllables, words and the arc of a
    /// phrase. A single sine reads as a machine idling, which flatters the
    /// styles that are smooth and hides how the others behave at an onset.
    /// The floor at zero is what gives the pauses that make the difference
    /// between the styles visible.
    static func level(at time: TimeInterval) -> Int {
        let t = CGFloat(time)
        let syllables = sin(t * 2.3) * 0.50
        let words = sin(t * 3.9) * 0.28
        let phrase = sin(t * 1.1) * 0.34
        let voiced = max(0, 0.42 + 0.55 * (syllables + words + phrase))
        let breath = 0.55 + 0.45 * sin(t * 0.6 + 1)
        return Int(voiced * breath * CGFloat(peakLevel))
    }
}
