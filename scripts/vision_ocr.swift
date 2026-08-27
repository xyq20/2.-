#!/usr/bin/env swift

import CoreGraphics
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

func imageOrientation(from source: CGImageSource) -> CGImagePropertyOrientation {
    guard
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
        let rawOrientation = properties[kCGImagePropertyOrientation] as? UInt32,
        let orientation = CGImagePropertyOrientation(rawValue: rawOrientation)
    else {
        return .up
    }
    return orientation
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

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(
        cgImage: image,
        orientation: imageOrientation(from: source),
        options: [:]
    )
    do {
        try handler.perform([request])
    } catch {
        throw OCRError.recognitionFailed(error.localizedDescription)
    }

    let tokens = (request.results ?? []).compactMap { observation -> OCRToken? in
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

    do {
        let output = try JSONEncoder().encode(tokens)
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
