// Thin wrapper around libsqlite3 used by the helper to write the xcindex
// cache directly. Holds prepared statements for the hot insert paths so we
// pay the parse cost once per process.
//
// All bind helpers use SQLITE_TRANSIENT — SQLite copies the bytes during
// `sqlite3_step`, so the Swift String can be released immediately after.

import Foundation
import SQLite3

let SQLITE_TRANSIENT = unsafeBitCast(
    OpaquePointer(bitPattern: -1)!,
    to: sqlite3_destructor_type.self
)

enum SQLiteWriterError: Error, CustomStringConvertible {
    case openFailed(path: String, message: String)
    case execFailed(sql: String, message: String)
    case prepareFailed(sql: String, message: String)

    var description: String {
        switch self {
        case .openFailed(let path, let msg):
            return "SQLite open failed at \(path): \(msg)"
        case .execFailed(let sql, let msg):
            return "SQLite exec failed (\(sql.prefix(80))): \(msg)"
        case .prepareFailed(let sql, let msg):
            return "SQLite prepare failed (\(sql.prefix(80))): \(msg)"
        }
    }
}

final class SQLiteWriter {
    private var db: OpaquePointer?

    // Prepared statements held for the duration of a write session.
    private var symbolStmt: OpaquePointer?
    private var occurrenceStmt: OpaquePointer?
    private var relationStmt: OpaquePointer?
    private var unitFilesStmt: OpaquePointer?
    private var unitsStmt: OpaquePointer?
    private var metaStmt: OpaquePointer?

    init(path: String) throws {
        var handle: OpaquePointer?
        let rc = sqlite3_open(path, &handle)
        if rc != SQLITE_OK {
            let msg = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "rc=\(rc)"
            sqlite3_close(handle)
            throw SQLiteWriterError.openFailed(path: path, message: msg)
        }
        self.db = handle
    }

    deinit {
        sqlite3_finalize(symbolStmt)
        sqlite3_finalize(occurrenceStmt)
        sqlite3_finalize(relationStmt)
        sqlite3_finalize(unitFilesStmt)
        sqlite3_finalize(unitsStmt)
        sqlite3_finalize(metaStmt)
        sqlite3_close(db)
    }

    func close() {
        sqlite3_finalize(symbolStmt); symbolStmt = nil
        sqlite3_finalize(occurrenceStmt); occurrenceStmt = nil
        sqlite3_finalize(relationStmt); relationStmt = nil
        sqlite3_finalize(unitFilesStmt); unitFilesStmt = nil
        sqlite3_finalize(unitsStmt); unitsStmt = nil
        sqlite3_finalize(metaStmt); metaStmt = nil
        sqlite3_close(db)
        db = nil
    }

    // MARK: - Setup

    func applyPragmas(_ pragmas: [String]) throws {
        for p in pragmas { try exec(p) }
    }

    func applySchema(_ statements: [String]) throws {
        for s in statements { try exec(s) }
    }

    func exec(_ sql: String) throws {
        var err: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(db, sql, nil, nil, &err)
        if rc != SQLITE_OK {
            let msg = err.map { String(cString: $0) } ?? "rc=\(rc)"
            sqlite3_free(err)
            throw SQLiteWriterError.execFailed(sql: sql, message: msg)
        }
    }

    func beginTransaction() throws { try exec("BEGIN IMMEDIATE") }
    func commit() throws { try exec("COMMIT") }
    func rollback() throws { try exec("ROLLBACK") }

    // MARK: - Prepared inserts (called once before the hot loop)

    func prepareInsertStatements() throws {
        symbolStmt = try prepare("""
            INSERT OR REPLACE INTO symbols(
                usr, name, kind, sub_kind, language, module, file, line, is_system, properties
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """)
        occurrenceStmt = try prepare("""
            INSERT INTO occurrences(
                symbol_usr, file, line, column, roles, container_usr, unit_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """)
        relationStmt = try prepare("""
            INSERT INTO relations(occurrence_id, related_usr, related_name, kind, roles)
            VALUES (?, ?, ?, ?, ?)
        """)
        unitFilesStmt = try prepare("""
            INSERT OR IGNORE INTO unit_files(unit_name, file) VALUES (?, ?)
        """)
        unitsStmt = try prepare("""
            INSERT OR REPLACE INTO units(
                name, main_file, module, target, provider, mtime_ns, size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """)
        metaStmt = try prepare("""
            INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)
        """)
    }

