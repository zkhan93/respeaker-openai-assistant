import AppKit

/// An audio waveform: thin mirrored bars, height driven purely by what
/// the microphone is hearing.
///
/// **Nothing here animates on a clock.** An earlier version added a
/// free-running sine so the bars would undulate, and that is precisely
/// what made it read as a *processing* indicator — continuous motion
/// that carries no information is the visual language of a spinner. The
/// only thing that moves these bars is your voice: speak and it reacts,
/// stop and it settles flat.
///
/// Bars hold a short history, newest on the right, mirrored about the
/// centre line — the shape everyone already recognises as audio, from
/// Voice Memos to every recorder ever made. Silence is a thin flat line,
/// which is unmistakably "listening and hearing nothing" rather than
/// "busy".
///
/// Amplitude is scaled with a square root rather than linearly. Real
/// meters are compressive because hearing is: linear scaling makes
/// ordinary speech look timid and wastes the top of the range on shouts.
///
/// Bars are brand orange on the panel's black. The previous white-on-
/// translucent-grey was hard to see for an unavoidable reason: the panel
/// sampled whatever was behind it, so the background was never a known
/// colour and the contrast changed with the wallpaper.
final class WaveformView: NSView {

    /// Thin bars, and enough of them to read as a wave rather than as a
    /// row of dots.
    private static let barCount = 19

    /// Peak that maps to full height, before the square-root curve.
    /// Measured speech on the built-in mic peaks around 2–10k.
    static let fullScaleLevel: CGFloat = 8000

    /// Brand orange on the panel's black, deliberately the same colour
    /// the menu-bar mark turns while recording.
    ///
    /// Reusing `StatusIcon.brandColor` rather than restating the hex is
    /// the point: the two indicators are saying the same thing in two
    /// places, and they should not be able to drift into two oranges.
    static let barColor = StatusIcon.brandColor

    private var targets: [CGFloat]
    private var current: [CGFloat]
    private var timer: Timer?

    override init(frame frameRect: NSRect) {
        targets = Array(repeating: 0, count: Self.barCount)
        current = Array(repeating: 0, count: Self.barCount)
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        fatalError("not used")
    }

    /// Feed a peak sample. Main thread only.
    func append(peak: Int) {
        targets.removeFirst()
        targets.append(Self.normalise(peak: peak, fullScale: Self.fullScaleLevel))
    }

    /// 0...1, compressive and clamped.
    ///
    /// Clamping matters: int16 peaks at 32767 against a full scale of
    /// 8000, and an unclamped value draws bars outside the panel.
    static func normalise(peak: Int, fullScale: CGFloat) -> CGFloat {
        guard peak > 0 else { return 0 }
        return min(1.0, sqrt(CGFloat(peak) / fullScale))
    }

    /// The 60 Hz timer only *eases* bars toward audio-derived targets.
    /// It never generates motion of its own — see the class note.
    func startAnimating() {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.step()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stopAnimating() {
        timer?.invalidate()
        timer = nil
    }

    func reset() {
        targets = Array(repeating: 0, count: Self.barCount)
        current = Array(repeating: 0, count: Self.barCount)
        needsDisplay = true
    }

    private func step() {
        var changed = false
        for i in current.indices {
            let delta = targets[i] - current[i]
            if abs(delta) > 0.002 {
                // Rise fast so a syllable lands immediately; fall a
                // little slower so it settles instead of flickering.
                current[i] += delta * (delta > 0 ? 0.5 : 0.25)
                changed = true
            } else if current[i] != targets[i] {
                current[i] = targets[i]
                changed = true
            }
        }
        if changed { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        context.clear(dirtyRect)

        let spacing: CGFloat = 1.5
        let totalSpacing = spacing * CGFloat(Self.barCount - 1)
        let barWidth = max(1.5, (bounds.width - totalSpacing) / CGFloat(Self.barCount))
        let midY = bounds.midY
        let maxHalf = bounds.height / 2
        // At silence the bars are a flat line of the same thickness as
        // their width — dots, not slivers.
        let minHalf = barWidth / 2

        Self.barColor.setFill()

        for (index, value) in current.enumerated() {
            let half = minHalf + value * (maxHalf - minHalf)
            let x = CGFloat(index) * (barWidth + spacing)
            let rect = NSRect(x: x, y: midY - half, width: barWidth, height: half * 2)
            NSBezierPath(roundedRect: rect, xRadius: barWidth / 2, yRadius: barWidth / 2).fill()
        }
    }
}
