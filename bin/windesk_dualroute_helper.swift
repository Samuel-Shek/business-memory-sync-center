import AppKit
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

// Security contract (kept in one compiled binary so the Keychain ACL sees one
// stable application identity for both enrollment and mounting):
//   service: com.business.memory-bridge.windesk-smb
//   account: Example User
//   keychain: /Users/example/Library/Keychains/login.keychain-db
//
// The SMB password is never converted to an argv or environment value. In
// mount mode it is fetched only after mount_smbfs is waiting on a private PTY,
// written once to that PTY, and never copied to stdout/stderr or a file.

private enum Config {
    static let service = "com.business.memory-bridge.windesk-smb-dualroute"
    static let account = "Example User"
    static let itemLabel = "BUSINESS WinDesk SMB (LAN + ZeroTier)"
    static let loginKeychainPath = archivePath("/Users/example/Library/Keychains/login.keychain-db")

    static let server = "192.0.2.10"
    static let lanServer = "192.0.2.10"
    static let share = "DeskShare"
    static let mountPoint = archivePath("/Users/example/WinDesk")
    static let mountExecutable = "/sbin/mount_smbfs"

    static let promptTimeoutSeconds: UInt64 = 12
    static let mountTimeoutSeconds: UInt64 = 45
}

// Only async-signal-safe work is performed by this handler. If launchd or the
// wrapper stops this helper while mount_smbfs is active, kill that exact child
// before exiting so a credential prompt cannot be left orphaned. Closing the
// PTY on _exit also sends the normal terminal hangup to the child session.
private var activeChildPID: sig_atomic_t = -1

private func terminationSignalHandler(_ signalNumber: Int32) {
    let processID = activeChildPID
    if processID > 0 {
        _ = Darwin.kill(processID, SIGKILL)
    }
    Darwin._exit(128 + signalNumber)
}

private func installTerminationSignalHandlers() {
    _ = Darwin.signal(SIGTERM, terminationSignalHandler)
    _ = Darwin.signal(SIGINT, terminationSignalHandler)
}

// Mount-mode exit codes are intentionally stable so the shell wrapper can log
// a safe category without ever forwarding PTY output.
private enum ToolExit: Int32 {
    case success = 0
    case enrollmentCancelled = 10
    case enrollmentFailed = 11
    case keychainLocked = 20
    case keychainItemMissing = 21
    case keychainInteractionDenied = 22
    case keychainFailure = 23
    case ptySpawnFailure = 30
    case ptyPromptTimeout = 31
    case ptyWriteFailure = 32
    case mountFailure = 40
    case invalidUsage = 64
    case selfTestFailure = 70
}

private enum KeychainFailure: Error {
    case locked
    case missing
    case interactionDenied
    case other(OSStatus)
}

private struct StatusFailure: Error {
    let status: OSStatus
}

private enum MountResult {
    case success
    case keychain(KeychainFailure)
    case spawnFailure
    case promptTimeout
    case writeFailure
    case childFailure
}

@inline(__always)
private func terminate(_ code: ToolExit) -> Never {
    Darwin.exit(code.rawValue)
}

private func loginKeychain() -> Result<SecKeychain, KeychainFailure> {
    var keychain: SecKeychain?
    let openStatus = Config.loginKeychainPath.withCString {
        SecKeychainOpen($0, &keychain)
    }
    guard openStatus == errSecSuccess, let keychain else {
        return .failure(.other(openStatus))
    }

    var status: SecKeychainStatus = 0
    let statusResult = SecKeychainGetStatus(keychain, &status)
    guard statusResult == errSecSuccess else {
        return .failure(.other(statusResult))
    }
    guard (status & SecKeychainStatus(kSecUnlockStateStatus)) != 0 else {
        return .failure(.locked)
    }
    return .success(keychain)
}

private func baseItemQuery(keychain: SecKeychain) -> [CFString: Any] {
    [
        kSecClass: kSecClassGenericPassword,
        kSecAttrService: Config.service,
        kSecAttrAccount: Config.account,
        kSecUseKeychain: keychain,
        // Never let a launchd retry turn into a credential/ACL dialog.
        kSecUseAuthenticationUI: kSecUseAuthenticationUIFail,
    ]
}