    private func prepare(_ sql: String) throws -> OpaquePointer {
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
        if rc != SQLITE_OK {
            let msg = String(cString: sqlite3_errmsg(db))
            throw SQLiteWriterError.prepareFailed(sql: sql, message: msg)
        }
        return stmt!
    }

    // MARK: - Hot path: row insertion

    func insertSymbol(
        usr: String,
        name: String,
        kind: String,
        subKind: String?,
        language: String,
        module: String?,
        file: String?,
        line: Int?,
        isSystem: Bool,
        properties: UInt64
    ) {
        guard let stmt = symbolStmt else { return }
        bindText(stmt, 1, usr)
        bindText(stmt, 2, name)
        bindText(stmt, 3, kind)
        bindOptionalText(stmt, 4, subKind)
        bindText(stmt, 5, language)
        bindOptionalText(stmt, 6, module)
        bindOptionalText(stmt, 7, file)
        if let l = line {
            sqlite3_bind_int64(stmt, 8, sqlite3_int64(l))
        } else {
            sqlite3_bind_null(stmt, 8)
        }
        sqlite3_bind_int(stmt, 9, isSystem ? 1 : 0)
        bindUInt64Signed(stmt, 10, properties)
        sqlite3_step(stmt)
        sqlite3_reset(stmt)
    }

    /// Returns the rowid of the inserted occurrence, used to link relations.
    func insertOccurrence(
        symbolUSR: String,
        file: String,
        line: Int,
        column: Int,
        roles: UInt64,
        containerUSR: String?,
        unitName: String?
    ) -> sqlite3_int64 {
        guard let stmt = occurrenceStmt else { return 0 }
        bindText(stmt, 1, symbolUSR)
        bindText(stmt, 2, file)
        sqlite3_bind_int64(stmt, 3, sqlite3_int64(line))
        sqlite3_bind_int64(stmt, 4, sqlite3_int64(column))
        bindUInt64Signed(stmt, 5, roles)
        bindOptionalText(stmt, 6, containerUSR)
        bindOptionalText(stmt, 7, unitName)
        sqlite3_step(stmt)
        let rowid = sqlite3_last_insert_rowid(db)
        sqlite3_reset(stmt)
        return rowid
    }

    func insertRelation(
        occurrenceID: sqlite3_int64,
        relatedUSR: String,
        relatedName: String?,
        kind: String,
        roles: UInt64
    ) {
        guard let stmt = relationStmt else { return }
        sqlite3_bind_int64(stmt, 1, occurrenceID)
        bindText(stmt, 2, relatedUSR)
        bindOptionalText(stmt, 3, relatedName)
        bindText(stmt, 4, kind)
        bindUInt64Signed(stmt, 5, roles)
        sqlite3_step(stmt)
        sqlite3_reset(stmt)
    }

    func insertUnitFile(unitName: String, file: String) {
        guard let stmt = unitFilesStmt else { return }
        bindText(stmt, 1, unitName)
        bindText(stmt, 2, file)
        sqlite3_step(stmt)
        sqlite3_reset(stmt)
    }

    func insertUnit(
        name: String,
        mainFile: String?,
        module: String?,
        target: String?,
        provider: String?,
        mtimeNs: Int64,
        sizeBytes: Int64
    ) {
        guard let stmt = unitsStmt else { return }
        bindText(stmt, 1, name)
        bindOptionalText(stmt, 2, mainFile)
        bindOptionalText(stmt, 3, module)
        bindOptionalText(stmt, 4, target)
        bindOptionalText(stmt, 5, provider)
        sqlite3_bind_int64(stmt, 6, sqlite3_int64(mtimeNs))
        sqlite3_bind_int64(stmt, 7, sqlite3_int64(sizeBytes))
        sqlite3_step(stmt)
        sqlite3_reset(stmt)
    }

    func setMeta(_ key: String, _ value: String) {
        guard let stmt = metaStmt else { return }
        bindText(stmt, 1, key)
        bindText(stmt, 2, value)
        sqlite3_step(stmt)
        sqlite3_reset(stmt)
    }

    // MARK: - Bind helpers

    @inline(__always)
    private func bindText(_ stmt: OpaquePointer, _ col: Int32, _ value: String) {
        _ = value.withCString { cstr in
            sqlite3_bind_text(stmt, col, cstr, -1, SQLITE_TRANSIENT)
        }
    }

    @inline(__always)
    private func bindOptionalText(_ stmt: OpaquePointer, _ col: Int32, _ value: String?) {
        if let v = value {
            _ = v.withCString { sqlite3_bind_text(stmt, col, $0, -1, SQLITE_TRANSIENT) }
        } else {
            sqlite3_bind_null(stmt, col)
        }
    }

