# Releasing

How a signed, notarised `Raneen.dmg` gets built, and what it needs from you.

- **The pipeline** → [`.github/workflows/release.yml`](../.github/workflows/release.yml)
- **The gates it runs first** → [`.github/workflows/tests.yml`](../.github/workflows/tests.yml)

---

## Cutting a release

```bash
git tag v0.2.0
git push origin v0.2.0
```

That is the whole procedure. The tag is the version: the workflow stamps
`CFBundleShortVersionString` from it, so the bundle can never disagree with the
release it is attached to.

To prove the signing and notarisation path **without** minting a version, run the
workflow manually from the Actions tab. It builds, signs, notarises and uploads an
artefact, and publishes nothing.

---

## What it does, in order

| | |
| --- | --- |
| 1 | Rust unit tests, Swift unit tests, and the conformance suite in **strict** mode |
| 2 | Stamps the version from the tag and the build number from the run number |
| 3 | Imports the Developer ID certificate into a temporary keychain |
| 4 | `make dmg` — builds the core, assembles the bundle, signs it, packages it |
| 5 | Verifies the signature, then notarises with Apple and staples the ticket |
| 6 | Asks Gatekeeper what it thinks, on the mounted image |
| 7 | Publishes the DMG to a GitHub release (tags only) |
| 8 | Deletes the keychain, even if an earlier step failed |

**The gates run again here on purpose.** A tag can be pushed at a commit whose test
run never finished — or was never triggered — and a signed, notarised artefact is
the one thing that must never ship untested.

**Strict mode matters.** Cases in the conformance suite skip when an optional
dependency is missing, which is right on a laptop and wrong on a build server.
Before `RANEEN_SUITE_STRICT`, CI ran 7 of 11 cases and reported success: the two
wake-word cases and the two ZeroMQ cases were skipped for want of a model download
and `pyzmq`, and the summary line said "7 passed" without saying what had not been
tried.

---

## Secrets

Seven, all under **Settings → Secrets and variables → Actions**.

### Signing

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12` | base64 of a `.p12` holding the **Developer ID Application** certificate *and* its private key |
| `MACOS_CERTIFICATE_PASSWORD` | the password you set while exporting that `.p12` |
| `MACOS_KEYCHAIN_PASSWORD` | any random string — the password for the throwaway keychain CI creates |
| `MACOS_SIGNING_IDENTITY` | the identity's full name, e.g. `Developer ID Application: ACME LLP (TEAMID1234)` |

To produce the first one: **Keychain Access → login → My Certificates**, find the
Developer ID Application entry, expand it to confirm a private key is attached,
right-click → **Export** → `.p12` with a password. Then:

```bash
base64 -i certificate.p12 | tr -d '\n' | pbcopy
```

The `tr -d '\n'` is not decoration — a single-line value avoids any question about
how the decoder treats wrapped input.

For the identity name:

```bash
security find-identity -v -p codesigning
```

`MACOS_SIGNING_IDENTITY` is not really secret — it is embedded in every binary you
have ever signed — but keeping it here keeps the team ID out of build logs, and it
costs nothing.

For `MACOS_KEYCHAIN_PASSWORD`, anything will do:

```bash
openssl rand -base64 24
```

### Notarisation

| Secret | What it is |
| --- | --- |
| `NOTARY_ISSUER_ID` | App Store Connect API **Issuer ID** (a UUID) |
| `NOTARY_KEY_ID` | the API key's **Key ID** |
| `NOTARY_PRIVATE_KEY` | base64 of the `AuthKey_XXXXXXXXXX.p8` file |

**App Store Connect → Users and Access → Integrations → App Store Connect API**.
Generate a **Team Key** with at least the **Developer** role. The Issuer ID is shown
at the top of that page; the Key ID sits beside the key you just made.

```bash
base64 -i AuthKey_XXXXXXXXXX.p8 | tr -d '\n' | pbcopy
```

**The `.p8` downloads exactly once.** Apple will not let you fetch it again — if it
is lost, revoke the key and make another.

An API key is used rather than an Apple ID and app-specific password because it is
scoped to notarisation, revocable on its own, and unaffected by two-factor prompts
on a human's account. If you would rather use the Apple ID route, swap the three
secrets for `NOTARY_APPLE_ID`, `NOTARY_PASSWORD` (an app-specific password from
appleid.apple.com) and `NOTARY_TEAM_ID`, and change the `notarytool submit`
arguments to:

```bash
--apple-id "$NOTARY_APPLE_ID" --password "$NOTARY_PASSWORD" --team-id "$NOTARY_TEAM_ID"
```

---

## Why notarisation is not optional

Signing alone gets a user *"Apple could not verify Raneen is free of malware"* with
no obvious way past it. Notarisation is what turns that into an ordinary open
dialog, and stapling is what lets it work with no network — a first launch on a
plane should not fail.

The workflow proves both rather than assuming them: `stapler validate`, then
`spctl --assess` against the app inside the mounted image, which is the closest a
runner gets to a user's first double-click. Before notarisation, `spctl` reports
`rejected / source=Unnotarized Developer ID`.

---

## Deliberately not signed in `tests.yml`

The `raneen-app` job builds and tests the Swift shell on every push, and needs no
secrets — so it runs on forks and pull requests. Signing lives only in
`release.yml`, where secrets are unavailable to fork PRs by design.

---

## Known gaps

**Wake-word models are not in the bundle.** The core looks beside its own
executable first, then `~/.cache/raneen/wakeword`, and `tools/fetch-wakeword-models.sh`
fills the latter. That is fine for a developer and **wrong for a released app**: the
settings window currently tells a user to run a shell script from a repository they
do not have. Bundling `melspectrogram.onnx`, `embedding_model.onnx` and one
classifier is about 3.2 MB and needs only two `cp` lines in the Makefile — the
blocker is confirming the openWakeWord model licence permits redistribution inside
a signed binary, which is a different question from pip-installing them.

**x86_64 is not built.** An x86_64 build has to choose an AVX baseline at compile
time and then either crashes with SIGILL on older CPUs or leaves performance on
newer ones. See ROADMAP, "deliberately not doing".

The arm64 build is portable across every Apple Silicon Mac **only because
`.cargo/config.toml` sets `GGML_NATIVE=OFF`.** Without it, ggml tunes to whoever
compiled it and a core built on an M2 or later contains `i8mm` instructions that
`SIGILL` on an M1 — the same class of bug as the x86 one, on ARM, and invisible
to whoever built it. Do not remove that setting to chase a benchmark, and do not
"fix" a macOS build failure in ggml by moving to a newer Xcode: that makes it
compile *by enabling the instructions*, which ships the crash. See
LEARNINGS.md, "aarch64 mandates NEON, but ggml opts into more than NEON".

The release job also asserts `uname -m` is `arm64`, because an Intel runner would
produce an artefact identical in name and version to a correct one.

**The Python conformance run is not in CI.** `./protocol/run-suite.sh python` passes
locally and is the drift protection that keeps two implementations honest, but it
needs its ~12 s startup budgeted. Rust only, for now.
