// xcindex-helper — Swift binary that walks the Xcode IndexStore and writes
// the SQLite cache that the Python CLI queries.
//
// Subcommands:
//   version      — print {helper_version, schema_version, swift_version}.
//   bootstrap    — create a fresh SQLite at --output PATH (Bootstrap.swift).
//   incremental  — update an existing SQLite at --sqlite PATH given the unit
//                  names that changed (Incremental.swift).

import Foundation

let HELPER_VERSION = "0.4.0"
let SCHEMA_VERSION = 4

// MARK: - Entry point

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else {
    writeStderr("usage: xcindex-helper {version|bootstrap|incremental} [...]")
    exit(1)
}

switch command {
case "version":
    runVersion()
case "bootstrap":
    await runBootstrap(Array(args.dropFirst()))
case "incremental":
    await runIncremental(Array(args.dropFirst()))
default:
    writeStderrJSON(["error": "usage", "detail": "unknown command: \(command)"])
    exit(1)
}

// MARK: - Commands

func runVersion() {
    let payload: [String: Any] = [
        "helper_version": HELPER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "swift_version": detectSwiftVersion() ?? "unknown",
        "capabilities": ["bootstrap", "incremental"],
    ]
    writeStdoutJSON(payload)
}

// MARK: - Index store discovery

enum HelperError: Error, CustomStringConvertible {
    case toolchainNotFound
    case libIndexStoreNotFound(String)

    var description: String {
        switch self {
        case .toolchainNotFound:
            return "could not resolve Swift toolchain via xcrun"
        case .libIndexStoreNotFound(let path):
            return "libIndexStore.dylib not found at expected location: \(path)"
        }
    }
}

func resolveIndexStoreLibrary() throws -> String {
    if let override = ProcessInfo.processInfo.environment["XCINDEX_LIB_INDEXSTORE"],
       FileManager.default.fileExists(atPath: override) {
        return override
    }
    let swiftPath = try runProcessCapturing(["xcrun", "--find", "swift"])
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard !swiftPath.isEmpty else {
        throw HelperError.toolchainNotFound
    }
    let swiftURL = URL(fileURLWithPath: swiftPath)
    let toolchainBin = swiftURL.deletingLastPathComponent()
    let libURL = toolchainBin.deletingLastPathComponent()
        .appendingPathComponent("lib/libIndexStore.dylib")
    let libPath = libURL.path
    guard FileManager.default.fileExists(atPath: libPath) else {
        throw HelperError.libIndexStoreNotFound(libPath)
    }
    return libPath
}

func detectSwiftVersion() -> String? {
    do {
        let output = try runProcessCapturing(["swift", "--version"])
        return output.split(separator: "\n").first.map(String.init)
    } catch {
        return nil
    }
}

// MARK: - Subprocess helper

func runProcessCapturing(_ argv: [String]) throws -> String {
    let process = Process()
    process.launchPath = "/usr/bin/env"
    process.arguments = argv
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = Pipe()
    try process.run()
    process.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return String(decoding: data, as: UTF8.self)
}

// MARK: - JSON helpers (used by every subcommand)

func writeStdoutJSON(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func writeStderrJSON(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
        return
    }
    FileHandle.standardError.write(data)
    FileHandle.standardError.write(Data([0x0A]))
}

func writeStderr(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func exitArgError(_ message: String) -> Never {
    writeStderrJSON(["error": "usage", "detail": message])
    exit(1)
}