    @inline(__always)
    private func bindUInt64Signed(_ stmt: OpaquePointer, _ col: Int32, _ value: UInt64) {
        // SQLite INTEGER is signed 64-bit. Use the two's-complement bit pattern
        // so high-bit role flags (e.g., SymbolRole.canonical) survive the round
        // trip — Python decodes the same way (`query.decode_roles`).
        sqlite3_bind_int64(stmt, col, sqlite3_int64(bitPattern: value))
    }

    // MARK: - Incremental support

    /// Resolve a set of unit names to their associated file paths in one query.
    func filesForUnits(_ unitNames: [String]) throws -> Set<String> {
        if unitNames.isEmpty { return [] }
        let placeholders = Array(repeating: "?", count: unitNames.count).joined(separator: ",")
        let sql = "SELECT DISTINCT file FROM unit_files WHERE unit_name IN (\(placeholders))"
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
        if rc != SQLITE_OK {
            let msg = String(cString: sqlite3_errmsg(db))
            throw SQLiteWriterError.prepareFailed(sql: sql, message: msg)
        }
        defer { sqlite3_finalize(stmt) }
        for (i, name) in unitNames.enumerated() {
            _ = name.withCString {
                sqlite3_bind_text(stmt, Int32(i + 1), $0, -1, SQLITE_TRANSIENT)
            }
        }
        var files = Set<String>()
        while sqlite3_step(stmt) == SQLITE_ROW {
            if let cstr = sqlite3_column_text(stmt, 0) {
                files.insert(String(cString: cstr))
            }
        }
        return files
    }

    /// DELETE all rows for the given files, in the right order to avoid
    /// orphaning relations. Caller should be inside a transaction.
    func deleteRowsForFiles(_ files: Set<String>) throws {
        if files.isEmpty { return }
        let placeholders = Array(repeating: "?", count: files.count).joined(separator: ",")
        let filesList = Array(files)

        // relations → occurrences → symbols ordering matches FK semantics, even
        // though we don't enforce them with SQLite FK constraints.
        let stmts = [
            "DELETE FROM relations WHERE occurrence_id IN (SELECT id FROM occurrences WHERE file IN (\(placeholders)))",
            "DELETE FROM occurrences WHERE file IN (\(placeholders))",
            "DELETE FROM symbols WHERE file IN (\(placeholders))",
        ]
        for sql in stmts {
            var stmt: OpaquePointer?
            let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
            if rc != SQLITE_OK {
                throw SQLiteWriterError.prepareFailed(
                    sql: sql, message: String(cString: sqlite3_errmsg(db))
                )
            }
            for (i, file) in filesList.enumerated() {
                _ = file.withCString {
                    sqlite3_bind_text(stmt, Int32(i + 1), $0, -1, SQLITE_TRANSIENT)
                }
            }
            sqlite3_step(stmt)
            sqlite3_finalize(stmt)
        }
    }

    func deleteUnits(_ unitNames: [String]) throws {
        if unitNames.isEmpty { return }
        let placeholders = Array(repeating: "?", count: unitNames.count).joined(separator: ",")

        for sql in [
            "DELETE FROM unit_files WHERE unit_name IN (\(placeholders))",
            "DELETE FROM units WHERE name IN (\(placeholders))",
        ] {
            var stmt: OpaquePointer?
            let rc = sqlite3_prepare_v2(db, sql, -1, &stmt, nil)
            if rc != SQLITE_OK {
                throw SQLiteWriterError.prepareFailed(
                    sql: sql, message: String(cString: sqlite3_errmsg(db))
                )
            }
            for (i, name) in unitNames.enumerated() {
                _ = name.withCString {
                    sqlite3_bind_text(stmt, Int32(i + 1), $0, -1, SQLITE_TRANSIENT)
                }
            }
            sqlite3_step(stmt)
            sqlite3_finalize(stmt)
        }
    }

    /// Read meta.schema_version. Returns nil if the meta table is missing
    /// or the row is absent — caller treats that as "incompatible cache".
    func readSchemaVersion() -> Int? {
        var stmt: OpaquePointer?
        let rc = sqlite3_prepare_v2(
            db, "SELECT value FROM meta WHERE key = 'schema_version'", -1, &stmt, nil
        )
        if rc != SQLITE_OK { return nil }
        defer { sqlite3_finalize(stmt) }
        if sqlite3_step(stmt) == SQLITE_ROW, let cstr = sqlite3_column_text(stmt, 0) {
            return Int(String(cString: cstr))
        }
        return nil
    }
}