private func mapKeychainStatus(_ status: OSStatus) -> KeychainFailure {
    switch status {
    case errSecItemNotFound:
        return .missing
    case errSecInteractionNotAllowed, errSecAuthFailed, errSecUserCanceled:
        return .interactionDenied
    default:
        return .other(status)
    }
}

// Checks presence without requesting the secret. This happens before forkpty,
// so a locked/missing item cannot start mount_smbfs or trigger its prompt.
private func preflightSecret() -> Result<Void, KeychainFailure> {
    switch loginKeychain() {
    case let .failure(failure):
        return .failure(failure)
    case let .success(keychain):
        var query = baseItemQuery(keychain: keychain)
        query[kSecMatchLimit] = kSecMatchLimitOne
        query[kSecReturnAttributes] = kCFBooleanTrue
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else {
            return .failure(mapKeychainStatus(status))
        }
        return .success(())
    }
}

// The returned Data is never turned into a String. The caller zeroes its
// mutable copy immediately after the single PTY write.
private func fetchSecret() -> Result<Data, KeychainFailure> {
    switch loginKeychain() {
    case let .failure(failure):
        return .failure(failure)
    case let .success(keychain):
        var query = baseItemQuery(keychain: keychain)
        query[kSecMatchLimit] = kSecMatchLimitOne
        query[kSecReturnData] = kCFBooleanTrue
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess else {
            return .failure(mapKeychainStatus(status))
        }
        guard let data = result as? Data, !data.isEmpty else {
            return .failure(.other(errSecDecode))
        }
        return .success(data)
    }
}

private func selfAccess() -> Result<SecAccess, StatusFailure> {
    var trustedApplication: SecTrustedApplication?
    let trustedStatus = SecTrustedApplicationCreateFromPath(nil, &trustedApplication)
    guard trustedStatus == errSecSuccess, let trustedApplication else {
        return .failure(StatusFailure(status: trustedStatus))
    }

    var access: SecAccess?
    let trustedList = [trustedApplication] as CFArray
    let accessStatus = SecAccessCreate(Config.itemLabel as CFString, trustedList, &access)
    guard accessStatus == errSecSuccess, let access else {
        return .failure(StatusFailure(status: accessStatus))
    }
    return .success(access)
}

private func storeSecret(_ secret: Data) -> OSStatus {
    let keychain: SecKeychain
    switch loginKeychain() {
    case let .failure(.other(status)):
        return status
    case .failure(.locked):
        return errSecInteractionNotAllowed
    case .failure:
        return errSecNotAvailable
    case let .success(value):
        keychain = value
    }

    let access: SecAccess
    switch selfAccess() {
    case let .failure(failure):
        return failure.status
    case let .success(value):
        access = value
    }

    var addQuery: [CFString: Any] = [
        kSecClass: kSecClassGenericPassword,
        kSecAttrService: Config.service,
        kSecAttrAccount: Config.account,
        kSecAttrLabel: Config.itemLabel,
        kSecAttrDescription: "Local-only SMB credential for BUSINESS WinDesk",
        kSecAttrSynchronizable: kCFBooleanFalse as Any,
        kSecAttrAccess: access,
        kSecValueData: secret,
        kSecUseKeychain: keychain,
        kSecUseAuthenticationUI: kSecUseAuthenticationUIFail,
    ]

    let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
    guard addStatus == errSecDuplicateItem else {
        return addStatus
    }

    // An existing item is updated in place; no delete/re-add gap is created.
    // UI is forbidden here too. If the stable binary identity no longer owns
    // the ACL, this fails closed instead of presenting repeated prompts.
    addQuery.removeValue(forKey: kSecValueData)
    addQuery.removeValue(forKey: kSecAttrLabel)
    addQuery.removeValue(forKey: kSecAttrDescription)
    addQuery.removeValue(forKey: kSecAttrSynchronizable)
    addQuery.removeValue(forKey: kSecAttrAccess)

    let updateValues: [CFString: Any] = [
        kSecValueData: secret,
        kSecAttrLabel: Config.itemLabel,
        kSecAttrDescription: "Local-only SMB credential for BUSINESS WinDesk",
        kSecAttrAccess: access,
    ]
    return SecItemUpdate(addQuery as CFDictionary, updateValues as CFDictionary)
}

