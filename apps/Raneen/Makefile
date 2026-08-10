# Raneen — Swift menu-bar shell around the Python core.
#
# The whole bundle is assembled by script rather than by Xcode: layout,
# Info.plist and signing are all reviewable as text, and a clean checkout
# builds the same thing without anyone opening an IDE.
#
#   make app     build the helper and the .app
#   make run     run it (foreground, so you see helper logs)
#   make check   drive the helper's protocol and report pass/fail
#   make sign    codesign with a Developer ID (needs IDENTITY=...)
#   make dmg     package the signed app
#   make clean
#
# WHICH CORE:  CORE=rust (default) | CORE=python
#
#   make dmg                 Rust core   — ~60 MB bundle, ~65 MB resident
#   make dmg CORE=python     Python core — ~187 MB bundle, ~480 MB resident
#
# Both speak the same protocol (see protocol/README.md), so the Swift shell
# is identical either way. Python stays buildable deliberately: it is the
# reference implementation and the other half of the conformance suite.

# Which core to bundle. Rust by default since 2026-08-09 — verified in the
# real app, and 61 MB against the Python helper's ~480.
CORE        ?= rust

APP_NAME    := Raneen
BUNDLE      := build/$(APP_NAME).app
CONTENTS    := $(BUNDLE)/Contents
HELPER_DIR  := $(CONTENTS)/Resources/helper
SWIFT_BIN   := .build/release/$(APP_NAME)
ENTITLEMENTS:= Resources/entitlements.plist

# Signing identity, found in the keychain by default.
#
# **Why this is auto-detected rather than defaulting to ad-hoc.** An
# ad-hoc signature ("-") is derived from the binary itself, so every
# rebuild is a different application as far as TCC is concerned. The
# Accessibility grant does not carry across, the stale entry in System
# Settings still *looks* ticked, and `CGEvent.tapCreate` then returns nil
# — the hotkey silently stops working with no error anywhere. That cost
# a debugging session once; a default nobody has to remember costs
# nothing.
#
# Override explicitly if you have several:
#   make dmg IDENTITY="Developer ID Application: ACME (TEAMID)"
# ----- core selection -------------------------------------------------------

RUST_CRATE   := ../../crates/voice-helper
RUST_BIN     := $(RUST_CRATE)/target/release/voice-helper
RUST_SOURCES := $(shell find $(RUST_CRATE)/src -name '*.rs' 2>/dev/null)

# The model ships inside the bundle. `default_model()` in the helper looks
# beside its own executable first, which is why no Swift change and no
# extra argument are needed to find it.
MODEL_NAME   := ggml-base.en-q5_1.bin
MODEL_SRC    ?= $(HOME)/.cache/voice-helper/models/$(MODEL_NAME)
MODEL_URL    := https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$(MODEL_NAME)

ifeq ($(CORE),rust)
  HELPER_EXE    := voice-helper
  HELPER_PREREQ := helper-rust
else ifeq ($(CORE),python)
  HELPER_EXE    := voice-desktop
  HELPER_PREREQ := helper
else
  $(error CORE must be 'rust' or 'python', not '$(CORE)')
endif

DEVELOPER_ID := $(shell security find-identity -v -p codesigning 2>/dev/null \
	| sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)
IDENTITY ?= $(if $(DEVELOPER_ID),$(DEVELOPER_ID),-)

.PHONY: all app helper helper-rust model swift run check sign verify dmg clean

all: app

# ----- helper (Python core, frozen) -----------------------------------------

