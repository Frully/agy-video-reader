#!/usr/bin/env swift

import AppKit
import Darwin
import Foundation

private let backupFormatVersion = 1

private struct BridgeError: Error {
    let code: String
    let message: String
    let exitCode: Int32
    let backupPreserved: Bool
    let clipboardRestored: Bool?

    init(
        _ code: String,
        _ message: String,
        exitCode: Int32,
        backupPreserved: Bool = false,
        clipboardRestored: Bool? = nil
    ) {
        self.code = code
        self.message = message
        self.exitCode = exitCode
        self.backupPreserved = backupPreserved
        self.clipboardRestored = clipboardRestored
    }
}

private struct Representation: Equatable {
    let type: String
    let data: Data
}

private typealias ClipboardSnapshot = [[Representation]]

private func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
          let line = String(data: data, encoding: .utf8) else {
        FileHandle.standardOutput.write(Data("{\"ok\":false,\"code\":\"INTERNAL_ERROR\"}\n".utf8))
        return
    }
    FileHandle.standardOutput.write(Data((line + "\n").utf8))
}

private func arguments(after command: String, allowed: Set<String>) throws -> [String: String] {
    let raw = Array(CommandLine.arguments.dropFirst(2))
    guard raw.count.isMultiple(of: 2) else {
        throw BridgeError("INVALID_ARGUMENTS", "Options must be provided as --name value pairs.", exitCode: 2)
    }

    var parsed: [String: String] = [:]
    var index = 0
    while index < raw.count {
        let key = raw[index]
        guard allowed.contains(key), parsed[key] == nil else {
            throw BridgeError("INVALID_ARGUMENTS", "Unknown or duplicate option for \(command).", exitCode: 2)
        }
        parsed[key] = raw[index + 1]
        index += 2
    }
    return parsed
}

private func snapshot(_ pasteboard: NSPasteboard) throws -> ClipboardSnapshot {
    var result: ClipboardSnapshot = []
    for item in pasteboard.pasteboardItems ?? [] {
        var savedItem: [Representation] = []
        for type in item.types {
            guard let data = item.data(forType: type) else {
                throw BridgeError(
                    "CLIPBOARD_BACKUP_FAILED",
                    "An advertised clipboard representation could not be materialized; the clipboard was not changed.",
                    exitCode: 3
                )
            }
            savedItem.append(Representation(type: type.rawValue, data: data))
        }
        result.append(savedItem)
    }
    return result
}

private func serialize(_ value: ClipboardSnapshot) throws -> Data {
    let items: [[String: Any]] = value.map { item in
        ["representations": item.map { ["type": $0.type, "data": $0.data] }]
    }
    let propertyList: [String: Any] = ["format_version": backupFormatVersion, "items": items]
    do {
        return try PropertyListSerialization.data(fromPropertyList: propertyList, format: .binary, options: 0)
    } catch {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The clipboard backup could not be serialized.", exitCode: 3)
    }
}

private func deserialize(_ data: Data) throws -> ClipboardSnapshot {
    let propertyList: Any
    do {
        propertyList = try PropertyListSerialization.propertyList(from: data, options: [], format: nil)
    } catch {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup is invalid.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }

    guard let root = propertyList as? [String: Any],
          root["format_version"] as? Int == backupFormatVersion,
          let items = root["items"] as? [[String: Any]] else {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup has an unsupported format.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }

    var snapshot: ClipboardSnapshot = []
    for item in items {
        guard let rawRepresentations = item["representations"] as? [[String: Any]] else {
            throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup is incomplete.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
        }
        var representations: [Representation] = []
        for raw in rawRepresentations {
            guard let type = raw["type"] as? String,
                  !type.isEmpty,
                  let bytes = raw["data"] as? Data else {
                throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup contains an invalid representation.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
            }
            representations.append(Representation(type: type, data: bytes))
        }
        snapshot.append(representations)
    }
    return snapshot
}

