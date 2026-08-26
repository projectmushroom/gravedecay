import Foundation

/// Small, platform-neutral interpretation of `git status --porcelain`.
public enum WorkStatus {
    public static func changedFileCount(_ porcelain: String) -> Int {
        porcelain.split(whereSeparator: \.isNewline).count
    }
}