# Packaging-time exclusions. These are NOT dependency changes: voice-desktop
# still declares voice-core[whisper,piper,hotword,openai], and
# `voice-desktop dictate --wake-word` still works when run from source. They
# only say "this bundle does not ship that code path".
#
# openwakeword + its scipy/scikit-learn tail (~50 MB): Raneen drives the
# helper with trigger="external" — the hotkey is a Swift keypress, never a
# wake word — so the whole hotword stack is dead weight here. Excluding it
# needs the lazy import in voice_desktop.app; module-scope, PyInstaller
# follows it no matter what is listed below. openwakeword is excluded
# explicitly as well as its two big deps, so that a build which *does* need
# it fails loudly at import rather than deep inside a half-present package.
#
# av (PyAV, ~44 MB of bundled FFmpeg): faster-whisper uses it only in
# `decode_audio`, for reading audio *files*. We always hand it raw PCM
# numpy arrays (voice_core/stt/faster_whisper_engine.py), so nothing in
# this bundle can reach a decode. But faster_whisper/audio.py does
# `import av` at module scope and __init__ re-exports it, so dropping the
# module alone would break `from faster_whisper import WhisperModel` —
# hence the stub runtime hook below, and hence `make check` after any
# faster-whisper bump.
EXCLUDES := openwakeword scipy sklearn av

# The Python sources are prerequisites too. Without them, editing
# voice_core or voice_desktop leaves the frozen helper untouched and the
# bundle silently ships the previous build — which presents as a fix that
# "didn't work" and costs an hour before anyone suspects the Makefile.
HELPER_SOURCES := $(shell find ../../packages/voice-core/src \
                                ../../packages/voice-desktop/src -name '*.py' 2>/dev/null)

