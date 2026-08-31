import Foundation
import SwiftUI
import GravedecayKit

@MainActor
final class AppModel: ObservableObject {
    @Published var box = UserDefaults.standard.string(forKey: "boxHost").flatMap(BoxConfig.init(input:))

    func save() { UserDefaults.standard.set(box?.host, forKey: "boxHost") }
}