private enum GUIRunMode {
    case enrollment
    case uiSelfTest
}

private final class NativeGUIAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let mode: GUIRunMode
    private var window: NSWindow?
    private var firstPasswordField: NSSecureTextField?
    private var secondPasswordField: NSSecureTextField?
    private var statusLabel: NSTextField?
    private var storeButton: NSButton?
    private var cancelButton: NSButton?
    private var started = false
    private var finishing = false
    private(set) var result: ToolExit = .selfTestFailure

    init(mode: GUIRunMode) {
        self.mode = mode
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        startIfNeeded()
    }

    func startIfNeeded() {
        guard !started else { return }
        started = true

        let contentRect = mode == .uiSelfTest
            ? NSRect(x: 0, y: 0, width: 540, height: 210)
            : NSRect(x: 0, y: 0, width: 580, height: 350)
        let window = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = mode == .uiSelfTest
            ? "WinDesk GUI 自检（不访问钥匙串）"
            : "WinDesk 密码安全登记"
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.moveToActiveSpace]
        window.tabbingMode = .disallowed
        window.delegate = self
        window.center()
        window.contentView = mode == .uiSelfTest
            ? makeSelfTestContent(frame: contentRect)
            : makeEnrollmentContent(frame: contentRect)
        self.window = window

        NSApplication.shared.unhide(nil)
        NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()

        if mode == .enrollment, let firstPasswordField {
            window.initialFirstResponder = firstPasswordField
            window.makeFirstResponder(firstPasswordField)
        }

        // A second foreground request after LaunchServices has finished its
        // activation handoff avoids a race where the window exists but lands
        // behind the invoking application.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak window] in
            guard let window else { return }
            NSRunningApplication.current.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
            NSApplication.shared.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
        }

        if mode == .uiSelfTest {
            Timer.scheduledTimer(withTimeInterval: 12, repeats: false) { [weak self] _ in
                guard let self else { return }
                self.result = .success
                self.finish()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !finishing {
            clearPasswordFields()
            result = mode == .enrollment ? .enrollmentCancelled : .success
        }
        return .terminateNow
    }

    func applicationWillTerminate(_ notification: Notification) {
        clearPasswordFields()
    }

    func windowWillClose(_ notification: Notification) {
        if !finishing {
            clearPasswordFields()
            result = mode == .enrollment ? .enrollmentCancelled : .success
        }
    }

    private func makeSelfTestContent(frame: NSRect) -> NSView {
        let contentView = NSView(frame: frame)

        let title = NSTextField(labelWithString: "可见窗口检查通过后将自动关闭")
        title.font = NSFont.systemFont(ofSize: 20, weight: .semibold)
        title.alignment = .center

        let message = NSTextField(wrappingLabelWithString:
            "这是纯离线 UI 自检：不读取或写入钥匙串，不连接 SMB，也不处理任何凭据。窗口将在 12 秒后自动关闭。"
        )
        message.font = NSFont.systemFont(ofSize: 14)
        message.alignment = .center
        message.textColor = .secondaryLabelColor

        [title, message].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            contentView.addSubview($0)
        }
        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 40),
            title.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 34),
            title.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -34),
            message.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 22),
            message.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 48),
            message.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -48),
        ])
        return contentView
    }

    private func makeEnrollmentContent(frame: NSRect) -> NSView {
        let contentView = NSView(frame: frame)

        let title = NSTextField(labelWithString: "安全登记 WinDesk 密码")
        title.font = NSFont.systemFont(ofSize: 22, weight: .semibold)

        let explanation = NSTextField(wrappingLabelWithString:
            "密码将直接写入当前用户的 login Keychain；不会进入命令参数、环境变量、终端输出、日志、配置或状态文件。"
        )
        explanation.font = NSFont.systemFont(ofSize: 13)
        explanation.textColor = .secondaryLabelColor

        let firstLabel = NSTextField(labelWithString: "Windows DeskShare 密码")
        let secondLabel = NSTextField(labelWithString: "再次输入以确认")
        let firstField = NSSecureTextField()
        let secondField = NSSecureTextField()
        firstField.placeholderString = "输入密码"
        secondField.placeholderString = "再次输入密码"
        firstField.setAccessibilityLabel("Windows DeskShare 密码")
        secondField.setAccessibilityLabel("再次输入以确认")

        let status = NSTextField(wrappingLabelWithString: "等待输入；关闭窗口或按取消不会写入任何内容。")
        status.font = NSFont.systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor

        let store = NSButton(title: "安全存入", target: self, action: #selector(storePressed))
        store.bezelStyle = .rounded
        store.keyEquivalent = "\r"
        let cancel = NSButton(title: "取消", target: self, action: #selector(cancelPressed))
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"

        [title, explanation, firstLabel, secondLabel, firstField, secondField, status, store, cancel].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            contentView.addSubview($0)
        }
        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 26),
            title.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 36),
            title.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -36),

            explanation.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 9),
            explanation.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            explanation.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            firstLabel.topAnchor.constraint(equalTo: explanation.bottomAnchor, constant: 18),
            firstLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            firstLabel.widthAnchor.constraint(equalToConstant: 150),
            firstField.centerYAnchor.constraint(equalTo: firstLabel.centerYAnchor),
            firstField.leadingAnchor.constraint(equalTo: firstLabel.trailingAnchor, constant: 12),
            firstField.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            secondLabel.topAnchor.constraint(equalTo: firstLabel.bottomAnchor, constant: 18),
            secondLabel.leadingAnchor.constraint(equalTo: firstLabel.leadingAnchor),
            secondLabel.widthAnchor.constraint(equalTo: firstLabel.widthAnchor),
            secondField.centerYAnchor.constraint(equalTo: secondLabel.centerYAnchor),
            secondField.leadingAnchor.constraint(equalTo: secondLabel.trailingAnchor, constant: 12),
            secondField.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            status.topAnchor.constraint(equalTo: secondLabel.bottomAnchor, constant: 20),
            status.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            status.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            cancel.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            cancel.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -24),
            store.trailingAnchor.constraint(equalTo: cancel.leadingAnchor, constant: -12),
            store.centerYAnchor.constraint(equalTo: cancel.centerYAnchor),
        ])

        firstField.nextKeyView = secondField
        secondField.nextKeyView = store
        self.firstPasswordField = firstField
        self.secondPasswordField = secondField
        self.statusLabel = status
        self.storeButton = store
        self.cancelButton = cancel
        return contentView
    }

    @objc private func storePressed() {
        guard mode == .enrollment,
              let firstPasswordField,
              let secondPasswordField else { return }

        var first = Data(firstPasswordField.stringValue.utf8)
        var second = Data(secondPasswordField.stringValue.utf8)
        clearPasswordFields()
        defer {
            first.resetBytes(in: 0..<first.count)
            second.resetBytes(in: 0..<second.count)
        }

        guard !first.isEmpty else {
            showStatus("未写入：密码不能为空，请重新输入。", color: .systemOrange)
            window?.makeFirstResponder(firstPasswordField)
            return
        }
        guard first == second else {
            showStatus("未写入：两次输入不一致，请重新输入。", color: .systemOrange)
            window?.makeFirstResponder(firstPasswordField)
            return
        }

        setEnrollmentControlsEnabled(false)
        let status = storeSecret(first)
        guard status == errSecSuccess else {
            showStatus(enrollmentFailureMessage(status), color: .systemRed)
            setEnrollmentControlsEnabled(true)
            window?.makeFirstResponder(firstPasswordField)
            return
        }

        result = .success
        showStatus("已安全登记。Keychain 项目已更新，窗口即将关闭。", color: .systemGreen)
        Timer.scheduledTimer(withTimeInterval: 1.2, repeats: false) { [weak self] _ in
            self?.finish()
        }
    }

    @objc private func cancelPressed() {
        guard mode == .enrollment else { return }
        clearPasswordFields()
        result = .enrollmentCancelled
        finish()
    }

    private func enrollmentFailureMessage(_ status: OSStatus) -> String {
        switch status {
        case errSecInteractionNotAllowed:
            return "未写入：login Keychain 已锁定或系统拒绝非预期交互，请先在系统中解锁后重试。"
        case errSecAuthFailed, errSecUserCanceled:
            return "未写入：当前应用身份无权更新该项目（状态码 \(status)）。请按轮换说明重新登记。"
        default:
            return "未完成安全登记（状态码 \(status)）；密码没有写到其他位置。"
        }
    }

    private func showStatus(_ text: String, color: NSColor) {
        statusLabel?.stringValue = text
        statusLabel?.textColor = color
    }

    private func setEnrollmentControlsEnabled(_ enabled: Bool) {
        firstPasswordField?.isEnabled = enabled
        secondPasswordField?.isEnabled = enabled
        storeButton?.isEnabled = enabled
        cancelButton?.isEnabled = enabled
    }

    private func clearPasswordFields() {
        firstPasswordField?.stringValue = ""
        secondPasswordField?.stringValue = ""
    }

    private func finish() {
        guard !finishing else { return }
        finishing = true
        clearPasswordFields()
        window?.orderOut(nil)
        NSApplication.shared.terminate(nil)
    }
}

