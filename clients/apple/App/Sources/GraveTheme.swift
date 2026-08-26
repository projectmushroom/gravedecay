#if os(macOS)
import SwiftUI

enum GraveTheme {
    static let page = Color(red: 7/255, green: 9/255, blue: 7/255)
    static let surface = Color(red: 10/255, green: 13/255, blue: 10/255)
    static let inset = Color(red: 5/255, green: 7/255, blue: 5/255)
    static let ink = Color(red: 214/255, green: 255/255, blue: 208/255)
    static let ink2 = Color(red: 168/255, green: 230/255, blue: 163/255)
    static let muted = Color(red: 85/255, green: 122/255, blue: 85/255)
    static let hairline = Color(red: 28/255, green: 43/255, blue: 28/255)
    static let ring = Color(red: 46/255, green: 74/255, blue: 46/255)
    static let amber = Color(red: 1, green: 176/255, blue: 0)
    static let accentSoft = Color(red: 125/255, green: 216/255, blue: 125/255)
    static let good = Color(red: 57/255, green: 211/255, blue: 83/255)
    static let crit = Color(red: 1, green: 95/255, blue: 86/255)
    static let mono = Font.system(.body, design: .monospaced)
}

/// Native translation of the Omarchy plugin's 18×18 GraveIcon.qml geometry.
struct GraveMark: View {
    var color: Color = .primary
    var size: CGFloat = 18
    private var scale: CGFloat { size / 18 }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Rectangle().fill(color).frame(width: 4 * scale, height: 8 * scale).offset(x: 7 * scale, y: scale)
            Rectangle().fill(color).frame(width: 10 * scale, height: 3 * scale).offset(x: 4 * scale, y: 4 * scale)
            RoundedRectangle(cornerRadius: 2 * scale).fill(color).frame(width: 12 * scale, height: 8 * scale).offset(x: 3 * scale, y: 9 * scale)
            Rectangle().strokeBorder(.black.opacity(0.45), lineWidth: scale).frame(width: 6 * scale, height: scale).offset(x: 6 * scale, y: 12 * scale)
        }.frame(width: size, height: size)
    }
}

struct GravePanel<Content: View>: View {
    let title: String; @ViewBuilder let content: Content
    init(_ title: String, @ViewBuilder content: () -> Content) { self.title = title; self.content = content() }
    var body: some View {
        content.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(GraveTheme.surface).overlay(Rectangle().stroke(GraveTheme.ring, lineWidth: 1))
            .overlay(alignment: .topLeading) { Text(" \(title.uppercased()) ").font(.system(size: 10, weight: .bold, design: .monospaced)).tracking(1.2).foregroundStyle(GraveTheme.amber).background(GraveTheme.page).offset(x: 9, y: -7) }
    }
}

struct GraveButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { configuration.label.font(.system(size: 11, weight: .bold, design: .monospaced)).foregroundStyle(configuration.isPressed ? GraveTheme.page : GraveTheme.ink).padding(.vertical, 7).padding(.horizontal, 10).background(configuration.isPressed ? GraveTheme.ink2 : .clear).overlay(Rectangle().stroke(GraveTheme.ring)) }
}

struct GraveMeter: View {
    let value: Double?
    var body: some View { GeometryReader { proxy in ZStack(alignment: .leading) { GraveTheme.hairline; if let value { Rectangle().fill(value > 85 ? GraveTheme.crit : value > 70 ? GraveTheme.amber : GraveTheme.good).frame(width: proxy.size.width * min(max(value, 0), 100) / 100) } } }.frame(height: 8).overlay(Rectangle().stroke(GraveTheme.ring)) }
}

private struct GraveAtmosphere: View {
    var body: some View {
        GeometryReader { proxy in
            ZStack {
                Canvas { context, size in
                    for y in stride(from: 1.0, through: size.height, by: 4) {
                        context.fill(Path(CGRect(x: 0, y: y, width: size.width, height: 0.5)), with: .color(GraveTheme.good.opacity(0.022)))
                    }
                }
                RadialGradient(colors: [.clear, GraveTheme.page.opacity(0.42)], center: .center, startRadius: min(proxy.size.width, proxy.size.height) * 0.22, endRadius: max(proxy.size.width, proxy.size.height) * 0.68)
            }
        }.allowsHitTesting(false)
    }
}

extension View {
    func graveRoot() -> some View { self.font(GraveTheme.mono).foregroundStyle(GraveTheme.ink2).background(GraveTheme.page).tint(GraveTheme.amber).overlay(GraveAtmosphere()) }
}
#endif
