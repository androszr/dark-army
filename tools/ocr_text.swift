// Read the text in pictures with the Mac's own text recognition (Vision).
//
//     xcrun swift tools/ocr_text.swift docs/images/showcase/*.png
//
// Prints one line per picture: `<path>\t<recognised strings joined by " | ">`.
// An unreadable file prints `<path>\t<unreadable>` and the run exits 2 at the
// end. Used by host/tests/test_showcase_images.py to prove the README's
// showcase slides carry no personal name.
import AppKit
import Vision

var failed = false
for path in CommandLine.arguments.dropFirst() {
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("\(path)\t<unreadable>")
        failed = true
        continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    do {
        try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
    } catch {
        print("\(path)\t<unreadable>")
        failed = true
        continue
    }
    let strings = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    print("\(path)\t\(strings.joined(separator: " | "))")
}
exit(failed ? 2 : 0)
