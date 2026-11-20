// SPDX-License-Identifier: GPL-3.0-or-later
import AppKit

/// A small window with the current status, the one action that makes sense right now, and the log.
final class ActivityWindowController: NSWindowController {
    private let engine: RelayEngine
    private let statusLabel = NSTextField(labelWithString: "")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let actionButton = NSButton(title: "", target: nil, action: nil)
    private let logView = NSTextView()
    private var primary: (() -> Void)?

    init(engine: RelayEngine) {
        self.engine = engine
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 440),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "Login Relay"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("LoginRelayActivity")
        super.init(window: window)
        build(in: window)
        update()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func build(in window: NSWindow) {
        let icon = NSImageView(image: NSApp.applicationIconImage)
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.widthAnchor.constraint(equalToConstant: 56).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 56).isActive = true

        statusLabel.font = .systemFont(ofSize: 17, weight: .semibold)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.font = .systemFont(ofSize: 13)
        detailLabel.preferredMaxLayoutWidth = 400
        let titles = NSStackView(views: [statusLabel, detailLabel])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 3

        let header = NSStackView(views: [icon, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 14

        actionButton.target = self
        actionButton.action = #selector(runPrimary)
        actionButton.bezelStyle = .rounded
        actionButton.keyEquivalent = "\r"
        let buttons = NSStackView(views: [actionButton])
        buttons.orientation = .horizontal
        buttons.spacing = 10

        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        logView.textContainerInset = NSSize(width: 6, height: 6)
        logView.autoresizingMask = [.width]
        let scroll = NSScrollView()
        scroll.documentView = logView
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true

        let logTitle = NSTextField(labelWithString: "Activity")
        logTitle.font = .systemFont(ofSize: 11, weight: .semibold)
        logTitle.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [header, buttons, logTitle, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -40).isActive = true

        window.contentView = NSView()
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            stack.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor),
        ])
    }

    func show() {
        update()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    @objc private func runPrimary() { primary?() }

    func update() {
        let (status, detail) = Self.describe(engine)
        statusLabel.stringValue = status
        detailLabel.stringValue = detail

        primary = nil
        actionButton.isHidden = true
        func offer(_ title: String, _ action: @escaping () -> Void) {
            actionButton.title = title
            actionButton.isHidden = false
            primary = action
        }
        switch engine.phase {
        case .ready:
            break
        case .working:
            offer("Cancel") { self.engine.cancelActive("Cancelled on the computer.", notifyPhone: true) }
        case .noPhone, .connecting:
            offer("Check Again") { self.engine.checkAgain() }
        }

        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        logView.string = engine.logLines.map { "\(f.string(from: $0.date))  \($0.text)" }.joined(separator: "\n")
        logView.scrollToEndOfDocument(nil)
    }

    /// Headline and one line of explanation for the current phase. Shared with the menu.
    static func describe(_ engine: RelayEngine) -> (String, String) {
        switch engine.phase {
        case .noPhone:
            return ("No phone found over USB",
                    "Plug the phone in and choose \u{201C}File transfer\u{201D} in its USB notification. USB debugging isn't needed.")
        case .connecting(let d, let detail):
            // Once the USB pipe is open the phone is plugged in and connected; it's just not answering yet.
            return (engine.linked ? "\(d.title) is connected over USB" : "Connecting to \(d.title)…",
                    detail.isEmpty ? "Waiting for the phone." : detail)
        case .ready(let d):
            return ("\(d.title) is ready", "Sign in to an app on the phone and the page opens here.")
        case .working(let flow):
            return ("\(flow.request.appName) \(flow.request.verb) in progress",
                    "Finish in your browser. If it asks to open Login Relay, allow it.")
        }
    }
}
