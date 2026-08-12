//! The two fan-out mechanisms the core is built on.
//!
//! Ported from `voice_core.bus` rather than invented, because the shape
//! is what makes the modes in AD-17 possible at all: always-on and
//! hotkey are the same pipeline with a different trigger, and disk
//! logging is another consumer rather than another code path.

pub mod audio_bus;
pub mod event_bus;
