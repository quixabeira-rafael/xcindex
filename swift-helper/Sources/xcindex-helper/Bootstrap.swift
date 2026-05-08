// `bootstrap` subcommand: walks the IndexStore via the `IndexStore` Swift
// wrapper (raw libIndexStore C API), writes a fresh SQLite cache through
// `SQLiteWriter`, atomic-renames into place on success.

import Foundation
import IndexStore
import Darwin

func runBootstrap(_ args: [String]) async {
    var indexStorePath: String? = nil
    var outputPath: String? = nil
    var includeSystem = false
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--index-store":
            i += 1
            guard i < args.count else { exitArgError("--index-store requires a value") }
            indexStorePath = args[i]
        case "--output":
            i += 1
            guard i < args.count else { exitArgError("--output requires a value") }
            outputPath = args[i]
        case "--include-system":
            includeSystem = true
        default:
            exitArgError("unknown argument: \(args[i])")
        }
        i += 1
    }
    guard let indexStorePath = indexStorePath else {
        exitArgError("--index-store PATH is required")
    }
    guard let outputPath = outputPath else {
        exitArgError("--output PATH is required")
    }

    do {
        try await performBootstrap(
            indexStorePath: indexStorePath,
            outputPath: outputPath,
            includeSystem: includeSystem
        )
    } catch {
        writeStderrJSON([
            "error": "bootstrap_failed",
            "detail": "\(error)",
        ])
        exit(2)
    }
}