private func writePrivateExclusive(_ data: Data, to path: String) throws {
    let descriptor = path.withCString { open($0, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR) }
    guard descriptor >= 0 else {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "A private clipboard backup file could not be created.", exitCode: 3)
    }

    var completed = false
    defer {
        if !completed {
            _ = path.withCString { unlink($0) }
        }
        close(descriptor)
    }

    guard fchmod(descriptor, S_IRUSR | S_IWUSR) == 0 else {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "Private permissions could not be applied to the clipboard backup.", exitCode: 3)
    }

    let writeSucceeded = data.withUnsafeBytes { rawBuffer -> Bool in
        guard let base = rawBuffer.baseAddress else { return data.isEmpty }
        var offset = 0
        while offset < rawBuffer.count {
            let count = Darwin.write(descriptor, base.advanced(by: offset), rawBuffer.count - offset)
            if count < 0 {
                if errno == EINTR { continue }
                return false
            }
            offset += count
        }
        return true
    }
    guard writeSucceeded, fsync(descriptor) == 0 else {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The private clipboard backup could not be written.", exitCode: 3)
    }
    completed = true
}

private func loadPrivateBackup(at path: String) throws -> ClipboardSnapshot {
    let descriptor = path.withCString { open($0, O_RDONLY | O_NOFOLLOW) }
    guard descriptor >= 0 else {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup is missing or unsafe.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }

    var info = stat()
    guard fstat(descriptor, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG,
          (info.st_mode & (S_IRWXG | S_IRWXO)) == 0 else {
        close(descriptor)
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup is missing, unsafe, or not a regular private file.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }

    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    do {
        let data = try handle.readToEnd() ?? Data()
        try handle.close()
        return try deserialize(data)
    } catch let error as BridgeError {
        throw error
    } catch {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard recovery backup could not be read.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }
}

private func writeSnapshot(_ value: ClipboardSnapshot, to pasteboard: NSPasteboard, expectedChangeCount: Int) throws {
    var objects: [NSPasteboardItem] = []
    for savedItem in value {
        let item = NSPasteboardItem()
        for representation in savedItem {
            guard item.setData(representation.data, forType: NSPasteboard.PasteboardType(representation.type)) else {
                throw BridgeError("CLIPBOARD_RESTORE_FAILED", "A clipboard representation could not be prepared for restoration.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
            }
        }
        objects.append(item)
    }

    guard pasteboard.changeCount == expectedChangeCount else {
        throw BridgeError(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed immediately before restoration. Newer content was left untouched.",
            exitCode: 4,
            backupPreserved: true,
            clipboardRestored: false
        )
    }
    pasteboard.clearContents()
    if !objects.isEmpty && !pasteboard.writeObjects(objects) {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The original clipboard items could not be restored.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }
}

private func representationsByType(_ item: [Representation]) -> [String: Data]? {
    var result: [String: Data] = [:]
    for representation in item {
        guard result.updateValue(representation.data, forKey: representation.type) == nil else {
            return nil
        }
    }
    return result
}

private func snapshotsEquivalent(_ lhs: ClipboardSnapshot, _ rhs: ClipboardSnapshot) -> Bool {
    guard lhs.count == rhs.count else { return false }
    for index in lhs.indices {
        guard let left = representationsByType(lhs[index]),
              let right = representationsByType(rhs[index]),
              left == right else {
            return false
        }
    }
    return true
}

private func verifiedRestore(_ value: ClipboardSnapshot, to pasteboard: NSPasteboard, expectedChangeCount: Int) throws -> Int {
    try writeSnapshot(value, to: pasteboard, expectedChangeCount: expectedChangeCount)
    let restored: ClipboardSnapshot
    do {
        restored = try snapshot(pasteboard)
    } catch {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The restored clipboard could not be verified.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }
    guard snapshotsEquivalent(restored, value) else {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "Clipboard verification did not match the saved item boundaries, type sets, and bytes.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
    }
    return pasteboard.changeCount
}

private func removeBackup(at path: String) throws {
    guard path.withCString({ unlink($0) }) == 0 else {
        throw BridgeError("CLIPBOARD_RESTORE_FAILED", "The clipboard was restored, but its recovery backup could not be deleted.", exitCode: 5, backupPreserved: true, clipboardRestored: true)
    }
}

private func require(_ options: [String: String], _ name: String) throws -> String {
    guard let value = options[name], !value.isEmpty else {
        throw BridgeError("INVALID_ARGUMENTS", "Missing required option \(name).", exitCode: 2)
    }
    return value
}

private func stage() throws {
    let options = try arguments(after: "stage", allowed: ["--file", "--backup"])
    let videoPath = try require(options, "--file")
    let backupPath = try require(options, "--backup")
    guard videoPath.hasPrefix("/"), backupPath.hasPrefix("/") else {
        throw BridgeError("INVALID_ARGUMENTS", "Video and backup paths must be absolute.", exitCode: 2)
    }

    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: videoPath, isDirectory: &isDirectory), !isDirectory.boolValue else {
        throw BridgeError("INVALID_ARGUMENTS", "The staged file must be an existing regular file.", exitCode: 2)
    }
    var fileInfo = stat()
    guard lstat(videoPath, &fileInfo) == 0, (fileInfo.st_mode & S_IFMT) == S_IFREG else {
        throw BridgeError("INVALID_ARGUMENTS", "The staged file must not be a link, directory, pipe, or device.", exitCode: 2)
    }

    let pasteboard = NSPasteboard.general
    let originalChangeCount = pasteboard.changeCount
    let original = try snapshot(pasteboard)
    guard pasteboard.changeCount == originalChangeCount else {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The clipboard changed while it was being read; it was not modified.", exitCode: 3)
    }
    let backupData = try serialize(original)
    try writePrivateExclusive(backupData, to: backupPath)

    guard pasteboard.changeCount == originalChangeCount else {
        _ = backupPath.withCString { unlink($0) }
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The clipboard changed while its backup was being secured; newer content was not modified.", exitCode: 3)
    }

    let item = NSPasteboardItem()
    let fileURL = URL(fileURLWithPath: videoPath, isDirectory: false).standardizedFileURL
    guard item.setString(fileURL.absoluteString, forType: .fileURL) else {
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The video file URL could not be prepared for the clipboard.", exitCode: 3, backupPreserved: true, clipboardRestored: true)
    }

    guard pasteboard.changeCount == originalChangeCount else {
        _ = backupPath.withCString { unlink($0) }
        throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The clipboard changed immediately before video staging; newer content was not modified.", exitCode: 3)
    }
    pasteboard.clearContents()
    guard pasteboard.writeObjects([item]),
          let stagedItems = pasteboard.pasteboardItems,
          stagedItems.count == 1,
          stagedItems[0].types.contains(.fileURL) else {
        do {
            _ = try verifiedRestore(original, to: pasteboard, expectedChangeCount: pasteboard.changeCount)
            try removeBackup(at: backupPath)
            throw BridgeError("CLIPBOARD_BACKUP_FAILED", "The video file URL could not be staged; the original clipboard was restored.", exitCode: 3, clipboardRestored: true)
        } catch let bridgeError as BridgeError where bridgeError.code == "CLIPBOARD_BACKUP_FAILED" {
            throw bridgeError
        } catch {
            throw BridgeError("CLIPBOARD_RESTORE_FAILED", "Video staging failed and the original clipboard could not be restored; retain the recovery backup.", exitCode: 5, backupPreserved: true, clipboardRestored: false)
        }
    }

    emit([
        "ok": true,
        "operation": "stage",
        "staged_change_count": pasteboard.changeCount,
        "backup_item_count": original.count,
        "backup_type_counts": original.map { $0.count },
        "staged_types": stagedItems[0].types.map(\.rawValue),
    ])
}

private func restore() throws {
    let options = try arguments(after: "restore", allowed: ["--backup", "--expected-change-count"])
    let backupPath = try require(options, "--backup")
    let expectedString = try require(options, "--expected-change-count")
    guard backupPath.hasPrefix("/"), let expected = Int(expectedString), expected >= 0 else {
        throw BridgeError("INVALID_ARGUMENTS", "The backup path must be absolute and the expected change count must be a non-negative integer.", exitCode: 2)
    }

    let original = try loadPrivateBackup(at: backupPath)
    let pasteboard = NSPasteboard.general
    guard pasteboard.changeCount == expected else {
        throw BridgeError(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed after video staging. Newer clipboard content was left untouched; use the retained private backup for manual recovery.",
            exitCode: 4,
            backupPreserved: true,
            clipboardRestored: false
        )
    }

    let restoredChangeCount = try verifiedRestore(original, to: pasteboard, expectedChangeCount: expected)
    try removeBackup(at: backupPath)
    emit([
        "ok": true,
        "operation": "restore",
        "restored_change_count": restoredChangeCount,
        "restored_item_count": original.count,
        "restored_type_counts": original.map { $0.count },
        "backup_deleted": true,
    ])
}

private func status() throws {
    _ = try arguments(after: "status", allowed: [])
    emit(["ok": true, "operation": "status", "change_count": NSPasteboard.general.changeCount])
}

private func isStagedVideo(_ snapshot: ClipboardSnapshot, path: String) -> Bool {
    guard snapshot.count == 1, snapshot[0].count == 1,
          snapshot[0][0].type == NSPasteboard.PasteboardType.fileURL.rawValue,
          let value = String(data: snapshot[0][0].data, encoding: .utf8),
          let url = URL(string: value), url.isFileURL else {
        return false
    }
    return url.standardizedFileURL.path == URL(fileURLWithPath: path).standardizedFileURL.path
}

private func recover() throws {
    let options = try arguments(after: "recover", allowed: ["--backup", "--file"])
    let backupPath = try require(options, "--backup")
    let videoPath = try require(options, "--file")
    guard backupPath.hasPrefix("/"), videoPath.hasPrefix("/") else {
        throw BridgeError("INVALID_ARGUMENTS", "Recovery paths must be absolute.", exitCode: 2)
    }
    let original = try loadPrivateBackup(at: backupPath)
    let pasteboard = NSPasteboard.general
    let expected = pasteboard.changeCount
    let current = try snapshot(pasteboard)
    guard pasteboard.changeCount == expected else {
        throw BridgeError(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard changed while recovery identity was being checked. Newer content was left untouched.",
            exitCode: 4,
            backupPreserved: true,
            clipboardRestored: false
        )
    }
    if snapshotsEquivalent(current, original) {
        try removeBackup(at: backupPath)
        emit([
            "ok": true,
            "operation": "recover",
            "already_restored": true,
            "restored_change_count": pasteboard.changeCount,
            "backup_deleted": true,
        ])
        return
    }
    guard isStagedVideo(current, path: videoPath) else {
        throw BridgeError(
            "CLIPBOARD_CHANGED_EXTERNALLY",
            "The clipboard no longer contains the staged video. Newer clipboard content was left untouched.",
            exitCode: 4,
            backupPreserved: true,
            clipboardRestored: false
        )
    }
    let restoredChangeCount = try verifiedRestore(original, to: pasteboard, expectedChangeCount: expected)
    try removeBackup(at: backupPath)
    emit([
        "ok": true,
        "operation": "recover",
        "already_restored": false,
        "restored_change_count": restoredChangeCount,
        "backup_deleted": true,
    ])
}

do {
    guard CommandLine.arguments.count >= 2 else {
        throw BridgeError("INVALID_ARGUMENTS", "Use stage, restore, recover, or status.", exitCode: 2)
    }
    switch CommandLine.arguments[1] {
    case "stage": try stage()
    case "restore": try restore()
    case "recover": try recover()
    case "status": try status()
    default: throw BridgeError("INVALID_ARGUMENTS", "Use stage, restore, recover, or status.", exitCode: 2)
    }
} catch let error as BridgeError {
    var response: [String: Any] = [
        "ok": false,
        "code": error.code,
        "message": error.message,
        "backup_preserved": error.backupPreserved,
    ]
    if let restored = error.clipboardRestored {
        response["clipboard_restored"] = restored
    }
    emit(response)
    exit(error.exitCode)
} catch {
    emit([
        "ok": false,
        "code": "INTERNAL_ERROR",
        "message": "The clipboard bridge failed unexpectedly.",
        "backup_preserved": true,
    ])
    exit(70)
}
