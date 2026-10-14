import AppKit
import Darwin
import WebKit
import UserNotifications

private func archivePath(_ legacy: String) -> String {
    let base = ProcessInfo.processInfo.environment["SYNC_CENTER_HOME"] ??
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/business-memory-sync-center").path
    let old = "/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync"
    if legacy == old || legacy.hasPrefix(old + "/") { return base + legacy.dropFirst(old.count) }
    let prefix = "/Users/example/"
    if legacy.hasPrefix(prefix) { return base + "/isolated-home/" + legacy.dropFirst(prefix.count) }
    return legacy
}

private enum OwnerRetirement {
    static func stopped() -> Bool {
        let paths = [
            (archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/config.json"), "retirement_active"),
            (archivePath("/Users/example/AI/source-governance/retirements.json"), "active")
        ]
        for (path, key) in paths {
            guard let data = FileManager.default.contents(atPath: path),
                  let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { return true }
            let value = key == "active" ? (root["business"] as? [String: Any])?[key] : root[key]
            guard let flag = value as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID(), flag.boolValue == false else { return true }
        }
        return false
    }
}

// The background helper runs inside this signed app bundle. It never activates
// a window or treats an accepted request as a delivered/read notification.
private enum SyncNotifications {
    static func reply(_ status: String, _ code: String? = nil) -> Never {
        var value: [String: Any] = ["status": status, "read_confirmed": false]
        if let code { value["error_code"] = code }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data)
        }
        exit(0)
    }

    static func runHelper() -> Never {
        guard !OwnerRetirement.stopped() else { reply("owner_retired", "company_automation_stopped") }
        // Do not instantiate NSApplication on this headless path. Even changing
        // its activation policy immediately can register a transient Dock tile
        // before the process exits, which makes the visible app appear to
        // relaunch every time the supervisor checks notification state.
        let bytes = FileHandle.standardInput.readData(ofLength: 4097)
        guard bytes.count <= 4096,
              let value = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: String],
              let identifier = value["identifier"],
              identifier.range(of: "^sync-[a-f0-9]{40}$", options: .regularExpression) != nil,
              let operation = value["operation"], ["send", "status"].contains(operation)
        else { reply("failed", "notification_contract_invalid") }
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                reply(settings.authorizationStatus == .denied ? "permission_off" : "permission_required", "notification_authorization_required")
            }
            center.getDeliveredNotifications { delivered in
                if delivered.contains(where: { $0.request.identifier == identifier }) { reply("visible") }
                if operation == "status" { reply("requested", "notification_not_in_center") }
                guard let body = value["body"], !body.isEmpty, body.count <= 700 else {
                    reply("failed", "notification_body_invalid")
                }
                let content = UNMutableNotificationContent()
                content.title = "业务记忆同步中心"
                content.body = body
                let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
                guard !OwnerRetirement.stopped() else { reply("owner_retired", "company_automation_stopped") }
                center.add(request) { error in
                    if error != nil { reply("failed", "notification_request_rejected") }
                    center.getDeliveredNotifications { notifications in
                        reply(notifications.contains(where: { $0.request.identifier == identifier }) ? "visible" : "requested")
                    }
                }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { reply("uncertain", "notification_timeout") }
        RunLoop.main.run()
        reply("uncertain", "notification_helper_interrupted")
    }

    static func requestPermission() {
        // Only called from an explicit menu selection; never from the daemon.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = granted ? "本机故障通知已允许" : "本机故障通知尚未开启"
                alert.informativeText = granted
                    ? "后台会重新核对仍未解决且明确未发送的故障。已知悉、历史恢复和送达不确定的通知不会因此重发；允许通知不代表故障已恢复。"
                    : "请你在系统设置 → 通知 → 业务记忆同步中心中开启允许通知。飞书通知和其他修复不受影响。"
                if error != nil { alert.informativeText = "系统未确认通知授权，请在系统设置中核对。不会把本次请求当作授权成功。" }
                alert.addButton(withTitle: "知道了")
                if !granted { alert.addButton(withTitle: "打开通知设置") }
                if alert.runModal() == .alertSecondButtonReturn,
                   let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }
}

private struct BackgroundServiceConfiguration {
    let label: String
    let plistPath: String
}

private struct ServiceRepairConfiguration {
    let services: [BackgroundServiceConfiguration]
    let repairLogPath: String
}

