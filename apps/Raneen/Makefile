# Raneen — Swift menu-bar shell around the Rust core.
#
# The whole bundle is assembled by script rather than by Xcode: layout,
# Info.plist and signing are all reviewable as text, and a clean checkout
# builds the same thing without anyone opening an IDE.
#
#   make app     build the core and the .app
#   make run     run it (foreground, so you see helper logs)
#   make check   run the conformance suite (IMPL=rust|python)
#   make sign    codesign with a Developer ID (needs IDENTITY=...)
#   make dmg     package the signed app
#   make clean
#
# THE BUNDLED CORE IS RUST, ALWAYS.
#
# The PyInstaller path was deleted 2026-08-10 (ROADMAP step 10). It froze
# `voice-desktop` into ~380 files and 187 MB, against one 4 MB static
# binary, and it was the source of the worst packaging bugs this project
# had — see LEARNINGS.md for the two hook fixes it needed to work at all.
#
# **This did not delete the Python reference implementation.** That runs
# from source, and `make check IMPL=python` still conformance-tests it —
# which is the drift protection, and the reason the harness exists. What
# went away is the *packaging*, not the second implementation.

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

RUST_CRATE   := ../../crates/raneen-core
RUST_BIN     := $(RUST_CRATE)/target/release/raneen-core
RUST_SOURCES := $(shell find $(RUST_CRATE)/src -name '*.rs' 2>/dev/null)

# The model ships inside the bundle. `default_model()` in the helper looks
# beside its own executable first, which is why no Swift change and no
# extra argument are needed to find it.
MODEL_NAME   := ggml-base.en-q5_1.bin
MODEL_SRC    ?= $(HOME)/.cache/raneen/models/$(MODEL_NAME)
MODEL_URL    := https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$(MODEL_NAME)

HELPER_EXE := raneen-core

# Which implementation `make check` conformance-tests — a separate question
# from what gets bundled, and conflating the two in one `CORE` variable is
# what made it look as though deleting the packaging would delete the
# reference implementation too.
IMPL ?= rust

DEVELOPER_ID := $(shell security find-identity -v -p codesigning 2>/dev/null \
	| sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)
IDENTITY ?= $(if $(DEVELOPER_ID),$(DEVELOPER_ID),-)

.PHONY: all app helper-rust model swift run check sign verify dmg clean

all: app


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

app: swift helper-rust
	@rm -rf "$(BUNDLE)"
	@mkdir -p "$(CONTENTS)/MacOS" "$(CONTENTS)/Resources"
	cp "$(SWIFT_BIN)" "$(CONTENTS)/MacOS/$(APP_NAME)"
	cp Resources/Info.plist "$(CONTENTS)/Info.plist"
	cp Resources/$(APP_NAME).icns "$(CONTENTS)/Resources/"
	cp Resources/MenuBarMark.png "$(CONTENTS)/Resources/"
	@mkdir -p "$(HELPER_DIR)"
	cp "$(RUST_BIN)" "$(HELPER_DIR)/$(HELPER_EXE)"
	cp "$(MODEL_SRC)" "$(HELPER_DIR)/$(MODEL_NAME)"
	@echo "built $(BUNDLE) — two files ($$(du -sh "$(BUNDLE)" | cut -f1))"

run: app
	"$(CONTENTS)/MacOS/$(APP_NAME)"

# The protocol suite, not a bespoke script: it is the same set of cases CI
# runs, and it drives whichever core is selected.
check: helper-rust
	cd ../.. && ./protocol/run-suite.sh $(IMPL)

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
	@# There are no nested dylibs at all now — one static binary — so both
	@# passes below find nothing and cost nothing. That is the point: the
	@# frozen-Python bundle had ~380 files to timestamp individually, which
	@# is why the parallel workaround and the retry exist. Kept because they
	@# cost nothing and a future dependency could reintroduce dylibs.
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
