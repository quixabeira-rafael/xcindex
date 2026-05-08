import Foundation
import IndexStoreDB

let HELPER_VERSION = "0.2.0"
let SCHEMA_VERSION = 2

// MARK: - Entry point

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else {
    writeStderr("usage: xcindex-helper {version|dump|dump-files} --index-store PATH [...]")
    exit(1)
}

switch command {
case "version":
    runVersion()
case "dump":
    runDump(Array(args.dropFirst()))
case "dump-files":
    runDumpFiles(Array(args.dropFirst()))
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
    ]
    writeStdoutJSON(payload)
}

func runDump(_ args: [String]) {
    var indexStorePath: String? = nil
    var includeSystem = false
    var i = 0
    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--index-store":
            i += 1
            guard i < args.count else { exitArgError("--index-store requires a value") }
            indexStorePath = args[i]
        case "--include-system":
            includeSystem = true
        default:
            exitArgError("unknown argument: \(arg)")
        }
        i += 1
    }
    guard let path = indexStorePath else {
        exitArgError("--index-store PATH is required")
    }

    do {
        try dumpIndexStore(at: path, includeSystem: includeSystem)
    } catch {
        writeStderrJSON([
            "error": "dump_failed",
            "detail": "\(error)",
        ])
        exit(2)
    }
}

// MARK: - Dump

func dumpIndexStore(at path: String, includeSystem: Bool) throws {
    let libPath = try resolveIndexStoreLibrary()
    let library = try IndexStoreLibrary(dylibPath: libPath)

    let dbDir = NSTemporaryDirectory() + "xcindex-helper-" + UUID().uuidString
    try FileManager.default.createDirectory(
        atPath: dbDir,
        withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(atPath: dbDir) }

    let db = try IndexStoreDB(
        storePath: path,
        databasePath: dbDir,
        library: library,
        waitUntilDoneInitializing: true,
        listenToUnitEvents: false
    )

    db.pollForUnitChangesAndWait()

    // IndexStoreDB uses LMDB; nested `forEach` traversals reuse reader slots and crash
    // (MDB_BAD_RSLOT). We collect names → USRs → emit symbols and occurrences in
    // sequential phases so at most one LMDB iteration is active at a time.

    var allNames: [String] = []
    let _ = db.forEachSymbolName { name in
        allNames.append(name)
        return true
    }

    var canonicalSymbols: [(Symbol, SymbolLocation)] = []
    var seenUSRs = Set<String>()
    for name in allNames {
        let _ = db.forEachCanonicalSymbolOccurrence(byName: name) { canonical in
            let usr = canonical.symbol.usr
            if seenUSRs.contains(usr) {
                return true
            }
            if !includeSystem && canonical.location.isSystem {
                return true
            }
            seenUSRs.insert(usr)
            canonicalSymbols.append((canonical.symbol, canonical.location))
            return true
        }
    }

    var emittedCount = (symbols: 0, occurrences: 0, relations: 0, fileUnits: 0)
    for (symbol, location) in canonicalSymbols {
        emitSymbol(symbol, location: location)
        emittedCount.symbols += 1
    }

    var occurrenceID = 0
    var seenFiles = Set<String>()
    for (symbol, _) in canonicalSymbols {
        let _ = db.forEachSymbolOccurrence(byUSR: symbol.usr, roles: .all) { occ in
            if !includeSystem && occ.location.isSystem {
                return true
            }
            occurrenceID += 1
            seenFiles.insert(occ.location.path)
            let containerUSR = extractContainerUSR(from: occ)
            emitOccurrence(occ, id: occurrenceID, containerUSR: containerUSR)
            emittedCount.occurrences += 1
            for relation in occ.relations {
                emitRelation(occurrenceID: occurrenceID, relation: relation)
                emittedCount.relations += 1
            }
            return true
        }
    }

    // file_unit map: needed for incremental update bookkeeping in v2 schema.
    // Emitted at the end so the consumer can flush per-file batches sequentially.
    for file in seenFiles.sorted() {
        let _ = db.forEachUnitNameContainingFile(path: file) { unitName in
            emitFileUnit(file: file, unitName: unitName)
            emittedCount.fileUnits += 1
            return true
        }
    }

    writeStderrJSON([
        "info": "dump_complete",
        "symbols": emittedCount.symbols,
        "occurrences": emittedCount.occurrences,
        "relations": emittedCount.relations,
        "file_units": emittedCount.fileUnits,
    ])
}

