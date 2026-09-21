#!/usr/bin/env swift

import CoreGraphics
import CoreImage
import Foundation
import ImageIO
import Vision

struct OCRToken: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

enum OCRError: LocalizedError {
    case invalidArguments
    case unreadableImage(String)
    case recognitionFailed(String)
    case encodingFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidArguments:
            return "用法：vision_ocr.swift <图片路径>"
        case .unreadableImage(let path):
            return "无法读取图片：\(path)"
        case .recognitionFailed(let detail):
            return "Vision OCR 识别失败：\(detail)"
        case .encodingFailed(let detail):
            return "OCR JSON 编码失败：\(detail)"
        }
    }
}

func writeError(_ message: String) {
    let data = Data((message + "\n").utf8)
    FileHandle.standardError.write(data)
}

// 小字号数字在原始分辨率下容易被漏检，而表头字母在放大过度时又会丢失，
// 因此按多个缩放倍数分别识别，再按位置合并结果。
let recognitionScales: [CGFloat] = [1.0, 2.0, 3.0]
let maximumLongSide = 4096
let overlapThreshold = 0.3
let ciContext = CIContext()

// 低对比度图片（例如浅灰底上的白色尺码字母）在正向识别时容易漏字，
// 反相后文字与底色的明暗关系互换，Vision 可以稳定识别，因此额外补一次反相识别。
func invertedImage(_ image: CGImage) -> CGImage? {
    guard let filter = CIFilter(name: "CIColorInvert") else {
        return nil
    }
    filter.setValue(CIImage(cgImage: image), forKey: kCIInputImageKey)
    guard let output = filter.outputImage else {
        return nil
    }
    return ciContext.createCGImage(output, from: output.extent)
}

func merge(_ tokens: [OCRToken], into merged: inout [OCRToken]) {
    for token in tokens {
        let overlapsExisting = merged.contains {
            overlapRatio($0, token) >= overlapThreshold
        }
        if !overlapsExisting {
            merged.append(token)
        }
    }
}

func scaledImage(_ image: CGImage, scale: CGFloat) -> CGImage? {
    let width = Int((CGFloat(image.width) * scale).rounded())
    let height = Int((CGFloat(image.height) * scale).rounded())
    guard max(width, height) <= maximumLongSide else {
        return nil
    }
    guard width > image.width || height > image.height else {
        return nil
    }
    guard
        let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    else {
        return nil
    }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return context.makeImage()
}

func overlapRatio(_ left: OCRToken, _ right: OCRToken) -> Double {
    let intersectionWidth =
        min(left.x + left.width, right.x + right.width) - max(left.x, right.x)
    let intersectionHeight =
        min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    guard intersectionWidth > 0, intersectionHeight > 0 else {
        return 0
    }
    let intersection = intersectionWidth * intersectionHeight
    let union = left.width * left.height + right.width * right.height - intersection
    guard union > 0 else {
        return 0
    }
    return intersection / union
}

func imageOrientation(from source: CGImageSource) -> CGImagePropertyOrientation {
    guard
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
        let rawOrientation = properties[kCGImagePropertyOrientation] as? NSNumber,
        let orientation = CGImagePropertyOrientation(rawValue: rawOrientation.uint32Value)
    else {
        return .up
    }
    return orientation
}

func recognize(
    _ image: CGImage,
    orientation: CGImagePropertyOrientation
) throws -> [OCRToken] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(
        cgImage: image,
        orientation: orientation,
        options: [:]
    )
    do {
        try handler.perform([request])
    } catch {
        throw OCRError.recognitionFailed(error.localizedDescription)
    }

    return (request.results ?? []).compactMap { observation -> OCRToken? in
        guard let candidate = observation.topCandidates(1).first else {
            return nil
        }
        let box = observation.boundingBox
        return OCRToken(
            text: candidate.string,
            confidence: candidate.confidence,
            x: box.origin.x,
            y: 1.0 - box.origin.y - box.height,
            width: box.width,
            height: box.height
        )
    }
}

do {
    guard CommandLine.arguments.count == 2 else {
        throw OCRError.invalidArguments
    }

    let imagePath = CommandLine.arguments[1]
    let imageURL = URL(fileURLWithPath: imagePath)
    guard
        let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        throw OCRError.unreadableImage(imagePath)
    }

    let orientation = imageOrientation(from: source)
    var merged: [OCRToken] = []
    for scale in recognitionScales {
        let candidate: CGImage
        if scale <= 1.0 {
            candidate = image
        } else {
            guard let enlarged = scaledImage(image, scale: scale) else {
                continue
            }
            candidate = enlarged
        }
        merge(try recognize(candidate, orientation: orientation), into: &merged)
    }

    if let inverted = invertedImage(image) {
        merge(try recognize(inverted, orientation: orientation), into: &merged)
    }

    do {
        let output = try JSONEncoder().encode(merged)
        FileHandle.standardOutput.write(output)
        FileHandle.standardOutput.write(Data("\n".utf8))
    } catch {
        throw OCRError.encodingFailed(error.localizedDescription)
    }
} catch {
    let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    writeError(message)
    exit(EXIT_FAILURE)
}
