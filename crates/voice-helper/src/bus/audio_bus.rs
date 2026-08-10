//! Ring buffer of PCM frames with independent reader cursors.
//!
//! A direct port of `voice_core.bus.audio_bus`, and for the same reason:
//! more than one thing wants the audio, and they want it at different
//! rates. The level meter wants every frame now; the segmenter wants a
//! contiguous run starting slightly *before* it was told to start; a
//! disk recorder wants everything, slowly, and must not stall either of
//! the others when the filesystem hiccups.
//!
//! One shared ring with per-consumer cursors gives all three. The
//! alternative — a broadcast channel — cannot express `rewind`, which is
//! the entire mechanism behind pre-roll (AD-11).
//!
//! ## What Rust changes
//!
//! Frames are `Arc<[i16]>`, so fan-out to N consumers costs N refcount
//! bumps rather than N copies. The Python version hands out `bytes`,
//! which is also cheap, but here it is enforced: a consumer *cannot*
//! mutate a frame another consumer is holding, because the type does not
//! permit it.

use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

/// One frame of PCM16, shared between consumers without copying.
pub type Frame = Arc<[i16]>;

/// ~40 s at 80 ms per frame — the same figure the Python bus uses.
///
/// It is a *history* budget, not a latency budget: it bounds how far a
/// slow consumer may fall behind before it starts losing frames, and how
/// far back `rewind` can reach.
pub const DEFAULT_CAPACITY: usize = 500;

struct Ring {
    slots: Vec<Option<Frame>>,
    /// Total frames ever published. Monotonic, so cursor arithmetic
    /// never has to reason about wrap-around — only the *index* wraps.
    write_pos: u64,
}

pub struct AudioBus {
    ring: Mutex<Ring>,
    published: Condvar,
    capacity: usize,
}

impl AudioBus {
    pub fn new(capacity: usize) -> Arc<Self> {
        assert!(capacity > 0, "an audio bus needs at least one slot");
        Arc::new(Self {
            ring: Mutex::new(Ring {
                slots: vec![None; capacity],
                write_pos: 0,
            }),
            published: Condvar::new(),
            capacity,
        })
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    /// Publish one frame. Never blocks — the oldest frame is overwritten.
    ///
    /// Overwriting rather than blocking is the correct trade for audio:
    /// the capture thread is real-time and stalling it drops frames at
    /// the device, which is worse than dropping them for one slow reader.
    pub fn publish(&self, frame: Frame) {
        let mut ring = self.ring.lock().unwrap_or_else(|e| e.into_inner());
        let index = (ring.write_pos % self.capacity as u64) as usize;
        ring.slots[index] = Some(frame);
        ring.write_pos += 1;
        drop(ring);
        self.published.notify_all();
    }

    pub fn write_pos(&self) -> u64 {
        self.ring
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .write_pos
    }

    /// A cursor starting at the present. Existing history is not replayed
    /// — a new consumer wants what happens next, and anything that wants
    /// the past asks for it explicitly with `rewind`.
    pub fn create_reader(self: &Arc<Self>) -> AudioBusReader {
        AudioBusReader {
            read_pos: self.write_pos(),
            bus: Arc::clone(self),
        }
    }
}

pub struct AudioBusReader {
    bus: Arc<AudioBus>,
    read_pos: u64,
}

impl AudioBusReader {
    pub fn position(&self) -> u64 {
        self.read_pos
    }

    /// Next frame, or `None` if none arrived within `timeout`.
    ///
    /// A reader that has fallen more than `capacity` behind is silently
    /// advanced to the oldest surviving frame. Silent because there is
    /// nothing the reader can do about it and nothing useful to hand
    /// back — the frames are already gone. `available()` is how a
    /// consumer that cares notices it is losing.
    pub fn read(&mut self, timeout: Duration) -> Option<Frame> {
        let mut ring = self.bus.ring.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            let oldest = ring.write_pos.saturating_sub(self.bus.capacity as u64);
            if self.read_pos < oldest {
                self.read_pos = oldest;
            }
            if self.read_pos < ring.write_pos {
                let index = (self.read_pos % self.bus.capacity as u64) as usize;
                let frame = ring.slots[index].clone();
                self.read_pos += 1;
                return frame;
            }

            let (guard, timed_out) = self
                .bus
                .published
                .wait_timeout(ring, timeout)
                .unwrap_or_else(|e| e.into_inner());
            ring = guard;
            if timed_out.timed_out() {
                return None;
            }
        }
    }