private struct CommandExecution {
    let status: Int32
    let output: String
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private let localURL = URL(string: "http://127.0.0.1:17626/")!
    private let configPath = archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/config.json")
    private let maintenanceStatePath = archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/state/maintenance-mode.json")
    private let maintenanceLockPath = archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/state/maintenance-mode.lock")
    private let repairableServiceLabels: Set<String> = [
        "com.example.business-sync-supervisor",
        "com.example.business-sync-supervisor-health",
        "com.example.business-memory-bridge",
        "com.example.business-memory-folder-watcher",
        "com.example.business-dingtalk-memory-listener",
        "com.example.dingtalk-minutes-review",
        "com.example.dingtalk-minutes-archive",
        "com.example.dingtalk-decision-notifier",
    ]
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var serviceRepairTimer: Timer?
    private var controlKeyMonitor: Any?
    private var attempts = 0
    private var observesTitle = false
    private var lastMaintenanceActive = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSLog("BUSINESS Memory Sync Center applicationDidFinishLaunching")
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        buildWindow()
        installControlKeyEditingShortcuts()
        guard !OwnerRetirement.stopped() else {
            window.title = "业务记忆同步中心 · 本人停用"
            webView.loadHTMLString("<meta charset='utf-8'><body style='font:18px -apple-system;padding:48px'><h2>本人停用</h2><p>公司自动采集、同步、处理和通知已停止。资料、账号和历史记录保留。</p></body>", baseURL: nil)
            return
        }
        lastMaintenanceActive = maintenanceIsActive()
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.ensureBackgroundServices()
        }
        scheduleBackgroundServiceRepair()
        startLocalServer()
        waitForServer()
        if !CommandLine.arguments.contains("--background") { NSApp.activate(ignoringOtherApps: true) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        setDashboardVisibility(false)
        sender.orderOut(nil)
        NSLog("BUSINESS Memory Sync Center window hidden; application remains active")
        return false
    }

    func windowDidMiniaturize(_ notification: Notification) {
        setDashboardVisibility(false)
    }

    func windowDidDeminiaturize(_ notification: Notification) {
        setDashboardVisibility(true)
    }

    func applicationDidHide(_ notification: Notification) {
        setDashboardVisibility(false)
    }

    func applicationDidUnhide(_ notification: Notification) {
        if window.isVisible && !window.isMiniaturized {
            setDashboardVisibility(true)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        serviceRepairTimer?.invalidate()
        serviceRepairTimer = nil
        if let controlKeyMonitor {
            NSEvent.removeMonitor(controlKeyMonitor)
            self.controlKeyMonitor = nil
        }
        if observesTitle {
            webView.removeObserver(self, forKeyPath: "title")
            observesTitle = false
        }
        guard let server, server.isRunning else { return }
        server.terminate()
        server.waitUntilExit()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        setDashboardVisibility(true)
        return true
    }

    private func setDashboardVisibility(_ visible: Bool) {
        guard webView != nil else { return }
        webView.evaluateJavaScript("window.setDashboardVisibility(\(visible ? "true" : "false"))") { _, error in
            if let error {
                NSLog("BUSINESS Memory Sync Center visibility update deferred: %@", String(describing: error))
            }
        }
    }

    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于业务记忆同步中心", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(withTitle: "启用本机故障通知…", action: #selector(enableLocalNotifications), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出业务记忆同步中心", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: NSSelectorFromString("undo:"), keyEquivalent: "z")
        let redoItem = editMenu.addItem(withTitle: "重做", action: NSSelectorFromString("redo:"), keyEquivalent: "z")
        redoItem.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: NSSelectorFromString("cut:"), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: NSSelectorFromString("copy:"), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: NSSelectorFromString("paste:"), keyEquivalent: "v")
        let pastePlainItem = editMenu.addItem(withTitle: "粘贴并匹配样式", action: NSSelectorFromString("pasteAsPlainText:"), keyEquivalent: "v")
        pastePlainItem.keyEquivalentModifierMask = [.command, .option, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "全选", action: NSSelectorFromString("selectAll:"), keyEquivalent: "a")
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(withTitle: "放大", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "缩小", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        viewMenu.addItem(withTitle: "实际大小", action: #selector(actualSize(_:)), keyEquivalent: "0")
        viewItem.submenu = viewMenu
        NSApp.mainMenu = main
    }

    @objc private func enableLocalNotifications() {
        SyncNotifications.requestPermission()
    }

    private func installControlKeyEditingShortcuts() {
        controlKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.window?.isKeyWindow == true else { return event }
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            guard flags.contains(.control), !flags.contains(.command),
                  let key = event.charactersIgnoringModifiers?.lowercased()
            else { return event }
            let action: Selector?
            switch key {
            case "a": action = NSSelectorFromString("selectAll:")
            case "c": action = NSSelectorFromString("copy:")
            case "v": action = NSSelectorFromString("paste:")
            case "x": action = NSSelectorFromString("cut:")
            case "z": action = flags.contains(.shift) ? NSSelectorFromString("redo:") : NSSelectorFromString("undo:")
            default: action = nil
            }
            guard let action else { return event }
            return NSApp.sendAction(action, to: nil, from: self.webView) ? nil : event
        }
    }

    @objc private func zoomIn(_ sender: Any?) {
        webView.setMagnification(min(2.0, webView.magnification + 0.1), centeredAt: .zero)
    }

    @objc private func zoomOut(_ sender: Any?) {
        webView.setMagnification(max(0.8, webView.magnification - 0.1), centeredAt: .zero)
    }

    @objc private func actualSize(_ sender: Any?) {
        webView.setMagnification(1.0, centeredAt: .zero)
    }

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        // Keep local drafts and original submission ids through application restarts.
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.allowsMagnification = true
        webView.setValue(false, forKey: "drawsBackground")
        webView.addObserver(self, forKeyPath: "title", options: [.new], context: nil)
        observesTitle = true
        webView.loadHTMLString("""
        <html style="color-scheme:light dark"><body style="margin:0;background:Canvas;color:CanvasText;font:14px -apple-system;display:grid;place-items:center;height:100vh"><div>正在启动业务记忆同步中心…</div></body></html>
        """, baseURL: nil)

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "业务记忆同步中心"
        window.titleVisibility = .visible
        window.titlebarAppearsTransparent = false
        window.isMovable = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.minSize = NSSize(width: 840, height: 640)
        window.contentView = webView
        window.center()
        if !CommandLine.arguments.contains("--background") { window.makeKeyAndOrderFront(nil) }
    }

    private func startLocalServer() {
        guard !OwnerRetirement.stopped() else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [
            archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/bin/dingtalk_decision_center.py"),
            "--host", "127.0.0.1", "--port", "17626"
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync"))
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            server = process
        } catch {
            NSLog("BUSINESS Memory Sync Center local service launch failed: %@", String(describing: error))
            showStartupError("本机服务无法启动。请重新打开应用；如仍失败，再查看运行状态。")
        }
    }

    private func restartLocalServerAfterMaintenance() {
        guard !OwnerRetirement.stopped() else { return }
        if let server, server.isRunning {
            server.terminate()
            server.waitUntilExit()
        }
        server = nil
        attempts = 0
        NSLog("BUSINESS Memory Sync Center maintenance ended; restarting local service")
        startLocalServer()
        waitForServer()
    }

    private func runLaunchctl(_ arguments: [String]) -> CommandExecution {
        if ["enable", "bootstrap", "kickstart"].contains(arguments.first ?? ""), OwnerRetirement.stopped() {
            return CommandExecution(status: -1, output: "owner_retired")
        }
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return CommandExecution(
                status: process.terminationStatus,
                output: String(data: data, encoding: .utf8) ?? ""
            )
        } catch {
            NSLog("BUSINESS Memory Sync Center launchctl failed: %@", String(describing: error))
            return CommandExecution(status: -1, output: "")
        }
    }

    private func loadServiceRepairConfiguration() -> ServiceRepairConfiguration? {
        guard
            let data = FileManager.default.contents(atPath: configPath),
            let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let syncCenter = root["sync_center"] as? [String: Any],
            let rows = syncCenter["background_services"] as? [[String: Any]],
            let repairLogPath = syncCenter["service_repair_log"] as? String,
            repairLogPath.hasPrefix(archivePath("/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/state/")),
            repairLogPath.hasSuffix(".jsonl")
        else {
            NSLog("BUSINESS Memory Sync Center skipped service repair: configuration unavailable or invalid")
            return nil
        }
        let labelPattern = try! NSRegularExpression(pattern: #"^[A-Za-z0-9.-]+$"#)
        let desiredRows = rows.filter { $0["desired_enabled"] as? Bool == true }
        guard !desiredRows.isEmpty else {
            NSLog("BUSINESS Memory Sync Center skipped service repair: desired service whitelist is empty")
            return nil
        }
        let services = desiredRows.compactMap { row -> BackgroundServiceConfiguration? in
            guard row["desired_enabled"] as? Bool == true,
                  let label = row["label"] as? String,
                  let plistPath = row["plist_path"] as? String,
                  repairableServiceLabels.contains(label),
                  labelPattern.firstMatch(in: label, range: NSRange(label.startIndex..., in: label)) != nil,
                  plistPath.hasPrefix(archivePath("/Users/example/Library/LaunchAgents/")),
                  plistPath.hasSuffix(".plist"),
                  (plistPath as NSString).lastPathComponent == "\(label).plist"
            else { return nil }
            return BackgroundServiceConfiguration(label: label, plistPath: plistPath)
        }
        guard services.count == desiredRows.count else {
            NSLog("BUSINESS Memory Sync Center skipped service repair: desired service whitelist contains an invalid entry")
            return nil
        }
        return ServiceRepairConfiguration(services: services, repairLogPath: repairLogPath)
    }

    private func serviceIsDisabled(_ label: String, output: String) -> Bool {
        let escaped = NSRegularExpression.escapedPattern(for: label)
        let pattern = #"\""# + escaped + #"\"\s*=>\s*(true|disabled)"#
        return output.range(of: pattern, options: .regularExpression) != nil
    }

    private func recordServiceRepair(
        label: String,
        actions: [String],
        statuses: [String: Int32],
        path: String
    ) {
        guard !actions.isEmpty else { return }
        let payload: [String: Any] = [
            "at": ISO8601DateFormatter().string(from: Date()),
            "label": label,
            "actions": actions,
            "statuses": statuses.mapValues { Int($0) },
        ]
        guard var data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return }
        data.append(0x0A)
        let manager = FileManager.default
        if !manager.fileExists(atPath: path) {
            let directory = (path as NSString).deletingLastPathComponent
            try? manager.createDirectory(atPath: directory, withIntermediateDirectories: true)
            manager.createFile(atPath: path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        do {
            let handle = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            try handle.close()
        } catch {
            NSLog("BUSINESS Memory Sync Center could not record service repair: %@", String(describing: error))
        }
    }

    private func ensureBackgroundServices() {
        guard !OwnerRetirement.stopped() else { return }
        guard let repairConfiguration = loadServiceRepairConfiguration() else { return }
        let descriptor = Darwin.open(maintenanceLockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            NSLog("BUSINESS Memory Sync Center skipped service repair: maintenance lock unavailable")
            return
        }
        defer {
            _ = flock(descriptor, LOCK_UN)
            Darwin.close(descriptor)
        }
        guard flock(descriptor, LOCK_SH) == 0 else {
            NSLog("BUSINESS Memory Sync Center skipped service repair: maintenance lock busy")
            return
        }
        guard !maintenanceIsActive() else {
            NSLog("BUSINESS Memory Sync Center is read-only during maintenance; service repair skipped")
            return
        }
        let domain = "gui/\(getuid())"
        let disabledState = runLaunchctl(["print-disabled", domain])
        for service in repairConfiguration.services {
            var actions: [String] = []
            var statuses: [String: Int32] = [:]
            if disabledState.status == 0 && serviceIsDisabled(service.label, output: disabledState.output) {
                let enable = runLaunchctl(["enable", "\(domain)/\(service.label)"])
                actions.append("enable")
                statuses["enable"] = enable.status
            }
            let printResult = runLaunchctl(["print", "\(domain)/\(service.label)"])
            if printResult.status != 0 {
                let bootstrap = runLaunchctl(["bootstrap", domain, service.plistPath])
                actions.append("bootstrap")
                statuses["bootstrap"] = bootstrap.status
            }
            if !actions.isEmpty {
                let verification = runLaunchctl(["print", "\(domain)/\(service.label)"])
                statuses["verify_loaded"] = verification.status
                if verification.status != 0 {
                    actions.append("verify_failed")
                }
                recordServiceRepair(
                    label: service.label,
                    actions: actions,
                    statuses: statuses,
                    path: repairConfiguration.repairLogPath
                )
                NSLog("BUSINESS Memory Sync Center service repair %@ actions=%@", service.label, actions.joined(separator: ","))
            }
        }
    }

    private func kickstartPostMaintenanceProbe() {
        guard !OwnerRetirement.stopped() else { return }
        let descriptor = Darwin.open(maintenanceLockPath, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            NSLog("BUSINESS Memory Sync Center skipped post-maintenance probe: maintenance lock unavailable")
            return
        }
        defer {
            _ = flock(descriptor, LOCK_UN)
            Darwin.close(descriptor)
        }
        guard flock(descriptor, LOCK_SH) == 0, !maintenanceIsActive() else {
            NSLog("BUSINESS Memory Sync Center skipped post-maintenance probe: maintenance active or lock busy")
            return
        }
        let domain = "gui/\(getuid())"
        let label = "com.example.business-sync-supervisor"
        let result = runLaunchctl(["kickstart", "\(domain)/\(label)"])
        guard let configuration = loadServiceRepairConfiguration() else { return }
        recordServiceRepair(
            label: label,
            actions: ["post_maintenance_probe"],
            statuses: ["kickstart": result.status],
            path: configuration.repairLogPath
        )
        NSLog("BUSINESS Memory Sync Center post-maintenance probe status=%d", result.status)
    }

    private func maintenanceIsActive() -> Bool {
        guard FileManager.default.fileExists(atPath: maintenanceStatePath) else { return true }
        guard
            let data = FileManager.default.contents(atPath: maintenanceStatePath),
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let status = payload["status"] as? String
        else {
            return true
        }
        return status != "inactive"
    }

    private func scheduleBackgroundServiceRepair() {
        serviceRepairTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            guard let self else { return }
            let maintenanceActive = self.maintenanceIsActive()
            let maintenanceExited = self.lastMaintenanceActive && !maintenanceActive
            if maintenanceExited {
                self.restartLocalServerAfterMaintenance()
                DispatchQueue.global(qos: .utility).async {
                    self.ensureBackgroundServices()
                    self.kickstartPostMaintenanceProbe()
                }
            } else if self.server?.isRunning != true {
                self.attempts = 0
                self.startLocalServer()
                self.waitForServer()
            }
            self.lastMaintenanceActive = maintenanceActive
            if !maintenanceActive && !maintenanceExited {
                DispatchQueue.global(qos: .utility).async {
                    self.ensureBackgroundServices()
                }
            }
        }
    }

    private func waitForServer() {
        guard server?.isRunning == true else { return }
        var request = URLRequest(url: localURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    NSLog("BUSINESS Memory Sync Center local service ready after %d probes", self.attempts + 1)
                    self.webView.load(URLRequest(url: self.localURL, cachePolicy: .reloadIgnoringLocalCacheData))
                    return
                }
                self.attempts += 1
                if self.server?.isRunning != true {
                    self.showStartupError("本机服务已退出。后台会检查服务状态；重新打开应用也可重试。")
                    return
                }
                if self.attempts == 40 {
                    NSLog("BUSINESS Memory Sync Center startup delayed; continuing readiness probes")
                    self.showStartupError("本机服务启动较慢，正在等待后台就绪。每 5 秒自动检查一次，恢复后会自动打开。")
                }
                let interval = self.attempts >= 40 ? 5.0 : 0.25
                DispatchQueue.main.asyncAfter(deadline: .now() + interval) { self.waitForServer() }
            }
        }.resume()
    }

    private func showStartupError(_ message: String) {
        let escaped = message.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("""
        <html style="color-scheme:light dark"><body style="margin:0;background:Canvas;color:CanvasText;font:14px -apple-system;display:grid;place-items:center;height:100vh"><div style="max-width:520px;line-height:1.7;text-align:center"><h2>无法打开同步中心</h2><p style="opacity:.65">\(escaped)</p></div></body></html>
        """, baseURL: nil)
    }

    override func observeValue(forKeyPath keyPath: String?, of object: Any?, change: [NSKeyValueChangeKey : Any]?, context: UnsafeMutableRawPointer?) {
        guard keyPath == "title", let title = webView.title else {
            super.observeValue(forKeyPath: keyPath, of: object, change: change, context: context)
            return
        }
        if let match = title.range(of: #"^\(([0-9]+)\)"#, options: .regularExpression) {
            let count = title[match].dropFirst().dropLast()
            NSApp.dockTile.badgeLabel = String(count)
        } else {
            NSApp.dockTile.badgeLabel = nil
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" || (url.scheme == "http" && url.host == "127.0.0.1" && url.port == 17626) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView.url == localURL {
            NSLog("BUSINESS Memory Sync Center dashboard navigation finished")
        }
    }
}

@main
struct DingTalkSyncCenterApplication {
    static func main() {
        if CommandLine.arguments.contains("--sync-notification") { SyncNotifications.runHelper() }
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.run()
    }
}
