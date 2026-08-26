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

/// Font-independent rendering of Material Design's `md-skull` (U+F068C).
struct GraveMark: View {
    var color: Color = .primary
    var size: CGFloat = 18
    var body: some View { SkullShape().fill(color, style: FillStyle(eoFill: true)).frame(width: size, height: size) }
}

private struct SkullShape: Shape {
    func path(in rect: CGRect) -> Path {
        let s = min(rect.width, rect.height) / 24
        let x = rect.midX - 12 * s, y = rect.midY - 12 * s
        func p(_ px: CGFloat, _ py: CGFloat) -> CGPoint { CGPoint(x: x + px * s, y: y + py * s) }
        var path = Path()
        path.move(to: p(12, 2))
        path.addArc(center: p(12, 11), radius: 9 * s, startAngle: .degrees(-90), endAngle: .degrees(-180), clockwise: true)
        path.addCurve(to: p(7, 18.47), control1: p(3, 14.03), control2: p(4.53, 16.82))
        path.addLine(to: p(7, 22)); path.addLine(to: p(9, 22)); path.addLine(to: p(9, 19))
        path.addLine(to: p(11, 19)); path.addLine(to: p(11, 22)); path.addLine(to: p(13, 22))
        path.addLine(to: p(13, 19)); path.addLine(to: p(15, 19)); path.addLine(to: p(15, 22))
        path.addLine(to: p(17, 22)); path.addLine(to: p(17, 18.46))
        path.addCurve(to: p(21, 11), control1: p(19.47, 16.81), control2: p(21, 14))
        path.addArc(center: p(12, 11), radius: 9 * s, startAngle: .degrees(0), endAngle: .degrees(-90), clockwise: true)
        path.closeSubpath()
        path.addEllipse(in: CGRect(x: x + 6 * s, y: y + 11 * s, width: 4 * s, height: 4 * s))
        path.addEllipse(in: CGRect(x: x + 14 * s, y: y + 11 * s, width: 4 * s, height: 4 * s))
        path.move(to: p(12, 14)); path.addLine(to: p(13.5, 17)); path.addLine(to: p(10.5, 17)); path.closeSubpath()
        return path
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
    let goodAtHigh: Bool
    init(value: Double?, goodAtHigh: Bool = false) { self.value = value; self.goodAtHigh = goodAtHigh }
    var body: some View { GeometryReader { proxy in ZStack(alignment: .leading) { GraveTheme.hairline; if let value { Rectangle().fill(goodAtHigh ? (value > 85 ? GraveTheme.good : value > 70 ? GraveTheme.amber : GraveTheme.crit) : (value > 85 ? GraveTheme.crit : value > 70 ? GraveTheme.amber : GraveTheme.good)).frame(width: proxy.size.width * min(max(value, 0), 100) / 100) } } }.frame(height: 8).overlay(Rectangle().stroke(GraveTheme.ring)) }
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
