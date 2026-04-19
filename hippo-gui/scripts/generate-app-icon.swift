#!/usr/bin/env swift

import AppKit
import Foundation

struct IconSlot {
    let filename: String
    let pointSize: Int
    let scale: Int

    var pixelSize: Int { pointSize * scale }
}

let slots: [IconSlot] = [
    .init(filename: "icon_16x16.png", pointSize: 16, scale: 1),
    .init(filename: "icon_16x16@2x.png", pointSize: 16, scale: 2),
    .init(filename: "icon_32x32.png", pointSize: 32, scale: 1),
    .init(filename: "icon_32x32@2x.png", pointSize: 32, scale: 2),
    .init(filename: "icon_128x128.png", pointSize: 128, scale: 1),
    .init(filename: "icon_128x128@2x.png", pointSize: 128, scale: 2),
    .init(filename: "icon_256x256.png", pointSize: 256, scale: 1),
    .init(filename: "icon_256x256@2x.png", pointSize: 256, scale: 2),
    .init(filename: "icon_512x512.png", pointSize: 512, scale: 1),
    .init(filename: "icon_512x512@2x.png", pointSize: 512, scale: 2),
]

let fm = FileManager.default
let root = URL(fileURLWithPath: fm.currentDirectoryPath)
let assetDir = root
    .appendingPathComponent("Resources", isDirectory: true)
    .appendingPathComponent("Assets.xcassets", isDirectory: true)
    .appendingPathComponent("AppIcon.appiconset", isDirectory: true)

try fm.createDirectory(at: assetDir, withIntermediateDirectories: true)

func color(_ hex: UInt32, alpha: CGFloat = 1.0) -> NSColor {
    let red = CGFloat((hex >> 16) & 0xFF) / 255.0
    let green = CGFloat((hex >> 8) & 0xFF) / 255.0
    let blue = CGFloat(hex & 0xFF) / 255.0
    return NSColor(red: red, green: green, blue: blue, alpha: alpha)
}

func roundedRectPath(_ rect: CGRect, radius: CGFloat) -> NSBezierPath {
    NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
}

func ellipse(_ rect: CGRect) -> NSBezierPath {
    NSBezierPath(ovalIn: rect)
}

func strokeSpiral(in contextRect: CGRect) {
    let spiral = NSBezierPath()
    spiral.lineWidth = max(contextRect.width * 0.028, 3)
    spiral.lineCapStyle = .round
    spiral.lineJoinStyle = .round

    let center = CGPoint(x: contextRect.midX + contextRect.width * 0.1, y: contextRect.midY + contextRect.height * 0.12)
    let startRadius = contextRect.width * 0.02
    let endRadius = contextRect.width * 0.18
    let turns = 1.8
    let steps = 120

    for step in 0...steps {
        let t = CGFloat(step) / CGFloat(steps)
        let angle = t * turns * 2 * .pi - .pi * 0.35
        let radius = startRadius + (endRadius - startRadius) * t
        let x = center.x + cos(angle) * radius
        let y = center.y + sin(angle) * radius
        if step == 0 {
            spiral.move(to: CGPoint(x: x, y: y))
        } else {
            spiral.line(to: CGPoint(x: x, y: y))
        }
    }

    let tailStart = spiral.currentPoint
    spiral.curve(
        to: CGPoint(x: contextRect.midX + contextRect.width * 0.27, y: contextRect.midY - contextRect.height * 0.18),
        controlPoint1: CGPoint(x: tailStart.x + contextRect.width * 0.08, y: tailStart.y - contextRect.height * 0.02),
        controlPoint2: CGPoint(x: contextRect.midX + contextRect.width * 0.24, y: contextRect.midY - contextRect.height * 0.02)
    )

    let glow = spiral.copy() as! NSBezierPath
    glow.lineWidth = spiral.lineWidth * 1.9
    color(0xFFF2A6, alpha: 0.18).setStroke()
    glow.stroke()

    color(0xF9C74F).setStroke()
    spiral.stroke()
}

