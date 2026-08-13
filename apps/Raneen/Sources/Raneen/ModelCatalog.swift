import Foundation

/// A whisper model the app can fetch for itself.
///
/// Pinned rather than discovered: there is no "list the models" API on the
/// other end that returns sizes and digests, and a picker that shows a
/// remote directory listing would offer the tokenizer files and the
/// coreml archives alongside the weights. A curated list is also the only
/// place the *tradeoff* can be written down, which is the part a person
/// actually needs in order to choose.
struct CatalogModel: Identifiable, Hashable {

    /// The filename on disk, which is also the path component on the
    /// server and what `WhisperModel.isEnglishOnly` reads.
    let filename: String

    let title: String

    /// Exactly what the server will send. Shown before a download starts,
    /// and checked against the response afterwards — a mismatch means the
    /// pin is stale, not that the network is slow.
    let bytes: Int64

    /// SHA-256 of the finished file.
    ///
    /// Verified before the file is given its real name. Without this a
    /// truncated download becomes a model that fails to load with a ggml
    /// error reading like a corrupt build — the same failure the
    /// wake-word fetch script already learned to avoid.
    let sha256: String

    let multilingual: Bool

    /// One line on what choosing this costs and buys.
    let detail: String

    var id: String { filename }

    var url: URL { ModelCatalog.repository.appendingPathComponent(filename) }

    var sizeDescription: String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

enum ModelCatalog {

    /// The same host and repository the Makefile bundles from and CI
    /// caches from. Three conventions for where weights come from is how
    /// one of them ends up unmaintained.
    static let repository = URL(
        string: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/")!

    /// **Nearly everything here is quantised**, because on these weights
    /// it is close to free: `large-v3` is 3.1 GB at full precision and
    /// 1.1 GB as q5_0, for an accuracy difference most dictation will
    /// never notice. The full-precision variants are listed anyway — the
    /// choice belongs to whoever has the disk.
    ///
    /// Sizes and digests were taken from the server's own `content-length`
    /// and `x-linked-etag` (HuggingFace returns the LFS object's SHA-256
    /// there), and the digest scheme was confirmed by hashing the copy of
    /// `base.en-q5_1` already on disk. To add a model, ask for its headers:
    ///
    ///     curl -sI https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-NAME.bin \
    ///       | grep -iE 'x-linked-etag|x-linked-size'
    static let all: [CatalogModel] = [
        CatalogModel(
            filename: "ggml-tiny.en-q5_1.bin",
            title: "Tiny · English",
            bytes: 32_166_155,
            sha256: "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b",
            multilingual: false,
            detail: "Fastest, and the least accurate. Fine for short commands."
        ),
        CatalogModel(
            filename: "ggml-base.en-q5_1.bin",
            title: "Base · English",
            bytes: 59_721_011,
            sha256: "4baf70dd0d7c4247ba2b81fafd9c01005ac77c2f9ef064e00dcf195d0e2fdd2f",
            multilingual: false,
            detail: "The model shipped inside the app. Dependable in a quiet room."
        ),
        CatalogModel(
            filename: "ggml-small.en-q5_1.bin",
            title: "Small · English",
            bytes: 190_098_681,
            sha256: "bfdff4894dcb76bbf647d56263ea2a96645423f1669176f4844a1bf8e478ad30",
            multilingual: false,
            detail: "Clearly better on names, punctuation and accented speech."
        ),
        CatalogModel(
            filename: "ggml-medium.en-q5_0.bin",
            title: "Medium · English",
            bytes: 539_225_533,
            sha256: "76733e26ad8fe1c7a5bf7531a9d41917b2adc0f20f2e4f5531688a8c6cd88eb0",
            multilingual: false,
            detail: "Close to the large models on English, at a third of the size."
        ),
        CatalogModel(
            filename: "ggml-tiny-q5_1.bin",
            title: "Tiny",
            bytes: 32_152_673,
            sha256: "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7",
            multilingual: true,
            detail: "Every language, but too small to be reliable in most of them."
        ),
        CatalogModel(
            filename: "ggml-base-q5_1.bin",
            title: "Base",
            bytes: 59_707_625,
            sha256: "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
            multilingual: true,
            detail: "The smallest model worth using for a language other than English."
        ),
        CatalogModel(
            filename: "ggml-small-q5_1.bin",
            title: "Small",
            bytes: 190_085_487,
            sha256: "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
            multilingual: true,
            detail: "A reasonable balance for everyday multilingual dictation."
        ),
        CatalogModel(
            filename: "ggml-medium-q5_0.bin",
            title: "Medium",
            bytes: 539_212_467,
            sha256: "19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f",
            multilingual: true,
            detail: "Strong across languages. Slower per turn than Small."
        ),
        CatalogModel(
            filename: "ggml-large-v3-turbo-q5_0.bin",
            title: "Large v3 Turbo",
            bytes: 574_041_195,
            sha256: "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
            multilingual: true,
            detail: "Near-large accuracy with a much shorter decode. The best heavy choice."
        ),
        CatalogModel(
            filename: "ggml-large-v3-q5_0.bin",
            title: "Large v3",
            bytes: 1_081_140_203,
            sha256: "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
            multilingual: true,
            detail: "The most accurate model here, and the slowest to answer."
        ),
        CatalogModel(
            filename: "ggml-large-v3-turbo.bin",
            title: "Large v3 Turbo · full precision",
            bytes: 1_624_555_275,
            sha256: "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
            multilingual: true,
            detail: "Unquantised Turbo. Three times the disk for a marginal gain."
        ),
        CatalogModel(
            filename: "ggml-large-v3.bin",
            title: "Large v3 · full precision",
            bytes: 3_095_033_483,
            sha256: "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
            multilingual: true,
            detail: "Everything the model has, resident in memory while it runs."
        ),
    ]

    static func model(named filename: String) -> CatalogModel? {
        all.first { $0.filename == filename }
    }

    /// Grouped for display. English-only and multilingual are genuinely
    /// different products rather than two sizes of one — an `.en` model
    /// given other speech returns confident nonsense instead of failing —
    /// so they are not interleaved by size in one list.
    static var englishOnly: [CatalogModel] { all.filter { !$0.multilingual } }
    static var multilingual: [CatalogModel] { all.filter(\.multilingual) }
}