private func runNativeGUIApplication(_ mode: GUIRunMode) -> Never {
    let application = NSApplication.shared
    application.setActivationPolicy(.regular)
    let delegate = NativeGUIAppDelegate(mode: mode)
    application.delegate = delegate
    application.finishLaunching()
    delegate.startIfNeeded()
    application.run()
    terminate(delegate.result)
}

@main
private struct WinDeskKeychainEntry {
    static func main() {
        // Normal app launch opens the user-controlled enrollment window.
        if CommandLine.arguments.count == 1 {
            runNativeGUIApplication(.enrollment)
        }
        guard CommandLine.arguments.count == 2 else {
            terminate(.invalidUsage)
        }
        switch CommandLine.arguments[1] {
        case "enroll":
            runNativeGUIApplication(.enrollment)
        case "ui-self-test":
            runNativeGUIApplication(.uiSelfTest)
        case "mount":
            installTerminationSignalHandlers()
            terminate(exitCode(for: mount()))
        case "mount-lan":
            installTerminationSignalHandlers()
            terminate(exitCode(for: mount(server: Config.lanServer)))
        case "self-test":
            terminate(extendedSelfTest() ? .success : .selfTestFailure)
        case "__pty-selftest-child":
            ptySelfTestChild()
        case "__pty-selftest-no-prompt-child":
            ptySelfTestNoPromptChild()
        case "__pty-selftest-mount-timeout-child":
            ptySelfTestMountTimeoutChild()
        default:
            terminate(.invalidUsage)
        }
    }
}