private func performBootstrap(
    indexStorePath: String,
    outputPath: String,
    includeSystem: Bool
) async throws {
    let totalStart = monotonicSeconds()

    let libPathStr = try resolveIndexStoreLibrary()
    let libURL = URL(fileURLWithPath: libPathStr)
    let library = try await IndexStoreLibrary.at(dylibPath: libURL)
    let store = try library.indexStore(at: URL(fileURLWithPath: indexStorePath))

    // Stage SQLite at <output>.tmp.<pid>; atomic rename on success.
    let tempPath = "\(outputPath).tmp.\(getpid())"
    try? FileManager.default.removeItem(atPath: tempPath)

    let writer = try SQLiteWriter(path: tempPath)
    try writer.applyPragmas(Schema.writePragmas)
    try writer.applySchema(Schema.createStatements)
    try writer.prepareInsertStatements()

    // Phase A: enumerate unit names.
    var unitNames: [String] = []
    try store.unitNames(sorted: false).forEach { ref in
        unitNames.append(ref.string)
        return .continue
    }

    // Phase B: walk units. Build:
    //   - recordNames (distinct)
    //   - recordToFile: record name → source file path (used by phase C for
    //     occurrence.file column)
    //   - recordToUnitInfo: record name → (module, file) of the FIRST non-system
    //     unit that referenced this record (used to populate symbol.module / file)
    var recordNames: [String] = []
    var recordSeen = Set<String>()
    var recordToFile: [String: String] = [:]
    var recordToUnitInfo: [String: (module: String, file: String)] = [:]
    var systemUnitsSkipped = 0
    var unitFilesEmitted = 0

    let unitsDirPath = (indexStorePath as NSString).appendingPathComponent("v5/units")

    try writer.beginTransaction()
    for unitName in unitNames {
        // Snapshot the units table with disk stat for EVERY unit, including
        // system. Python's compute_unit_delta walks the units dir without
        // filtering system; if we skipped them here, all system units would
        // look "added" on the next invocation and force a full re-bootstrap.
        let (sizeBytes, mtimeNs) = unitFileStat(unitsDir: unitsDirPath, unitName: unitName)

        guard let unit = try? store.unit(named: unitName) else {
            // We can't open the unit but we still record its presence so the
            // delta detector matches.
            writer.insertUnit(
                name: unitName, mainFile: nil, module: nil, target: nil,
                provider: nil, mtimeNs: mtimeNs, sizeBytes: sizeBytes
            )
            continue
        }
        let isSystem = unit.isSystemUnit
        let mainFile = unit.hasMainFile ? unit.mainFile.string : ""
        let module = unit.moduleName.string
        let target = unit.target.string
        let provider = unit.providerIdentifier.string

        writer.insertUnit(
            name: unitName,
            mainFile: mainFile.isEmpty ? nil : mainFile,
            module: module.isEmpty ? nil : module,
            target: target.isEmpty ? nil : target,
            provider: provider.isEmpty ? nil : provider,
            mtimeNs: mtimeNs,
            sizeBytes: sizeBytes
        )

        if !includeSystem && isSystem {
            systemUnitsSkipped += 1
            continue
        }

        try unit.dependencies.forEach { dep in
            if dep.kind == .record && (includeSystem || !dep.isSystem) {
                let recordName = dep.name.string
                let filePath = dep.filePath.string
                if !recordSeen.contains(recordName) {
                    recordSeen.insert(recordName)
                    recordNames.append(recordName)
                    recordToFile[recordName] = filePath
                    recordToUnitInfo[recordName] = (module: module, file: filePath)
                }
                if !filePath.isEmpty {
                    writer.insertUnitFile(unitName: unitName, file: filePath)
                    unitFilesEmitted += 1
                }
            }
            return .continue
        }
    }
    try writer.commit()

    // Phase C: walk distinct records. For each:
    //   1. Pre-pass over occurrences to track per-USR definition site (line,
    //      column) — used to populate the symbol row's file/line.
    //   2. Emit occurrences + relations.
    //   3. Emit symbols (deduped globally by USR), filling file/line from the
    //      def-site map collected in step 1.
    var emittedSymbols = Set<String>()
    var symbolCount = 0
    var occurrenceCount = 0
    var relationCount = 0

    try writer.beginTransaction()
    for recordName in recordNames {
        guard let record = try? store.record(named: recordName) else { continue }
        let fileForRecord = recordToFile[recordName] ?? ""
        let info = recordToUnitInfo[recordName]

        // Pass 1: collect definition sites for symbols defined in THIS record.
        var defSiteByUSR: [String: (line: Int, column: Int)] = [:]
        try record.occurrences.forEach { occ in
            if occ.roles.contains(.definition) {
                let usr = occ.symbol.usr.string
                if defSiteByUSR[usr] == nil {
                    defSiteByUSR[usr] = (occ.position.line, occ.position.column)
                }
            }
            return .continue
        }

        // Pass 2: emit occurrences + relations.
        try record.occurrences.forEach { occ in
            let usr = occ.symbol.usr.string
            var containerUSR: String? = nil
            try occ.relations.forEach { rel in
                if rel.roles.contains(.containedBy) {
                    containerUSR = rel.symbol.usr.string
                    return .stop
                }
                return .continue
            }

            let occID = writer.insertOccurrence(
                symbolUSR: usr,
                file: fileForRecord,
                line: occ.position.line,
                column: occ.position.column,
                roles: UInt64(occ.roles.rawValue),
                containerUSR: containerUSR,
                unitName: nil
            )
            occurrenceCount += 1

            try occ.relations.forEach { rel in
                writer.insertRelation(
                    occurrenceID: occID,
                    relatedUSR: rel.symbol.usr.string,
                    relatedName: rel.symbol.name.string,
                    kind: Mappings.primaryRelationKind(rel.roles),
                    roles: UInt64(rel.roles.rawValue)
                )
                relationCount += 1
                return .continue
            }
            return .continue
        }

        // Pass 3: emit symbols ONLY when this record contains the symbol's
        // definition. A given USR appears in `record.symbols` of every record
        // that mentions it (including extension records, which classify the
        // extended type with kind=`extension`). Emitting on first sight risks
        // recording the wrong kind. Emitting only at the definition site gets
        // the canonical kind every time.
        try record.symbols.forEach { sym in
            let usr = sym.usr.string
            if emittedSymbols.contains(usr) { return .continue }
            guard let defSite = defSiteByUSR[usr] else {
                // This record only references the symbol — its definition will
                // appear in another record. Skip; we'll emit there.
                return .continue
            }
            emittedSymbols.insert(usr)

            writer.insertSymbol(
                usr: usr,
                name: sym.name.string,
                kind: Mappings.kindString(sym.kind),
                subKind: Mappings.subKindString(sym.subKind),
                language: Mappings.languageString(sym.language),
                module: info.flatMap { $0.module.isEmpty ? nil : $0.module },
                file: fileForRecord.isEmpty ? nil : fileForRecord,
                line: defSite.line,
                isSystem: false,  // we already filtered system units in phase B
                properties: UInt64(sym.properties.rawValue)
            )
            symbolCount += 1
            return .continue
        }
    }
    try writer.commit()

    // Indexes deferred until after bulk inserts: building the B-trees over
    // a populated table is dramatically faster than maintaining them as we go.
    try writer.applySchema(Schema.indexStatements)

    writer.setMeta("schema_version", String(Schema.version))
    writer.setMeta("helper_version", HELPER_VERSION)
    writer.setMeta("dumped_at", ISO8601DateFormatter().string(from: Date()))
    writer.setMeta("symbols_count", String(symbolCount))
    writer.setMeta("occurrences_count", String(occurrenceCount))
    writer.setMeta("relations_count", String(relationCount))
    writer.setMeta("unit_files_count", String(unitFilesEmitted))

    writer.close()

    // Atomic rename. If destination exists from a stale run, replace it.
    if FileManager.default.fileExists(atPath: outputPath) {
        try FileManager.default.removeItem(atPath: outputPath)
    }
    try FileManager.default.moveItem(atPath: tempPath, toPath: outputPath)

    let elapsed = monotonicSeconds() - totalStart
    writeStderrJSON([
        "info": "bootstrap_complete",
        "wall_seconds": elapsed,
        "units": unitNames.count,
        "system_units_skipped": systemUnitsSkipped,
        "records": recordNames.count,
        "symbols": symbolCount,
        "occurrences": occurrenceCount,
        "relations": relationCount,
        "unit_files": unitFilesEmitted,
    ])
}

/// Returns a monotonic wall-clock time, used purely for measuring elapsed
/// durations (immune to system clock changes).
func monotonicSeconds() -> Double {
    var ts = timespec()
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts)
    return Double(ts.tv_sec) + Double(ts.tv_nsec) / 1_000_000_000
}

/// Stat a unit file with full nanosecond precision matching Python's
/// `Path.stat().st_mtime_ns`. Foundation's FileManager rounds via `Date`;
/// we go through stat(2) to keep the snapshot byte-equal across processes.
func unitFileStat(unitsDir: String, unitName: String) -> (sizeBytes: Int64, mtimeNs: Int64) {
    let path = (unitsDir as NSString).appendingPathComponent(unitName)
    var info = Darwin.stat()
    let rc = path.withCString { (cstr: UnsafePointer<CChar>) -> Int32 in
        return stat(cstr, &info)
    }
    if rc != 0 { return (0, 0) }
    let mtimeNs = Int64(info.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(info.st_mtimespec.tv_nsec)
    return (Int64(info.st_size), mtimeNs)
}