build/dist/voice-desktop/voice-desktop: helper_entry.py hooks/*.py rthooks/*.py $(HELPER_SOURCES)
	cd .. && cd .. && uv run --package voice-desktop pyinstaller \
		--noconfirm --clean --onedir \
		--name voice-desktop \
		--additional-hooks-dir apps/$(APP_NAME)/hooks \
		--runtime-hook apps/$(APP_NAME)/rthooks/rthook-stub-av.py \
		$(foreach m,$(EXCLUDES),--exclude-module $(m)) \
		--distpath apps/$(APP_NAME)/build/dist \
		--workpath apps/$(APP_NAME)/build/work \
		--specpath apps/$(APP_NAME)/build \
		apps/$(APP_NAME)/helper_entry.py

helper: build/dist/voice-desktop/voice-desktop

# ----- helper (Rust core) ---------------------------------------------------

# One binary, no dylibs, no frozen interpreter. Cargo's own staleness
# check is finer than ours, so this always delegates rather than guarding
# on a timestamp.
helper-rust: model
	cargo build --release --manifest-path $(RUST_CRATE)/Cargo.toml

# Fetched rather than committed: 57 MB of weights do not belong in git,
# and the URL is stable. Cached in ~/.cache so a clean build does not
# re-download it.
model:
	@if [ ! -f "$(MODEL_SRC)" ]; then \
		echo "downloading $(MODEL_NAME) (57 MB) → $(MODEL_SRC)"; \
		mkdir -p "$$(dirname "$(MODEL_SRC)")"; \
		curl -fL --progress-bar -o "$(MODEL_SRC)" "$(MODEL_URL)"; \
	fi

# ----- swift ----------------------------------------------------------------

swift:
	swift build -c release

# ----- bundle ---------------------------------------------------------------

app: swift $(HELPER_PREREQ)
	@rm -rf "$(BUNDLE)"
	@mkdir -p "$(CONTENTS)/MacOS" "$(CONTENTS)/Resources"
	cp "$(SWIFT_BIN)" "$(CONTENTS)/MacOS/$(APP_NAME)"
	cp Resources/Info.plist "$(CONTENTS)/Info.plist"
	cp Resources/$(APP_NAME).icns "$(CONTENTS)/Resources/"
	cp Resources/MenuBarMark.png "$(CONTENTS)/Resources/"
	@mkdir -p "$(HELPER_DIR)"
ifeq ($(CORE),rust)
	cp "$(RUST_BIN)" "$(HELPER_DIR)/$(HELPER_EXE)"
	cp "$(MODEL_SRC)" "$(HELPER_DIR)/$(MODEL_NAME)"
else
	cp -R build/dist/voice-desktop/. "$(HELPER_DIR)/"
endif
	@echo "built $(BUNDLE) — CORE=$(CORE) ($$(du -sh "$(BUNDLE)" | cut -f1))"

run: app
	"$(CONTENTS)/MacOS/$(APP_NAME)"

# The protocol suite, not a bespoke script: it is the same set of cases CI
# runs, and it drives whichever core is selected.
check: $(HELPER_PREREQ)
	cd ../.. && ./protocol/run-suite.sh $(CORE)

# ----- signing --------------------------------------------------------------

# Order matters: every nested binary first, the bundle last. `--deep` is
# discouraged by Apple and signs inconsistently over a tree this size.
#
# -P 6 because `--timestamp` is a network round trip to Apple *per file*
# and there are ~380 of them: sequentially that is 11+ minutes of mostly
# waiting. Parallel it is closer to two. Measured, not guessed.
#
# The retry is not defensive padding — a sequential run failed partway
# through with a transient timestamp-server error and left the bundle
# half-signed, which then fails verification in a way that looks like a
# code problem. One retry over the whole set is cheap because signing an
# already-signed file just replaces the signature.
SIGN_JOBS ?= 6

sign: app
	@if [ "$(IDENTITY)" = "-" ]; then \
		echo ""; \
		echo "  ****  AD-HOC SIGNING — the hotkey will not work  ****"; \
		echo ""; \
		echo "  No 'Developer ID Application' identity was found in your keychain."; \
		echo "  An ad-hoc signature changes on every build, so macOS treats this as"; \
		echo "  a brand-new app: the Accessibility grant does NOT carry over, the old"; \
		echo "  entry in System Settings still looks ticked, and the event tap fails"; \
		echo "  silently. The menu bar will work and no key will do anything."; \
		echo ""; \
		echo "  Check with:  security find-identity -v -p codesigning"; \
		echo ""; \
	fi
	@echo "signing nested binaries (parallel x$(SIGN_JOBS))…"
	@# With CORE=rust there are no nested dylibs at all — one static binary —
	@# so both passes below find nothing and cost nothing. That is the point:
	@# ~380 timestamped files become 2.
	@find "$(BUNDLE)" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
		| xargs -0 -P $(SIGN_JOBS) -n1 codesign --force --timestamp \
			--options runtime --sign "$(IDENTITY)" 2>&1 \
		| grep -v "replacing existing signature" || true
	@echo "second pass (catches transient timestamp-server failures)…"
	@find "$(BUNDLE)" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
		| xargs -0 -P $(SIGN_JOBS) -n1 codesign --timestamp \
			--options runtime --sign "$(IDENTITY)" 2>&1 \
		| grep -vE "replacing existing signature|is already signed" || true
	codesign --force --timestamp --options runtime \
		--entitlements "$(ENTITLEMENTS)" \
		--sign "$(IDENTITY)" "$(HELPER_DIR)/$(HELPER_EXE)"
	codesign --force --timestamp --options runtime \
		--entitlements "$(ENTITLEMENTS)" \
		--sign "$(IDENTITY)" "$(BUNDLE)"
	@echo "signed with: $(IDENTITY)"

verify:
	codesign --verify --deep --strict --verbose=2 "$(BUNDLE)"
	@echo "--- gatekeeper:"
	-spctl --assess --type execute --verbose "$(BUNDLE)"

# Notarisation needs a Developer ID (not Apple Development / Distribution)
# and credentials stored once with:
#   xcrun notarytool store-credentials raneen --apple-id ... --team-id ... --password <app-specific>
# Staged through a folder containing the app *and* a symlink to
# /Applications, which is what produces the familiar "drag the icon onto
# the folder" window. Pointing hdiutil straight at the .app makes a disk
# image with a lone app in it and no obvious way to install — which is
# exactly what it looked like on the first try.
dmg: sign
	@rm -rf build/dmg build/$(APP_NAME).dmg
	@mkdir -p build/dmg
	cp -R "$(BUNDLE)" build/dmg/
	ln -s /Applications build/dmg/Applications
	hdiutil create -volname "$(APP_NAME)" -srcfolder build/dmg \
		-ov -format UDZO build/$(APP_NAME).dmg
	@rm -rf build/dmg
	@echo "next: xcrun notarytool submit build/$(APP_NAME).dmg --keychain-profile raneen --wait"
	@echo "then: xcrun stapler staple build/$(APP_NAME).dmg"

clean:
	rm -rf build .build