private struct PromptDetector {
    private var tail = Data()

    mutating func append(_ bytes: UnsafeRawBufferPointer) -> Bool {
        tail.append(bytes.bindMemory(to: UInt8.self))
        if tail.count > 1024 {
            tail.removeFirst(tail.count - 1024)
        }
        let text = String(decoding: tail, as: UTF8.self).lowercased()
        return text.contains("password") || text.contains("密码")
    }

    mutating func clear() {
        tail.resetBytes(in: 0..<tail.count)
        tail.removeAll(keepingCapacity: false)
    }
}

private func mountArguments(server: String = Config.server) -> [String] {
    // There is deliberately no colon/password component before '@'.
    ["//\(Config.account)@\(server)/\(Config.share)", Config.mountPoint]
}

private func disableTerminalEcho(_ fileDescriptor: Int32) -> Bool {
    var settings = termios()
    guard tcgetattr(fileDescriptor, &settings) == 0 else {
        return false
    }
    settings.c_lflag &= ~tcflag_t(ECHO | ECHOE | ECHOK | ECHONL)
    return tcsetattr(fileDescriptor, TCSANOW, &settings) == 0
}

private func writeAll(_ data: Data, to fileDescriptor: Int32) -> Bool {
    data.withUnsafeBytes { rawBuffer -> Bool in
        guard var baseAddress = rawBuffer.baseAddress else {
            return false
        }
        var remaining = rawBuffer.count
        while remaining > 0 {
            let written = Darwin.write(fileDescriptor, baseAddress, remaining)
            if written < 0 {
                if errno == EINTR { continue }
                return false
            }
            guard written > 0 else { return false }
            remaining -= written
            baseAddress = baseAddress.advanced(by: written)
        }
        return true
    }
}