// MARK: - dump-files

func runDumpFiles(_ args: [String]) {
    var indexStorePath: String? = nil
    var files: [String] = []
    var includeSystem = false
    var i = 0
    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--index-store":
            i += 1
            guard i < args.count else { exitArgError("--index-store requires a value") }
            indexStorePath = args[i]
        case "--file":
            i += 1
            guard i < args.count else { exitArgError("--file requires a value") }
            files.append(args[i])
        case "--include-system":
            includeSystem = true
        default:
            exitArgError("unknown argument: \(arg)")
        }
        i += 1
    }
    guard let path = indexStorePath else {
        exitArgError("--index-store PATH is required")
    }
    if files.isEmpty {
        exitArgError("at least one --file PATH is required")
    }

    do {
        try dumpFiles(at: path, files: files, includeSystem: includeSystem)
    } catch {
        writeStderrJSON([
            "error": "dump_failed",
            "detail": "\(error)",
        ])
        exit(2)
    }
}

func dumpFiles(at path: String, files: [String], includeSystem: Bool) throws {
    let libPath = try resolveIndexStoreLibrary()
    let library = try IndexStoreLibrary(dylibPath: libPath)

    let dbDir = NSTemporaryDirectory() + "xcindex-helper-" + UUID().uuidString
    try FileManager.default.createDirectory(
        atPath: dbDir,
        withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(atPath: dbDir) }

    let db = try IndexStoreDB(
        storePath: path,
        databasePath: dbDir,
        library: library,
        waitUntilDoneInitializing: true,
        listenToUnitEvents: false
    )

    db.pollForUnitChangesAndWait()

    var occurrenceID = 0
    var emittedSymbols = Set<String>()
    var emittedCount = (symbols: 0, occurrences: 0, relations: 0, fileUnits: 0)

    for file in files {
        let occs = db.symbolOccurrences(inFilePath: file)
        for occ in occs {
            if !includeSystem && occ.location.isSystem {
                continue
            }
            // For symbols whose definition site is in this file, emit the symbol
            // record so v2 cache stays consistent with the DELETE-by-file step.
            if occ.roles.contains(.definition) && !emittedSymbols.contains(occ.symbol.usr) {
                emittedSymbols.insert(occ.symbol.usr)
                emitSymbol(occ.symbol, location: occ.location)
                emittedCount.symbols += 1
            }
            occurrenceID += 1
            let containerUSR = extractContainerUSR(from: occ)
            emitOccurrence(occ, id: occurrenceID, containerUSR: containerUSR)
            emittedCount.occurrences += 1
            for relation in occ.relations {
                emitRelation(occurrenceID: occurrenceID, relation: relation)
                emittedCount.relations += 1
            }
        }
    }

    for file in files {
        let _ = db.forEachUnitNameContainingFile(path: file) { unitName in
            emitFileUnit(file: file, unitName: unitName)
            emittedCount.fileUnits += 1
            return true
        }
    }

    writeStderrJSON([
        "info": "dump_files_complete",
        "files": files.count,
        "symbols": emittedCount.symbols,
        "occurrences": emittedCount.occurrences,
        "relations": emittedCount.relations,
        "file_units": emittedCount.fileUnits,
    ])
}

// MARK: - Emitters

func emitSymbol(_ symbol: Symbol, location: SymbolLocation) {
    let payload: [String: Any] = [
        "type": "symbol",
        "usr": symbol.usr,
        "name": symbol.name,
        "kind": kindString(symbol.kind),
        "sub_kind": subKindString(symbol.subKind),
        "language": languageString(symbol.language),
        "module": location.moduleName,
        "file": location.path,
        "line": location.line,
        "is_system": location.isSystem,
        "properties": NSNumber(value: symbol.properties.rawValue),
    ]
    writeStdoutJSON(payload)
}

func emitOccurrence(_ occ: SymbolOccurrence, id: Int, containerUSR: String?) {
    let payload: [String: Any] = [
        "type": "occurrence",
        "id": id,
        "symbol_usr": occ.symbol.usr,
        "file": occ.location.path,
        "line": occ.location.line,
        "column": occ.location.utf8Column,
        "roles": NSNumber(value: occ.roles.rawValue),
        "container_usr": (containerUSR as Any?) ?? NSNull(),
    ]
    writeStdoutJSON(payload)
}

func emitFileUnit(file: String, unitName: String) {
    let payload: [String: Any] = [
        "type": "file_unit",
        "file": file,
        "unit_name": unitName,
    ]
    writeStdoutJSON(payload)
}

