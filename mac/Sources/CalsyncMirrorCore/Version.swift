/// The version, written in exactly one place.
///
/// Same rule as `calsync.__version__`, and for the same reason recorded in
/// `pyproject.toml`: two literals drifted for a whole release without anything
/// noticing. `install.sh` reads this out of the built binary rather than
/// carrying its own copy, so the number in `Info.plist` cannot disagree with
/// the number the tool reports.
///
/// Tracked separately from calsync's version on purpose. This is a different
/// program with its own lifecycle — it mirrors whatever Radicale holds, and a
/// shared number would imply a compatibility relationship that does not exist.
/// Named `Build` rather than after the tool: the CLI's own `@main` type is
/// `CalsyncMirror`, and a Core type sharing that name is shadowed inside it —
/// silently resolving to the wrong thing rather than failing loudly.
public enum Build {
    public static let version = "0.1.0"
    public static let name = "calsync-mirror"
}