// Never use blocking waitpid here: an SMB process stuck in a kernel path must
// not defeat the helper's deadline. A child that cannot be observed as reaped
// inside these two bounded windows is orphaned only for the few instructions
// until this one-shot helper exits, after which launchd reaps it.
@discardableResult
private func waitForExit(_ processID: pid_t, forNanoseconds duration: UInt64) -> Bool {
    var status: Int32 = 0
    let deadline = DispatchTime.now().uptimeNanoseconds + duration
    repeat {
        let result = waitpid(processID, &status, WNOHANG)
        if result == processID || (result < 0 && errno == ECHILD) { return true }
        if result < 0 && errno != EINTR { return true }
        usleep(20_000)
    } while DispatchTime.now().uptimeNanoseconds < deadline
    return false
}

@discardableResult
private func stopAndReap(_ processID: pid_t) -> Bool {
    _ = Darwin.kill(processID, SIGTERM)
    if waitForExit(processID, forNanoseconds: 1_000_000_000) { return true }
    _ = Darwin.kill(processID, SIGKILL)
    return waitForExit(processID, forNanoseconds: 1_000_000_000)
}

private func childExitedSuccessfully(_ status: Int32) -> Bool {
    // Darwin wait status: low 7 bits are zero for normal exit; next byte is code.
    (status & 0x7f) == 0 && ((status >> 8) & 0xff) == 0
}

