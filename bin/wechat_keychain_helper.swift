import CryptoKit
import Darwin
import Foundation
import Security

private func archivePath(_ legacy: String) -> String {
    let base = ProcessInfo.processInfo.environment["SYNC_CENTER_HOME"] ??
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/business-memory-sync-center").path
    let old = "/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync"
    if legacy == old || legacy.hasPrefix(old + "/") { return base + legacy.dropFirst(old.count) }
    let prefix = "/Users/example/"
    if legacy.hasPrefix(prefix) { return base + "/isolated-home/" + legacy.dropFirst(prefix.count) }
    return legacy
}

guard ProcessInfo.processInfo.environment["SYNC_CENTER_ENABLE_ARCHIVED_CODE"] == "1" else { exit(78) }

private let service = "com.example.business.wechat-source-key"
private let protocolMagic = Data("LWK1".utf8)

enum HelperError: Error, CustomStringConvertible {
    case invalidArguments
    case unsafeTerminal
    case invalidProtocol
    case invalidRecord
    case keychain(OSStatus)
    case io

    var description: String {
        switch self {
        case .invalidArguments: return "invalid_arguments"
        case .unsafeTerminal: return "secret_output_to_terminal_refused"
        case .invalidProtocol: return "invalid_enrollment_protocol"
        case .invalidRecord: return "invalid_enrollment_record"
        case .keychain(let status): return "keychain_error_\(status)"
        case .io: return "io_error"
        }
    }
}

func readExact(_ handle: FileHandle, count: Int) throws -> Data {
    var result = Data()
    while result.count < count {
        guard let chunk = try handle.read(upToCount: count - result.count), !chunk.isEmpty else {
            throw HelperError.io
        }
        result.append(chunk)
    }
    return result
}

func uint16(_ data: Data) throws -> Int {
    guard data.count == 2 else { throw HelperError.invalidProtocol }
    return Int(data[data.startIndex]) << 8 | Int(data[data.startIndex + 1])
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

func makeKeyID(account: String, path: String, kind: String, salt: Data) -> String {
    var material = Data(path.utf8)
    material.append(0)
    material.append(Data(kind.utf8))
    material.append(0)
    material.append(salt)
    return account + ":" + sha256Hex(material)
}

func storeSecret(keyID: String, kind: String, secret: Data) throws {
    let match: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: keyID,
        kSecAttrSynchronizable as String: kCFBooleanFalse as Any,
    ]
    let update: [String: Any] = [
        kSecValueData as String: secret,
        kSecAttrLabel as String: "BUSINESS WeChat \(kind) key",
        kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
    ]
    let updateStatus = SecItemUpdate(match as CFDictionary, update as CFDictionary)
    if updateStatus == errSecSuccess { return }
    guard updateStatus == errSecItemNotFound else { throw HelperError.keychain(updateStatus) }
    var add = match
    for (key, value) in update { add[key] = value }
    let addStatus = SecItemAdd(add as CFDictionary, nil)
    guard addStatus == errSecSuccess else { throw HelperError.keychain(addStatus) }
}

func fetchSecret(keyID: String) throws -> Data {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: keyID,
        kSecAttrSynchronizable as String: kCFBooleanFalse as Any,
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne,
    ]
    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    guard status == errSecSuccess, let data = item as? Data else {
        throw HelperError.keychain(status)
    }
    return data
}

func deleteSecret(keyID: String) throws {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: keyID,
        kSecAttrSynchronizable as String: kCFBooleanFalse as Any,
    ]
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
        throw HelperError.keychain(status)
    }
}