    /// Jump to the present, discarding the backlog.
    ///
    /// What a turn-based consumer does when a new turn starts: the audio
    /// it has not read yet belongs to the silence before the trigger, not
    /// to the utterance. Pair with `rewind` to keep a deliberate slice.
    pub fn skip_to_latest(&mut self) {
        self.read_pos = self.bus.write_pos();
    }

    /// Step back up to `frames`, returning how many were actually gained.
    ///
    /// **This is pre-roll.** A VAD reports "started" only after its
    /// threshold of consecutive speech frames, so a VAD-triggered
    /// recording begins some way *into* the first word; a hotkey press
    /// is exact but humans start speaking a beat early. Both are fixed
    /// by reaching backwards, and the ring already holds the audio.
    ///
    /// Returns less than asked when the history is not there — right
    /// after startup, or when the bus has lapped. Callers should treat a
    /// short rewind as normal, not as an error.
    pub fn rewind(&mut self, frames: usize) -> usize {
        let ring = self.bus.ring.lock().unwrap_or_else(|e| e.into_inner());
        let oldest = ring.write_pos.saturating_sub(self.bus.capacity as u64);
        let target = self.read_pos.saturating_sub(frames as u64).max(oldest);
        let gained = self.read_pos - target;
        self.read_pos = target;
        gained as usize
    }

    /// Frames waiting, capped at capacity. A number that keeps climbing
    /// is a consumer that cannot keep up.
    pub fn available(&self) -> usize {
        let ring = self.bus.ring.lock().unwrap_or_else(|e| e.into_inner());
        let behind = ring.write_pos.saturating_sub(self.read_pos);
        behind.min(self.bus.capacity as u64) as usize
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(value: i16) -> Frame {
        Arc::from(vec![value; 4].into_boxed_slice())
    }

    const NOW: Duration = Duration::from_millis(10);

    #[test]
    fn readers_are_independent() {
        let bus = AudioBus::new(8);
        let mut fast = bus.create_reader();
        let mut slow = bus.create_reader();

        for i in 0..3 {
            bus.publish(frame(i));
        }
        assert_eq!(fast.read(NOW).unwrap()[0], 0);
        assert_eq!(fast.read(NOW).unwrap()[0], 1);
        // The slow reader still has all three: one cursor moving does not
        // consume the frame for anybody else.
        assert_eq!(slow.read(NOW).unwrap()[0], 0);
        assert_eq!(slow.available(), 2);
    }

    #[test]
    fn a_reader_that_falls_behind_is_advanced_not_stalled() {
        let bus = AudioBus::new(4);
        let mut reader = bus.create_reader();
        for i in 0..10 {
            bus.publish(frame(i));
        }
        // Six frames were overwritten; the next read is the oldest that
        // still exists, not a panic and not frame 0.
        assert_eq!(reader.read(NOW).unwrap()[0], 6);
    }

    #[test]
    fn rewind_recovers_pre_trigger_audio() {
        let bus = AudioBus::new(16);
        let mut reader = bus.create_reader();
        for i in 0..10 {
            bus.publish(frame(i));
        }
        reader.skip_to_latest();
        assert_eq!(reader.available(), 0);

        assert_eq!(reader.rewind(3), 3);
        assert_eq!(reader.read(NOW).unwrap()[0], 7);
    }

    #[test]
    fn rewind_is_clamped_to_available_history() {
        let bus = AudioBus::new(4);
        let mut reader = bus.create_reader();
        for i in 0..2 {
            bus.publish(frame(i));
        }
        reader.skip_to_latest();
        // Asked for 10, only 2 exist. Short, not an error.
        assert_eq!(reader.rewind(10), 2);
        assert_eq!(reader.read(NOW).unwrap()[0], 0);
    }

    #[test]
    fn read_times_out_on_silence() {
        let bus = AudioBus::new(4);
        let mut reader = bus.create_reader();
        assert!(reader.read(NOW).is_none());
    }

    #[test]
    fn a_new_reader_starts_at_the_present() {
        let bus = AudioBus::new(8);
        for i in 0..5 {
            bus.publish(frame(i));
        }
        let mut reader = bus.create_reader();
        assert_eq!(reader.available(), 0);
        bus.publish(frame(99));
        assert_eq!(reader.read(NOW).unwrap()[0], 99);
    }
}
