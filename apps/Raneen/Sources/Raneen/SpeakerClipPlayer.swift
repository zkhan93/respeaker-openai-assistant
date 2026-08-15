import AVFoundation
import Foundation

/// Plays back the few seconds of audio that created a speaker profile.
///
/// **This is what makes naming possible at all.** `speaker_3 · 4
/// recordings` identifies nobody; four seconds of the person does it
/// instantly. Without playback the only way to work out who a profile
/// belongs to is to guess from the order they were discovered in, and a
/// wrong guess attaches a real person's name to someone else's voice.
///
/// One clip at a time, and pressing a second row stops the first. Two
/// voices over each other is worse than useless here — the entire task
/// is telling one from the other.
final class SpeakerClipPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {

    /// The speaker whose clip is playing, so their row can show it.
    @Published private(set) var playing: String?

    private var player: AVAudioPlayer?

    /// Play this speaker's clip, or stop it if it is already playing.
    func toggle(_ id: String, url: URL) {
        if playing == id {
            stop()
            return
        }
        stop()
        do {
            let player = try AVAudioPlayer(contentsOf: url)
            player.delegate = self
            self.player = player
            player.play()
            playing = id
        } catch {
            // The clip is the core's to write and the user's to delete, so
            // a missing one is a normal state rather than a fault. Say so
            // in the log and leave the row alone.
            Log.app.error("could not play \(url.lastPathComponent): \(error.localizedDescription)")
            stop()
        }
    }

    func stop() {
        player?.stop()
        player = nil
        playing = nil
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        // Dispatched rather than assigned directly: this arrives on
        // whichever thread started playback, and `playing` drives a view.
        DispatchQueue.main.async { [weak self] in
            guard self?.player === player else { return }
            self?.stop()
        }
    }
}