func emitRelation(occurrenceID: Int, relation: SymbolRelation) {
    let payload: [String: Any] = [
        "type": "relation",
        "occurrence_id": occurrenceID,
        "related_usr": relation.symbol.usr,
        "related_name": relation.symbol.name,
        "kind": primaryRelationKind(relation.roles),
        "roles": NSNumber(value: relation.roles.rawValue),
    ]
    writeStdoutJSON(payload)
}

// MARK: - Mapping helpers

func extractContainerUSR(from occ: SymbolOccurrence) -> String? {
    for relation in occ.relations {
        if relation.roles.contains(.containedBy) {
            return relation.symbol.usr
        }
    }
    return nil
}

func kindString(_ kind: IndexSymbolKind) -> String {
    switch kind {
    case .unknown:           return "unknown"
    case .module:            return "module"
    case .namespace:         return "namespace"
    case .namespaceAlias:    return "namespace-alias"
    case .macro:             return "macro"
    case .enum:              return "enum"
    case .struct:            return "struct"
    case .class:             return "class"
    case .protocol:          return "protocol"
    case .extension:         return "extension"
    case .union:             return "union"
    case .typealias:         return "typealias"
    case .function:          return "function"
    case .variable:          return "variable"
    case .field:             return "field"
    case .enumConstant:      return "enum-case"
    case .instanceMethod:    return "instance-method"
    case .classMethod:       return "class-method"
    case .staticMethod:      return "static-method"
    case .instanceProperty:  return "instance-property"
    case .classProperty:     return "class-property"
    case .staticProperty:    return "static-property"
    case .constructor:       return "constructor"
    case .destructor:        return "destructor"
    case .conversionFunction: return "conversion-function"
    case .parameter:         return "parameter"
    case .using:             return "using"
    case .concept:           return "concept"
    case .commentTag:        return "comment-tag"
    @unknown default:        return "unknown"
    }
}

func languageString(_ language: Language) -> String {
    switch language {
    case .c:    return "c"
    case .cxx:  return "cxx"
    case .objc: return "objc"
    case .swift: return "swift"
    @unknown default: return "unknown"
    }
}

func subKindString(_ subKind: IndexSymbolSubKind) -> Any {
    switch subKind {
    case .none: return NSNull()
    case .cxxCopyConstructor:           return "cxx-copy-constructor"
    case .cxxMoveConstructor:           return "cxx-move-constructor"
    case .accessorGetter:               return "accessor-getter"
    case .accessorSetter:               return "accessor-setter"
    case .swiftAccessorWillSet:         return "swift-accessor-willset"
    case .swiftAccessorDidSet:          return "swift-accessor-didset"
    case .swiftAccessorAddressor:       return "swift-accessor-addressor"
    case .swiftAccessorMutableAddressor: return "swift-accessor-mutable-addressor"
    case .swiftExtensionOfStruct:       return "swift-extension-of-struct"
    case .swiftExtensionOfClass:        return "swift-extension-of-class"
    case .swiftExtensionOfEnum:         return "swift-extension-of-enum"
    case .swiftExtensionOfProtocol:     return "swift-extension-of-protocol"
    case .swiftPrefixOperator:          return "swift-prefix-operator"
    case .swiftPostfixOperator:         return "swift-postfix-operator"
    case .swiftInfixOperator:           return "swift-infix-operator"
    case .swiftSubscript:               return "swift-subscript"
    case .swiftAssociatedType:          return "swift-associated-type"
    case .swiftGenericTypeParam:        return "swift-generic-type-param"
    @unknown default:                   return NSNull()
    }
}

func primaryRelationKind(_ roles: SymbolRole) -> String {
    if roles.contains(.childOf)            { return "childOf" }
    if roles.contains(.baseOf)             { return "baseOf" }
    if roles.contains(.overrideOf)         { return "overrideOf" }
    if roles.contains(.receivedBy)         { return "receivedBy" }
    if roles.contains(.calledBy)           { return "calledBy" }
    if roles.contains(.extendedBy)         { return "extendedBy" }
    if roles.contains(.accessorOf)         { return "accessorOf" }
    if roles.contains(.containedBy)        { return "containedBy" }
    if roles.contains(.ibTypeOf)           { return "ibTypeOf" }
    if roles.contains(.specializationOf)   { return "specializationOf" }
    return "other"
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

// MARK: - JSON helpers

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
