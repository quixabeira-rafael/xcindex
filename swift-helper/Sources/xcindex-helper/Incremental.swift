// `incremental` subcommand: opens an existing SQLite cache, deletes rows for
// files belonging to modified-or-removed units, then re-walks the modified
// units and re-INSERTs the new rows. Single transaction.

import Foundation
import IndexStore

func runIncremental(_ args: [String]) async {
    var sqlitePath: String? = nil
    var indexStorePath: String? = nil
    var modifiedUnits: [String] = []
    var removedUnits: [String] = []
    var includeSystem = false

    var i = 0
    while i < args.count {
        switch args[i] {
        case "--sqlite":
            i += 1
            guard i < args.count else { exitArgError("--sqlite requires a value") }
            sqlitePath = args[i]
        case "--index-store":
            i += 1
            guard i < args.count else { exitArgError("--index-store requires a value") }
            indexStorePath = args[i]
        case "--modified-unit":
            i += 1
            guard i < args.count else { exitArgError("--modified-unit requires a value") }
            modifiedUnits.append(args[i])
        case "--removed-unit":
            i += 1
            guard i < args.count else { exitArgError("--removed-unit requires a value") }
            removedUnits.append(args[i])
        case "--include-system":
            includeSystem = true
        default:
            exitArgError("unknown argument: \(args[i])")
        }
        i += 1
    }

    guard let sqlitePath = sqlitePath else {
        exitArgError("--sqlite PATH is required")
    }
    guard let indexStorePath = indexStorePath else {
        exitArgError("--index-store PATH is required")
    }

    do {
        try await performIncremental(
            sqlitePath: sqlitePath,
            indexStorePath: indexStorePath,
            modifiedUnits: modifiedUnits,
            removedUnits: removedUnits,
            includeSystem: includeSystem
        )
    } catch {
        writeStderrJSON([
            "error": "incremental_failed",
            "detail": "\(error)",
        ])
        exit(2)
    }
}

private func performIncremental(
    sqlitePath: String,
    indexStorePath: String,
    modifiedUnits: [String],
    removedUnits: [String],
    includeSystem: Bool
) async throws {
    let totalStart = monotonicSeconds()

    let writer = try SQLiteWriter(path: sqlitePath)

    // Reject if the existing cache was written under a different schema.
    // Python catches exit code 4 and triggers a full re-bootstrap.
    let cachedVersion = writer.readSchemaVersion()
    if cachedVersion != Schema.version {
        writer.close()
        writeStderrJSON([
            "error": "schema_mismatch",
            "detail": "cache schema version is \(cachedVersion ?? -1), helper expects \(Schema.version)",
        ])
        exit(4)
    }

    try writer.applyPragmas(Schema.writePragmas)
    try writer.prepareInsertStatements()

    let affectedUnits = Array(Set(modifiedUnits).union(removedUnits))
    let filesToRedump = try writer.filesForUnits(affectedUnits)

    // Resolve libIndexStore once. We open the store even when there's no
    // re-walk to do — avoids a separate "removed-only" path.
    let libPathStr = try resolveIndexStoreLibrary()
    let libURL = URL(fileURLWithPath: libPathStr)
    let library = try await IndexStoreLibrary.at(dylibPath: libURL)
    let store = try library.indexStore(at: URL(fileURLWithPath: indexStorePath))

    let unitsDirPath = (indexStorePath as NSString).appendingPathComponent("v5/units")

    // Phase 1: in a single transaction, drop the rows we're about to replace.
    try writer.beginTransaction()
    try writer.deleteRowsForFiles(filesToRedump)
    try writer.deleteUnits(affectedUnits)
    try writer.commit()

    // Phase 2: re-walk modified units and re-INSERT. (Removed units are gone
    // from the IndexStore on disk; nothing to walk for them.)
    var recordNames: [String] = []
    var recordSeen = Set<String>()
    var recordToFile: [String: String] = [:]
    var recordToUnitInfo: [String: (module: String, file: String)] = [:]
    var unitFilesEmitted = 0

    try writer.beginTransaction()
    for unitName in modifiedUnits {
        guard let unit = try? store.unit(named: unitName) else { continue }
        if !includeSystem && unit.isSystemUnit { continue }

        let mainFile = unit.hasMainFile ? unit.mainFile.string : ""
        let module = unit.moduleName.string
        let target = unit.target.string
        let provider = unit.providerIdentifier.string

        let (sizeBytes, mtimeNs) = unitFileStat(unitsDir: unitsDirPath, unitName: unitName)

        writer.insertUnit(
            name: unitName,
            mainFile: mainFile.isEmpty ? nil : mainFile,
            module: module.isEmpty ? nil : module,
            target: target.isEmpty ? nil : target,
            provider: provider.isEmpty ? nil : provider,
            mtimeNs: mtimeNs,
            sizeBytes: sizeBytes
        )

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

    var symbolCount = 0
    var occurrenceCount = 0
    var relationCount = 0
    var emittedSymbols = Set<String>()

    for recordName in recordNames {
        guard let record = try? store.record(named: recordName) else { continue }
        let fileForRecord = recordToFile[recordName] ?? ""
        let info = recordToUnitInfo[recordName]

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

        try record.symbols.forEach { sym in
            let usr = sym.usr.string
            if emittedSymbols.contains(usr) { return .continue }
            guard let defSite = defSiteByUSR[usr] else { return .continue }
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
                isSystem: false,
                properties: UInt64(sym.properties.rawValue)
            )
            symbolCount += 1
            return .continue
        }
    }
    try writer.commit()

    // Update meta with last-incremental diagnostics.
    writer.setMeta("last_incremental_at", ISO8601DateFormatter().string(from: Date()))
    writer.setMeta("last_incremental_modified", String(modifiedUnits.count))
    writer.setMeta("last_incremental_removed", String(removedUnits.count))

    writer.close()

    let elapsed = monotonicSeconds() - totalStart
    writeStderrJSON([
        "info": "incremental_complete",
        "wall_seconds": elapsed,
        "modified_units": modifiedUnits.count,
        "removed_units": removedUnits.count,
        "files_redumped": filesToRedump.count,
        "records_walked": recordNames.count,
        "symbols": symbolCount,
        "occurrences": occurrenceCount,
        "relations": relationCount,
        "unit_files": unitFilesEmitted,
    ])
}
