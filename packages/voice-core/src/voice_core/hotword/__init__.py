"""Wake-word detection (openWakeWord).

Cross-platform but heavy: requires the ``hotword`` extra
(``voice-core[hotword]``). Import the submodule directly so that
installations without the extra — a push-to-talk-only desktop build, for
instance — never trigger the import::

    from voice_core.hotword.detector import HotwordDetector, ensure_model
"""