func drawIcon(size: Int) throws -> NSBitmapImageRep {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: size,
        pixelsHigh: size,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw NSError(domain: "HippoGUIIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "Failed to create bitmap context"])
    }

    bitmap.size = NSSize(width: size, height: size)

    NSGraphicsContext.saveGraphicsState()
    guard let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw NSError(domain: "HippoGUIIcon", code: 3, userInfo: [NSLocalizedDescriptionKey: "Failed to create graphics context"])
    }
    NSGraphicsContext.current = context
    defer { NSGraphicsContext.restoreGraphicsState() }

    let rect = CGRect(x: 0, y: 0, width: size, height: size)

    let background = roundedRectPath(rect.insetBy(dx: rect.width * 0.02, dy: rect.height * 0.02), radius: rect.width * 0.225)
    let gradient = NSGradient(colors: [color(0x6C5CE7), color(0x1B4965)])!
    gradient.draw(in: background, angle: -65)

    color(0xFFFFFF, alpha: 0.09).setFill()
    roundedRectPath(
        CGRect(x: rect.width * 0.09, y: rect.height * 0.58, width: rect.width * 0.82, height: rect.height * 0.24),
        radius: rect.width * 0.12
    ).fill()

    let hippoColor = color(0x202A44)
    let hippoHighlight = color(0x34456B)
    let muzzleColor = color(0xE6EEF8)
    let muzzleShadow = color(0xB8C6DA)

    hippoColor.setFill()
    ellipse(CGRect(x: rect.width * 0.19, y: rect.height * 0.22, width: rect.width * 0.62, height: rect.height * 0.55)).fill()

    hippoHighlight.setFill()
    ellipse(CGRect(x: rect.width * 0.22, y: rect.height * 0.47, width: rect.width * 0.38, height: rect.height * 0.18)).fill()

    ellipse(CGRect(x: rect.width * 0.20, y: rect.height * 0.58, width: rect.width * 0.17, height: rect.height * 0.17)).fill()
    ellipse(CGRect(x: rect.width * 0.63, y: rect.height * 0.58, width: rect.width * 0.17, height: rect.height * 0.17)).fill()

    muzzleShadow.setFill()
    ellipse(CGRect(x: rect.width * 0.26, y: rect.height * 0.24, width: rect.width * 0.48, height: rect.height * 0.26)).fill()
    muzzleColor.setFill()
    ellipse(CGRect(x: rect.width * 0.27, y: rect.height * 0.255, width: rect.width * 0.46, height: rect.height * 0.235)).fill()

    hippoColor.setFill()
    ellipse(CGRect(x: rect.width * 0.36, y: rect.height * 0.40, width: rect.width * 0.06, height: rect.height * 0.08)).fill()
    ellipse(CGRect(x: rect.width * 0.58, y: rect.height * 0.40, width: rect.width * 0.06, height: rect.height * 0.08)).fill()

    color(0x121A2B).setFill()
    ellipse(CGRect(x: rect.width * 0.40, y: rect.height * 0.30, width: rect.width * 0.055, height: rect.height * 0.085)).fill()
    ellipse(CGRect(x: rect.width * 0.545, y: rect.height * 0.30, width: rect.width * 0.055, height: rect.height * 0.085)).fill()

    color(0xFFFFFF, alpha: 0.92).setFill()
    ellipse(CGRect(x: rect.width * 0.377, y: rect.height * 0.433, width: rect.width * 0.018, height: rect.height * 0.024)).fill()
    ellipse(CGRect(x: rect.width * 0.597, y: rect.height * 0.433, width: rect.width * 0.018, height: rect.height * 0.024)).fill()

    let smile = NSBezierPath()
    smile.lineWidth = max(rect.width * 0.018, 2)
    smile.lineCapStyle = .round
    smile.move(to: CGPoint(x: rect.width * 0.40, y: rect.height * 0.27))
    smile.curve(
        to: CGPoint(x: rect.width * 0.60, y: rect.height * 0.27),
        controlPoint1: CGPoint(x: rect.width * 0.45, y: rect.height * 0.22),
        controlPoint2: CGPoint(x: rect.width * 0.55, y: rect.height * 0.22)
    )
    color(0x7183A8, alpha: 0.9).setStroke()
    smile.stroke()

    strokeSpiral(in: rect)

    color(0xFFFFFF, alpha: 0.14).setStroke()
    let rim = roundedRectPath(rect.insetBy(dx: rect.width * 0.03, dy: rect.height * 0.03), radius: rect.width * 0.21)
    rim.lineWidth = max(rect.width * 0.012, 2)
    rim.stroke()

    return bitmap
}

func pngData(from bitmap: NSBitmapImageRep) throws -> Data {
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "HippoGUIIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "Failed to encode PNG"])
    }
    return png
}

for slot in slots {
    let bitmap = try drawIcon(size: slot.pixelSize)
    let data = try pngData(from: bitmap)
    try data.write(to: assetDir.appendingPathComponent(slot.filename))
}

let contents = """
{
  "images" : [
    { "filename" : "icon_16x16.png", "idiom" : "mac", "scale" : "1x", "size" : "16x16" },
    { "filename" : "icon_16x16@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "16x16" },
    { "filename" : "icon_32x32.png", "idiom" : "mac", "scale" : "1x", "size" : "32x32" },
    { "filename" : "icon_32x32@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "32x32" },
    { "filename" : "icon_128x128.png", "idiom" : "mac", "scale" : "1x", "size" : "128x128" },
    { "filename" : "icon_128x128@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "128x128" },
    { "filename" : "icon_256x256.png", "idiom" : "mac", "scale" : "1x", "size" : "256x256" },
    { "filename" : "icon_256x256@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "256x256" },
    { "filename" : "icon_512x512.png", "idiom" : "mac", "scale" : "1x", "size" : "512x512" },
    { "filename" : "icon_512x512@2x.png", "idiom" : "mac", "scale" : "2x", "size" : "512x512" }
  ],
  "info" : {
    "author" : "xcode",
    "version" : 1
  }
}
"""

try contents.write(to: assetDir.appendingPathComponent("Contents.json"), atomically: true, encoding: .utf8)
print("Generated AppIcon.appiconset at \(assetDir.path)")