private func runThroughPTY(
    executable: String,
    arguments: [String],
    secretProvider: () -> Result<Data, KeychainFailure>,
    promptTimeoutNanoseconds: UInt64 = Config.promptTimeoutSeconds * 1_000_000_000,
    mountTimeoutNanoseconds: UInt64 = Config.mountTimeoutSeconds * 1_000_000_000
) -> MountResult {
    var cArguments: [UnsafeMutablePointer<CChar>?] =
        ([executable] + arguments).map { strdup($0) }
    guard !cArguments.contains(where: { $0 == nil }) else {
        cArguments.compactMap { $0 }.forEach { free($0) }
        return .spawnFailure
    }
    cArguments.append(nil)

    var master: Int32 = -1
    var processID: pid_t = -1
    cArguments.withUnsafeMutableBufferPointer { buffer in
        processID = forkpty(&master, nil, nil, nil)
        if processID == 0 {
            execv(buffer[0], buffer.baseAddress)
            _exit(127)
        }
    }
    cArguments.compactMap { $0 }.forEach { free($0) }

    guard processID > 0, master >= 0 else {
        if master >= 0 { Darwin.close(master) }
        return .spawnFailure
    }
    activeChildPID = processID
    guard disableTerminalEcho(master) else {
        stopAndReap(processID)
        activeChildPID = -1
        Darwin.close(master)
        return .writeFailure
    }

    let started = DispatchTime.now().uptimeNanoseconds
    let promptDeadline = started + promptTimeoutNanoseconds
    let mountDeadline = started + mountTimeoutNanoseconds
    var promptAnswered = false
    var detector = PromptDetector()
    var readBuffer = [UInt8](repeating: 0, count: 512)
    var childStatus: Int32 = 0

    defer {
        activeChildPID = -1
        detector.clear()
        readBuffer.withUnsafeMutableBytes { $0.initializeMemory(as: UInt8.self, repeating: 0) }
        Darwin.close(master)
    }

    while true {
        let waitResult = waitpid(processID, &childStatus, WNOHANG)
        if waitResult == processID {
            return childExitedSuccessfully(childStatus) ? .success : .childFailure
        }
        if waitResult < 0 && errno != EINTR {
            stopAndReap(processID)
            return .childFailure
        }

        let now = DispatchTime.now().uptimeNanoseconds
        if !promptAnswered && now >= promptDeadline {
            stopAndReap(processID)
            return .promptTimeout
        }
        if now >= mountDeadline {
            stopAndReap(processID)
            return .childFailure
        }

        var descriptor = pollfd(fd: master, events: Int16(POLLIN | POLLHUP), revents: 0)
        let pollResult = Darwin.poll(&descriptor, 1, 200)
        if pollResult < 0 {
            if errno == EINTR { continue }
            stopAndReap(processID)
            return .childFailure
        }
        guard pollResult > 0 else { continue }

        let count = readBuffer.withUnsafeMutableBytes {
            Darwin.read(master, $0.baseAddress, $0.count)
        }
        if count <= 0 {
            if count < 0 && (errno == EINTR || errno == EAGAIN) { continue }
            usleep(20_000)
            continue
        }

        if !promptAnswered {
            let sawPrompt = readBuffer.withUnsafeBytes {
                detector.append(UnsafeRawBufferPointer(rebasing: $0[..<count]))
            }
            if sawPrompt {
                var secret: Data
                switch secretProvider() {
                case let .failure(failure):
                    stopAndReap(processID)
                    return .keychain(failure)
                case let .success(value):
                    secret = value
                }
                defer { secret.resetBytes(in: 0..<secret.count) }

                guard !secret.isEmpty, writeAll(secret, to: master) else {
                    stopAndReap(processID)
                    return .writeFailure
                }
                var newline = Data([0x0a])
                defer { newline.resetBytes(in: 0..<newline.count) }
                guard writeAll(newline, to: master) else {
                    stopAndReap(processID)
                    return .writeFailure
                }
                detector.clear()
                promptAnswered = true
            }
        }
        // After the one write, all PTY output is deliberately discarded. Even
        // if a future mount_smbfs build unexpectedly echoes input, it cannot
        // reach stdout, stderr, the wrapper, or its log.
    }
}

private func mount(server: String = Config.server) -> MountResult {
    switch preflightSecret() {
    case let .failure(failure):
        return .keychain(failure)
    case .success:
        return runThroughPTY(
            executable: Config.mountExecutable,
            arguments: mountArguments(server: server),
            secretProvider: fetchSecret
        )
    }
}

private let selfTestMarker = Data([0x70, 0x74, 0x79, 0x2d, 0x6f, 0x6b])

private func ptySelfTestChild() -> Never {
    let prompt = Data("Password: ".utf8)
    _ = writeAll(prompt, to: STDERR_FILENO)
    var received = [UInt8](repeating: 0, count: 64)
    let count = received.withUnsafeMutableBytes {
        Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count)
    }
    let expected = Array(selfTestMarker) + [0x0a]
    let actual = count > 0 ? Array(received.prefix(count)) : []
    received.withUnsafeMutableBytes { $0.initializeMemory(as: UInt8.self, repeating: 0) }
    Darwin.exit(actual == expected ? 0 : 1)
}

private func ptySelfTestNoPromptChild() -> Never {
    _ = Darwin.signal(SIGTERM, SIG_IGN)
    while true { pause() }
}

