// ShivaGPT — native Mac wrapper around the web UI.
//
// Single-file AppKit + WebKit app. No Xcode project required:
//   ./build.sh
//
// Defaults to http://kailash:8000. Override at launch time with the
// SHIVAGPT_URL env var, or change it from the app menu (Cmd-,).
//
// Persists window position/size and localStorage between launches via
// WKWebsiteDataStore.default() and setFrameAutosaveName.

import Cocoa
import WebKit

let DEFAULT_URL = "http://kailash:8000"
let URL_DEFAULTS_KEY = "shivagpt.serverURL"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!

    func applicationDidFinishLaunching(_ note: Notification) {
        let urlString = ProcessInfo.processInfo.environment["SHIVAGPT_URL"]
            ?? UserDefaults.standard.string(forKey: URL_DEFAULTS_KEY)
            ?? DEFAULT_URL
        let url = URL(string: urlString) ?? URL(string: DEFAULT_URL)!

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = WKWebsiteDataStore.default()  // persists localStorage
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled") // Cmd-Opt-I
        if #available(macOS 14.0, *) {
            cfg.preferences.inactiveSchedulingPolicy = .none
        }

        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.setValue(false, forKey: "drawsBackground")  // dark-friendly
        webView.load(URLRequest(url: url))

        let style: NSWindow.StyleMask = [
            .titled, .closable, .miniaturizable, .resizable, .fullSizeContentView,
        ]
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
            styleMask: style,
            backing: .buffered,
            defer: false
        )
        window.title = "ShivaGPT"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = false
        window.backgroundColor = NSColor(red: 0.043, green: 0.051, blue: 0.063, alpha: 1) // #0b0d10
        window.contentView = webView
        window.setFrameAutosaveName("ShivaGPTMainWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)

        NSApp.activate(ignoringOtherApps: true)
        installMenu()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // MARK: - Menu

    func installMenu() {
        let main = NSMenu()

        // App menu
        let appMenu = NSMenu()
        let appItem = NSMenuItem(); appItem.submenu = appMenu; main.addItem(appItem)
        appMenu.addItem(NSMenuItem(title: "About ShivaGPT",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Server URL…",
            action: #selector(promptForURL(_:)), keyEquivalent: ","))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Hide ShivaGPT",
            action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        let hideOthers = NSMenuItem(title: "Hide Others",
            action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(hideOthers)
        appMenu.addItem(NSMenuItem(title: "Show All",
            action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "Quit ShivaGPT",
            action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))

        // Edit menu — required so Cmd-X/C/V/A/Z work in the WebView
        let editMenu = NSMenu(title: "Edit")
        let editItem = NSMenuItem(); editItem.submenu = editMenu; main.addItem(editItem)
        editMenu.addItem(NSMenuItem(title: "Undo",       action: Selector(("undo:")),       keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(title: "Redo",       action: Selector(("redo:")),       keyEquivalent: "Z"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: "Cut",        action: #selector(NSText.cut(_:)),        keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy",       action: #selector(NSText.copy(_:)),       keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste",      action: #selector(NSText.paste(_:)),      keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)),  keyEquivalent: "a"))

        // View menu
        let viewMenu = NSMenu(title: "View")
        let viewItem = NSMenuItem(); viewItem.submenu = viewMenu; main.addItem(viewItem)
        viewMenu.addItem(NSMenuItem(title: "Reload",      action: #selector(reload(_:)),     keyEquivalent: "r"))
        viewMenu.addItem(NSMenuItem(title: "Hard Reload", action: #selector(hardReload(_:)), keyEquivalent: "R"))
        viewMenu.addItem(.separator())
        viewMenu.addItem(NSMenuItem(title: "Actual Size", action: #selector(zoomReset(_:)),  keyEquivalent: "0"))
        viewMenu.addItem(NSMenuItem(title: "Zoom In",     action: #selector(zoomIn(_:)),     keyEquivalent: "+"))
        viewMenu.addItem(NSMenuItem(title: "Zoom Out",    action: #selector(zoomOut(_:)),    keyEquivalent: "-"))
        viewMenu.addItem(.separator())
        viewMenu.addItem(NSMenuItem(title: "Enter Full Screen",
            action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f"))

        // Window menu
        let winMenu = NSMenu(title: "Window")
        let winItem = NSMenuItem(); winItem.submenu = winMenu; main.addItem(winItem)
        winMenu.addItem(NSMenuItem(title: "Minimize",
            action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        winMenu.addItem(NSMenuItem(title: "Zoom",
            action: #selector(NSWindow.performZoom(_:)), keyEquivalent: ""))
        NSApp.windowsMenu = winMenu

        NSApp.mainMenu = main
    }

    // MARK: - Actions

    @objc func reload(_ s: Any?)     { webView.reload() }
    @objc func hardReload(_ s: Any?) { webView.reloadFromOrigin() }
    @objc func zoomIn(_ s: Any?)     { webView.pageZoom += 0.1 }
    @objc func zoomOut(_ s: Any?)    { webView.pageZoom = max(0.3, webView.pageZoom - 0.1) }
    @objc func zoomReset(_ s: Any?)  { webView.pageZoom = 1.0 }

    @objc func promptForURL(_ s: Any?) {
        let alert = NSAlert()
        alert.messageText = "ShivaGPT server URL"
        alert.informativeText = "Where is the ShivaGPT server?\nDefault: \(DEFAULT_URL)"
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.stringValue = UserDefaults.standard.string(forKey: URL_DEFAULTS_KEY)
            ?? webView.url?.absoluteString
            ?? DEFAULT_URL
        alert.accessoryView = input
        alert.addButton(withTitle: "Open")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let s = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let u = URL(string: s) else {
            NSSound.beep(); return
        }
        UserDefaults.standard.set(s, forKey: URL_DEFAULTS_KEY)
        webView.load(URLRequest(url: u))
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        showLoadError(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLoadError(error)
    }

    private func showLoadError(_ error: Error) {
        let html = """
        <html><body style='background:#0b0d10;color:#e6e8eb;
            font-family:-apple-system,Helvetica,Arial,sans-serif;padding:48px;line-height:1.5'>
        <h2 style='color:#ff6b6b'>Couldn’t reach the ShivaGPT server</h2>
        <p>\(error.localizedDescription)</p>
        <p style='color:#9aa0a6'>Tried: <code>\(webView.url?.absoluteString ?? "(none)")</code></p>
        <p>Things to check:
          <ul>
            <li>Is the server running?  <code>ssh kailash 'systemctl status shivagpt'</code></li>
            <li>Is the host reachable?  <code>curl http://kailash:8000/healthz</code></li>
            <li>Set a different URL: <strong>ShivaGPT → Server URL…</strong> (⌘,)</li>
          </ul>
        </p>
        <p><a href='javascript:location.reload()' style='color:#7c5cff'>Try again</a></p>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // Open external links in the user’s default browser, not inside the app.
    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated,
           let url = navigationAction.request.url,
           let host = url.host,
           !host.contains("kailash"),
           !host.contains("localhost"),
           !host.contains("127.0.0.1") {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
