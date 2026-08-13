import Foundation

/// Loudness readings from the core, scaled and released one at a time.
///
/// **Extracted so there is one answer to "how loud is that".** Every
/// indicator style has to solve the same three problems — a noise floor so
/// a quiet room reads as silent, an adaptive ceiling so a quiet microphone
/// still fills the shape, and a backlog policy — and three copies of that
/// would be three meters that disagree about the same audio. The drawing
/// is the part that differs between styles; this part must not.
///
/// Owned by the view, which is what keeps the timer's lifetime tied to
/// something visible. Nothing here draws.
final class LevelStream {

    /// Below this it is a quiet room. RMS, not peak — silence measures
    /// tens, ordinary speech measures thousands.
    static let noiseFloor: CGFloat = 60

    /// The scale never drops below this, so room hiss is not amplified
    /// into a dancing meter the moment you stop talking.
    static let minimumCeiling: CGFloat = 900

    /// Per 20 ms update: roughly a two-second half-life.
    static let ceilingDecay: CGFloat = 0.993

    /// Interval between updates from the helper.
    static let updateInterval: TimeInterval = 0.02

    /// A backlog would show audio late, and a meter that is behind is worse
    /// than one that skipped: it says you are speaking when you have
    /// stopped.
    static let maximumBacklog = 8

    /// Called on the main thread, once per `updateInterval`, with 0...1.
    private let onValue: (CGFloat) -> Void

    private var ceiling = LevelStream.minimumCeiling
    private var pending: [Int] = []
    private var draining = false

    /// Capture the view weakly in `onValue` — this object is owned by the
    /// view it drives, and a strong capture would be a cycle that keeps a
    /// hidden panel's timer alive forever.
    init(onValue: @escaping (CGFloat) -> Void) {
        self.onValue = onValue
    }

    // MARK: - Input

    /// Feed per-block loudness, oldest first. Main thread only.
    func append(blocks: [Int]) {
        guard !blocks.isEmpty else { return }
        pending.append(contentsOf: blocks)
        if pending.count > Self.maximumBacklog {
            pending.removeFirst(pending.count - Self.maximumBacklog)
        }
        startDraining()
    }

    func reset() {
        pending.removeAll()
        ceiling = Self.minimumCeiling
    }

    func stop() {
        pending.removeAll()
    }

    // MARK: - Scaling

    /// 0...1 for one loudness reading, against the current ceiling.
    ///
    /// Square root rather than linear because hearing is compressive:
    /// linear scaling makes ordinary speech look timid and wastes the top
    /// of the range on shouting.
    static func normalise(level: Int, ceiling: CGFloat, noiseFloor: CGFloat = noiseFloor)
        -> CGFloat
    {
        let above = CGFloat(max(0, level)) - noiseFloor
        guard above > 0 else { return 0 }
        return min(1, sqrt(above / max(ceiling - noiseFloor, 1)))
    }

    /// Where the ceiling goes next: up instantly, down slowly.
    ///
    /// Rising immediately matters — a sound louder than anything before it
    /// must not clip while the scale catches up. Falling slowly matters
    /// for the opposite reason: a scale that dropped as fast as it rose
    /// would pump between every word.
    static func nextCeiling(
        current: CGFloat,
        level: CGFloat,
        decay: CGFloat = ceilingDecay,
        minimum: CGFloat = minimumCeiling
    ) -> CGFloat {
        max(minimum, max(level, current * decay))
    }

    /// Peak-meter ballistics: rise fast, ease down.
    ///
    /// Instant attack, gradual release — the same rule real peak meters
    /// use. A shape jumps to a loud sound the moment it arrives, because
    /// lagging there feels unresponsive, and then falls back smoothly,
    /// because something that dropped as fast as it rose would flicker on
    /// every syllable boundary rather than dance.
    ///
    /// `attack` of 1 means jump straight to the target. Below that it
    /// closes a fraction of the gap per update, which is what lets one part
    /// of a shape trail another.
    static func ease(
        current: CGFloat,
        target: CGFloat,
        attack: CGFloat = 1,
        release: CGFloat = 0.90
    ) -> CGFloat {
        if target > current { return current + (target - current) * attack }
        return max(target, current * release)
    }

    // MARK: - Draining

    private func startDraining() {
        guard !draining else { return }
        draining = true
        step()
    }

    /// Show one update, then schedule the next.
    ///
    /// Four arrive together every 80 ms. Applying all four at once would
    /// throw away three quarters of the resolution that measuring them
    /// separately bought; spacing them out is what makes a meter track
    /// syllables rather than words.
    private func step() {
        guard !pending.isEmpty else {
            draining = false
            return
        }
        let level = pending.removeFirst()
        ceiling = Self.nextCeiling(current: ceiling, level: CGFloat(max(0, level)))
        onValue(Self.normalise(level: level, ceiling: ceiling))

        DispatchQueue.main.asyncAfter(deadline: .now() + Self.updateInterval) { [weak self] in
            self?.step()
        }
    }
}