private func ptySelfTestMountTimeoutChild() -> Never {
    _ = Darwin.signal(SIGTERM, SIG_IGN)
    let prompt = Data("Password: ".utf8)
    _ = writeAll(prompt, to: STDERR_FILENO)
    var received = [UInt8](repeating: 0, count: 64)
    _ = received.withUnsafeMutableBytes {
        Darwin.read(STDIN_FILENO, $0.baseAddress, $0.count)
    }
    received.withUnsafeMutableBytes { $0.initializeMemory(as: UInt8.self, repeating: 0) }
    while true { pause() }
}

private func selfTest() -> Bool {
    let arguments = mountArguments()
    guard arguments == ["//Example User@192.0.2.10/DeskShare", archivePath("/Users/example/WinDesk")] else {
        return false
    }
    guard mountArguments(server: Config.lanServer) == ["//Example User@192.0.2.10/DeskShare", archivePath("/Users/example/WinDesk")] else {
        return false
    }
    guard !arguments.joined(separator: "\u{0}").contains(Config.service) else {
        return false
    }
    guard let atIndex = arguments[0].firstIndex(of: "@") else { return false }
    let authority = arguments[0][arguments[0].startIndex..<atIndex]
    guard !authority.contains(":") else { return false }

    var detector = PromptDetector()
    let chunkOne = Data("Pass".utf8)
    let chunkTwo = Data("word: ".utf8)
    let firstResult = chunkOne.withUnsafeBytes { detector.append($0) }
    let secondResult = chunkTwo.withUnsafeBytes { detector.append($0) }
    detector.clear()
    guard !firstResult, secondResult else { return false }

    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.path
    let ptyResult = runThroughPTY(
        executable: executable,
        arguments: ["__pty-selftest-child"],
        secretProvider: { .success(selfTestMarker) }
    )
    if case .success = ptyResult { return true }
    return false
}

private func extendedSelfTest() -> Bool {
    guard selfTest() else { return false }

    let resultMappings: [(MountResult, Int32)] = [
        (.success, 0),
        (.keychain(.locked), 20),
        (.keychain(.missing), 21),
        (.keychain(.interactionDenied), 22),
        (.keychain(.other(errSecDecode)), 23),
        (.spawnFailure, 30),
        (.promptTimeout, 31),
        (.writeFailure, 32),
        (.childFailure, 40),
    ]
    for (result, expectedCode) in resultMappings {
        guard exitCode(for: result).rawValue == expectedCode else { return false }
    }

    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.path
    let promptStarted = DispatchTime.now().uptimeNanoseconds
    let promptTimeoutResult = runThroughPTY(
        executable: executable,
        arguments: ["__pty-selftest-no-prompt-child"],
        secretProvider: { .success(selfTestMarker) },
        promptTimeoutNanoseconds: 150_000_000,
        mountTimeoutNanoseconds: 2_500_000_000
    )
    let promptElapsed = DispatchTime.now().uptimeNanoseconds - promptStarted
    guard case .promptTimeout = promptTimeoutResult,
          promptElapsed < 2_500_000_000 else { return false }

    let mountStarted = DispatchTime.now().uptimeNanoseconds
    let mountTimeoutResult = runThroughPTY(
        executable: executable,
        arguments: ["__pty-selftest-mount-timeout-child"],
        secretProvider: { .success(selfTestMarker) },
        promptTimeoutNanoseconds: 1_000_000_000,
        mountTimeoutNanoseconds: 250_000_000
    )
    let mountElapsed = DispatchTime.now().uptimeNanoseconds - mountStarted
    guard case .childFailure = mountTimeoutResult,
          mountElapsed < 2_500_000_000 else { return false }

    return true
}

private func exitCode(for result: MountResult) -> ToolExit {
    switch result {
    case .success:
        return .success
    case .keychain(.locked):
        return .keychainLocked
    case .keychain(.missing):
        return .keychainItemMissing
    case .keychain(.interactionDenied):
        return .keychainInteractionDenied
    case .keychain(.other):
        return .keychainFailure
    case .spawnFailure:
        return .ptySpawnFailure
    case .promptTimeout:
        return .ptyPromptTimeout
    case .writeFailure:
        return .ptyWriteFailure
    case .childFailure:
        return .mountFailure
    }
}