func enroll() throws {
    guard isatty(STDIN_FILENO) == 0 else { throw HelperError.unsafeTerminal }
    let input = FileHandle.standardInput
    guard try readExact(input, count: 4) == protocolMagic else { throw HelperError.invalidProtocol }
    let accountData = try readExact(input, count: 32)
    let account = accountData.map { String(format: "%02x", $0) }.joined()
    var records: [[String: Any]] = []

    while true {
        let pathLength = try uint16(readExact(input, count: 2))
        let secretLength = try uint16(readExact(input, count: 2))
        let saltLength = try uint16(readExact(input, count: 2))
        let kindLength = try uint16(readExact(input, count: 2))
        if pathLength == 0 {
            guard secretLength == 0, saltLength == 0, kindLength == 0 else {
                throw HelperError.invalidProtocol
            }
            break
        }
        guard pathLength <= 1024, (1...64).contains(secretLength), saltLength <= 32,
              (1...32).contains(kindLength), records.count < 128 else {
            throw HelperError.invalidRecord
        }
        let pathData = try readExact(input, count: pathLength)
        let kindData = try readExact(input, count: kindLength)
        let salt = try readExact(input, count: saltLength)
        var secret = try readExact(input, count: secretLength)
        guard let path = String(data: pathData, encoding: .utf8),
              let kind = String(data: kindData, encoding: .utf8),
              !path.hasPrefix("/"), !path.contains(".."),
              ["database", "image_aes"].contains(kind) else {
            secret.resetBytes(in: 0..<secret.count)
            throw HelperError.invalidRecord
        }
        if kind == "database" && (secret.count != 32 || salt.count != 16) {
            secret.resetBytes(in: 0..<secret.count)
            throw HelperError.invalidRecord
        }
        if kind == "image_aes" && secret.count != 16 {
            secret.resetBytes(in: 0..<secret.count)
            throw HelperError.invalidRecord
        }
        let keyID = makeKeyID(account: account, path: path, kind: kind, salt: salt)
        try storeSecret(keyID: keyID, kind: kind, secret: secret)
        secret.resetBytes(in: 0..<secret.count)
        records.append([
            "relative_path": path,
            "record_kind": kind,
            "secret_length": secretLength,
            "salt_hex": salt.map { String(format: "%02x", $0) }.joined(),
            "key_id": keyID,
        ])
    }
    guard !records.isEmpty else { throw HelperError.invalidProtocol }
    let output: [String: Any] = [
        "schema_version": 1,
        "account_fingerprint": account,
        "record_count": records.count,
        "records": records,
    ]
    let json = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write(Data([0x0a]))
}

func fetch(arguments: [String]) throws {
    guard isatty(STDOUT_FILENO) == 0 else { throw HelperError.unsafeTerminal }
    guard let index = arguments.firstIndex(of: "--key-id"), index + 1 < arguments.count else {
        throw HelperError.invalidArguments
    }
    let keyID = arguments[index + 1]
    guard keyID.range(of: "^[0-9a-f]{64}:[0-9a-f]{64}$", options: .regularExpression) != nil else {
        throw HelperError.invalidArguments
    }
    var secret = try fetchSecret(keyID: keyID)
    FileHandle.standardOutput.write(secret)
    secret.resetBytes(in: 0..<secret.count)
}

func selfTest() throws {
    let account = String(repeating: "a", count: 64)
    let salt = Data(repeating: 0x11, count: 16)
    let keyID = makeKeyID(account: account, path: "contact/contact.db", kind: "database", salt: salt)
    guard keyID.count == 129, keyID.hasPrefix(account + ":") else { throw HelperError.invalidProtocol }
    let transientID = "self-test:" + UUID().uuidString
    var transientSecret = Data(repeating: 0x5a, count: 32)
    defer {
        try? deleteSecret(keyID: transientID)
        transientSecret.resetBytes(in: 0..<transientSecret.count)
    }
    try storeSecret(keyID: transientID, kind: "database", secret: transientSecret)
    var fetched = try fetchSecret(keyID: transientID)
    defer { fetched.resetBytes(in: 0..<fetched.count) }
    guard fetched == transientSecret else { throw HelperError.invalidProtocol }
    try deleteSecret(keyID: transientID)
    let result: [String: Any] = ["verdict": "pass", "checks": 5]
    let json = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(json)
    FileHandle.standardOutput.write(Data([0x0a]))
}

do {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard let mode = arguments.first else { throw HelperError.invalidArguments }
    switch mode {
    case "enroll": try enroll()
    case "fetch": try fetch(arguments: arguments)
    case "self-test": try selfTest()
    default: throw HelperError.invalidArguments
    }
} catch {
    let message = (error as? HelperError)?.description ?? "unexpected_error"
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}
