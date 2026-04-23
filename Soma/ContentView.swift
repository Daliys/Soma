//
//  ContentView.swift
//  Soma
//
//  Redesigned: two-mode app
//    Scout mode  — direct chat with local Llama (original behaviour)
//    Relay mode  — Little Brother gathers context → Llama 3.2 answers
//

import AppKit
import Combine
import SwiftUI

// MARK: - Models

struct ChatMessage: Codable, Sendable {
    let role: String
    let content: String?
}

struct OllamaResponse: Codable, Sendable {
    let response: String?
    let history: [[String: AnyCodable]]?
    let error: String?
}

struct GatherBundle: Codable, Sendable {
    let mode: String?
    let original_prompt: String?
    let project_root: String?
    let project_type: String?
    let routing_decision: String?
    let gather_reason: String?
    let confidence: Double?
    let gathered_files: [String: GatheredFile]?
    let evidence_items: [EvidenceItem]?
    let error_lines: [String]?
    let context_summary: String?
    let open_questions: [String]?
    let assumptions: [String]?
    let enriched_prompt: String?
    let error: String?
}

struct GatheredFile: Codable, Sendable, Hashable {
    let tool: String?
    let preview: String?
}

struct EvidenceItem: Codable, Sendable, Hashable {
    let path: String?
    let kind: String?
    let reason: String?
    let preview: String?
}

struct RelayResponse: Codable, Sendable {
    let response: String?
    let source: String?
    let routing_decision: String?
    let enriched_prompt: String?
    let files_used: [String]?
    let errors_found: Int?
    let error: String?
}

struct AnyCodable: Codable, Sendable {
    let value: Any

    init(_ value: Any) { self.value = value }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(String.self)                 { self.value = value }
        else if let value = try? container.decode(Int.self)               { self.value = value }
        else if let value = try? container.decode(Double.self)            { self.value = value }
        else if let value = try? container.decode(Bool.self)             { self.value = value }
        else if let value = try? container.decode([String: AnyCodable].self) { self.value = value }
        else if let value = try? container.decode([AnyCodable].self)     { self.value = value }
        else { throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unknown type") }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let value = value as? String                     { try container.encode(value) }
        else if let value = value as? Int                  { try container.encode(value) }
        else if let value = value as? Double               { try container.encode(value) }
        else if let value = value as? Bool                 { try container.encode(value) }
        else if let value = value as? [String: AnyCodable] { try container.encode(value) }
        else if let value = value as? [AnyCodable]         { try container.encode(value) }
    }
}

// MARK: - Enums

enum AppMode: String, CaseIterable, Identifiable {
    case scout = "🐶  Scout"
    case relay = "🔗  Relay"

    var id: String { rawValue }
}

enum RelayPhase: Equatable {
    case idle
    case gathering
    case relaying
    case done
    case failed(String)
}

// MARK: - Ollama Manager

final class OllamaManager: ObservableObject {
    @Published var isModelLoaded = false
    @Published var isOllamaRunning = false
    @Published var isBusy = false

    let modelName = "llama3.2:3b"
    private var timer: Timer?

    init() { startPolling() }

    func startPolling() {
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.checkStatus()
        }
        checkStatus()
    }

    func checkStatus() {
        guard let url = URL(string: "http://localhost:11434/api/ps") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2

        URLSession.shared.dataTask(with: request) { data, _, error in
            DispatchQueue.main.async {
                if error != nil {
                    self.isOllamaRunning = false
                    self.isModelLoaded = false
                    return
                }

                self.isOllamaRunning = true
                if
                    let data,
                    let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    let models = json["models"] as? [[String: Any]]
                {
                    self.isModelLoaded = models.contains {
                        ($0["name"] as? String)?.lowercased().hasPrefix(self.modelName.lowercased()) == true
                    }
                }
            }
        }.resume()
    }

    func launchOllama() {
        isBusy = true
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ollama")
            process.arguments = ["serve"]
            try? process.run()
            Thread.sleep(forTimeInterval: 4)
            DispatchQueue.main.async {
                self.isBusy = false
                self.checkStatus()
            }
        }
    }

    func startModel() { sendKeepAlive(-1) }
    func stopModel() { sendKeepAlive(0) }

    private func sendKeepAlive(_ keepAlive: Int) {
        guard let url = URL(string: "http://localhost:11434/api/generate") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "model": modelName,
            "prompt": "",
            "keep_alive": keepAlive,
            "stream": false,
        ])
        isBusy = true

        URLSession.shared.dataTask(with: request) { _, _, _ in
            DispatchQueue.main.async {
                self.isBusy = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                    self.checkStatus()
                }
            }
        }.resume()
    }
}

// MARK: - Main View

struct ContentView: View {
    @AppStorage("relay.lastProjectRoot") private var storedLastProjectRoot = ""
    @AppStorage("relay.recentProjectRoots") private var storedRecentRootsJSON = "[]"

    @StateObject private var ollama = OllamaManager()
    @State private var mode: AppMode = .relay

    @State private var scoutPrompt = ""
    @State private var scoutTranscript = ""
    @State private var scoutHistory: [[String: AnyCodable]] = []
    @State private var scoutLoading = false

    @State private var relayPrompt = ""
    @State private var relayPhase: RelayPhase = .idle
    @State private var gatherBundle: GatherBundle?
    @State private var relayResponse: RelayResponse?
    @State private var showContextPanel = false
    @State private var relayError: String?
    @State private var selectedProjectRoot = ""
    @State private var recentProjectRoots: [String] = []

    var body: some View {
        VStack(spacing: 0) {
            headerBar
            Divider()
            Group {
                if mode == .scout { scoutView }
                else { relayView }
            }
        }
        .frame(minWidth: 640, minHeight: 780)
        .background(Color(NSColor.windowBackgroundColor))
        .onAppear(perform: hydrateProjectRoots)
    }

    // MARK: Header

    private var headerBar: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Soma").font(.headline).bold()
                HStack(spacing: 5) {
                    Circle()
                        .fill(
                            ollama.isOllamaRunning
                                ? (ollama.isModelLoaded ? Color.green : Color.orange)
                                : Color.red
                        )
                        .frame(width: 7, height: 7)
                    Text(
                        ollama.isOllamaRunning
                            ? (ollama.isModelLoaded ? "Model ready" : "Ollama idle")
                            : "Offline"
                    )
                    .font(.caption2)
                    .foregroundColor(.secondary)
                }
            }

            Spacer()

            Picker("Mode", selection: $mode) {
                ForEach(AppMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
            .onChange(of: mode) { _, _ in resetState() }

            Spacer()

            Button(action: ollamaAction) {
                if ollama.isBusy {
                    ProgressView().controlSize(.small).frame(width: 80)
                } else {
                    Text(
                        ollama.isOllamaRunning
                            ? (ollama.isModelLoaded ? "Stop AI" : "Start AI")
                            : "Launch"
                    )
                    .frame(width: 80)
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(ollama.isBusy)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color(NSColor.windowBackgroundColor))
    }

    // MARK: Scout View

    private var scoutView: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if scoutTranscript.isEmpty {
                            emptyState(
                                icon: "folder.badge.magnifyingglass",
                                title: "Scout mode",
                                subtitle: "Chat directly with Llama 3.2 to explore your files"
                            )
                        } else {
                            Text(scoutTranscript)
                                .font(.system(.body, design: .monospaced))
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .textSelection(.enabled)
                        }

                        if scoutLoading {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("Soma is scouting…")
                                    .foregroundColor(.secondary)
                                    .italic()
                            }
                            .id("loading")
                        }
                    }
                    .padding()
                }
                .background(Color(NSColor.textBackgroundColor).opacity(0.5))
                .cornerRadius(8)
                .padding(.horizontal)
                .onChange(of: scoutTranscript) { _, _ in
                    proxy.scrollTo("loading", anchor: .bottom)
                }
            }

            inputBar(
                text: $scoutPrompt,
                placeholder: "Ask Soma to find or read files…",
                disabled: scoutLoading || !ollama.isOllamaRunning,
                buttonLabel: "Scout Files",
                icon: "magnifyingglass",
                action: runScout
            )
        }
    }

    // MARK: Relay View

    private var relayView: some View {
        VStack(spacing: 0) {
            projectRootPanel

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if relayPhase == .idle && gatherBundle == nil && relayResponse == nil && relayError == nil {
                        emptyState(
                            icon: "arrow.triangle.branch",
                            title: "Relay mode",
                            subtitle: "Use a project root for debugging prompts. Generic questions can go straight to the local model."
                        )
                    }

                    if relayPhase == .gathering {
                        phaseCard(
                            emoji: "🐶",
                            title: "Little Brother is gathering context…",
                            subtitle: "Scanning the selected project root for likely scripts, manifests and logs",
                            color: .orange
                        )
                    }

                    if relayPhase == .relaying {
                        phaseCard(
                            emoji: "🧠",
                            title: "Sending prompt to Llama 3.2…",
                            subtitle: "Forwarding the curated prompt to your local Ollama model",
                            color: .blue
                        )
                    }

                    if let bundle = gatherBundle, bundle.error == nil {
                        bundlePanel(bundle)
                    }

                    if let relay = relayResponse {
                        answerPanel(relay)
                    }

                    if let relayError {
                        Text("⚠️ \(relayError)")
                            .foregroundColor(.red)
                            .padding()
                    }
                }
                .padding()
            }
            .background(Color(NSColor.textBackgroundColor).opacity(0.5))
            .cornerRadius(8)
            .padding(.horizontal)

            inputBar(
                text: $relayPrompt,
                placeholder: "Describe your problem (e.g. \"my script does not work\" or \"explain this architecture\")",
                disabled: relayIsBusy || !ollama.isOllamaRunning,
                buttonLabel: "Ask Llama 3.2",
                icon: "bolt.fill",
                action: runRelay
            )
        }
    }

    private var projectRootPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Project Root", systemImage: "folder")
                    .font(.subheadline.bold())
                Spacer()
                if !recentProjectRoots.isEmpty {
                    Menu("Recent Roots") {
                        ForEach(recentProjectRoots, id: \.self) { root in
                            Button(shortPath(root)) {
                                selectProjectRoot(root)
                            }
                        }
                    }
                    .controlSize(.small)
                }
                Button("Choose Folder", action: chooseProjectRoot)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                if !selectedProjectRoot.isEmpty {
                    Button("Clear", action: clearProjectRoot)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }

            if selectedProjectRoot.isEmpty {
                Text("Select a project root for debugging, investigation, or log-driven prompts. General questions can still bypass local gathering.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Selected")
                        .font(.caption.bold())
                        .foregroundColor(.secondary)
                    Text(selectedProjectRoot)
                        .font(.caption)
                        .textSelection(.enabled)
                }
            }

            if !recentProjectRoots.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(recentProjectRoots, id: \.self) { root in
                            recentRootButton(root)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .padding(12)
        .background(Color.secondary.opacity(0.06))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.secondary.opacity(0.15)))
        .cornerRadius(10)
        .padding(.horizontal)
        .padding(.top)
    }

    // MARK: Components

    private var relayIsBusy: Bool {
        relayPhase == .gathering || relayPhase == .relaying
    }

    private func emptyState(icon: String, title: String, subtitle: String) -> some View {
        VStack(spacing: 12) {
            Spacer(minLength: 60)
            Image(systemName: icon)
                .font(.system(size: 44))
                .foregroundColor(.secondary.opacity(0.4))
            Text(title).font(.title3).bold()
            Text(subtitle)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private func phaseCard(emoji: String, title: String, subtitle: String, color: Color) -> some View {
        HStack(spacing: 14) {
            Text(emoji).font(.system(size: 30))
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(subtitle).font(.caption).foregroundColor(.secondary)
            }
            Spacer()
            ProgressView().controlSize(.regular)
        }
        .padding(14)
        .background(color.opacity(0.08))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(color.opacity(0.25)))
        .cornerRadius(10)
    }

    private func bundlePanel(_ bundle: GatherBundle) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: { withAnimation { showContextPanel.toggle() } }) {
                HStack {
                    Image(systemName: showContextPanel ? "chevron.down" : "chevron.right")
                        .foregroundColor(.secondary)
                    Text("📦 Context Bundle").font(.headline)
                    Spacer()
                    if let summary = bundle.context_summary {
                        Text(summary)
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .lineLimit(2)
                    }
                }
            }
            .buttonStyle(.plain)

            if showContextPanel {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 10) {
                        if let routing = bundle.routing_decision {
                            badge(text: routing.replacingOccurrences(of: "_", with: " "))
                        }
                        if let projectType = bundle.project_type {
                            badge(text: projectType)
                        }
                        if let confidence = bundle.confidence {
                            badge(text: String(format: "confidence %.2f", confidence))
                        }
                    }

                    if let reason = bundle.gather_reason {
                        labeledBlock(title: "Why This Route", text: reason)
                    }

                    if let root = bundle.project_root {
                        labeledBlock(title: "Project Root", text: root)
                    }

                    if let assumptions = bundle.assumptions, !assumptions.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Assumptions").font(.subheadline).bold()
                            ForEach(assumptions, id: \.self) { item in
                                Text("• \(item)")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }

                    if let evidence = bundle.evidence_items, !evidence.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Evidence (\(evidence.count))").font(.subheadline).bold()
                            ForEach(Array(evidence.enumerated()), id: \.offset) { _, item in
                                evidenceRow(item)
                            }
                        }
                    } else if
                        let files = bundle.gathered_files,
                        !files.isEmpty
                    {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Files Gathered (\(files.count))").font(.subheadline).bold()
                            ForEach(Array(files.keys.sorted()), id: \.self) { path in
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(URL(fileURLWithPath: path).lastPathComponent)
                                        .font(.caption.bold())
                                    Text(path)
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                    if let preview = files[path]?.preview {
                                        Text(preview)
                                            .font(.system(.caption2, design: .monospaced))
                                            .foregroundColor(.secondary)
                                            .lineLimit(4)
                                    }
                                }
                                .padding(8)
                                .background(Color(NSColor.controlBackgroundColor))
                                .cornerRadius(8)
                            }
                        }
                    }

                    if let errors = bundle.error_lines, !errors.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Detected Errors (\(errors.count))").font(.subheadline).bold()
                            ForEach(errors.prefix(6), id: \.self) { line in
                                Text(line)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundColor(.red)
                            }
                        }
                    }

                    if let questions = bundle.open_questions, !questions.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Open Questions").font(.subheadline).bold()
                            ForEach(questions, id: \.self) { question in
                                Text("• \(question)")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                }
                .padding(10)
                .background(Color(NSColor.controlBackgroundColor))
                .cornerRadius(8)
            }
        }
        .padding(12)
        .background(Color.green.opacity(0.06))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.green.opacity(0.2)))
        .cornerRadius(10)
    }

    private func evidenceRow(_ item: EvidenceItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(item.kind?.uppercased() ?? "FILE")
                    .font(.caption2.bold())
                    .foregroundColor(.secondary)
                Text(URL(fileURLWithPath: item.path ?? "").lastPathComponent)
                    .font(.caption.bold())
            }
            Text(item.path ?? "")
                .font(.caption2)
                .foregroundColor(.secondary)
                .textSelection(.enabled)
            if let reason = item.reason {
                Text(reason)
                    .font(.caption)
            }
            if let preview = item.preview, !preview.isEmpty {
                Text(preview)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundColor(.secondary)
                    .lineLimit(8)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(NSColor.textBackgroundColor))
        .cornerRadius(8)
    }

    private func answerPanel(_ relay: RelayResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("🧠 Llama 3.2 says").font(.headline)
                Spacer()
                if let routing = relay.routing_decision {
                    badge(text: routing.replacingOccurrences(of: "_", with: " "))
                }
                if let source = relay.source {
                    Label(
                        source == "llama_local" ? "Llama 3.2" : source,
                        systemImage: source == "llama_local" ? "cpu.fill" : "bolt.fill"
                    )
                    .font(.caption)
                    .foregroundColor(.secondary)
                }
            }

            Divider()

            if let response = relay.response {
                Text(response)
                    .font(.body)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }

            if let error = relay.error {
                Text("Error: \(error)")
                    .foregroundColor(.red)
            }
        }
        .padding(14)
        .background(Color.blue.opacity(0.06))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.blue.opacity(0.2)))
        .cornerRadius(10)
    }

    private func badge(text: String) -> some View {
        Text(text)
            .font(.caption2.bold())
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Color.secondary.opacity(0.12))
            .cornerRadius(999)
    }

    private func recentRootButton(_ root: String) -> some View {
        Group {
            if root == selectedProjectRoot {
                Button(action: { selectProjectRoot(root) }) {
                    Text(shortPath(root))
                        .lineLimit(1)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            } else {
                Button(action: { selectProjectRoot(root) }) {
                    Text(shortPath(root))
                        .lineLimit(1)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
    }

    private func labeledBlock(title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline).bold()
            Text(text)
                .font(.caption)
                .foregroundColor(.secondary)
                .textSelection(.enabled)
        }
    }

    private func inputBar(
        text: Binding<String>,
        placeholder: String,
        disabled: Bool,
        buttonLabel: String,
        icon: String,
        action: @escaping () -> Void
    ) -> some View {
        VStack(spacing: 8) {
            ZStack(alignment: .topLeading) {
                if text.wrappedValue.isEmpty {
                    Text(placeholder)
                        .foregroundColor(.secondary)
                        .padding(.leading, 5)
                        .padding(.top, 8)
                        .font(.body)
                        .allowsHitTesting(false)
                }

                TextEditor(text: text)
                    .font(.body)
                    .frame(minHeight: 60, maxHeight: 100)
                    .padding(4)
                    .background(Color.clear)
                    .onSubmit { if !disabled { action() } }
            }
            .background(Color(NSColor.controlBackgroundColor))
            .cornerRadius(6)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.gray.opacity(0.2)))

            HStack {
                if mode == .relay {
                    Button("Clear", action: resetState)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }

                Spacer()

                Button(action: action) {
                    HStack {
                        Image(systemName: icon)
                        Text(buttonLabel)
                    }
                    .bold()
                    .padding(.horizontal, 8)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.regular)
                .disabled(disabled || text.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .keyboardShortcut(.return, modifiers: .command)
            }
        }
        .padding()
    }

    // MARK: Actions

    private func ollamaAction() {
        if !ollama.isOllamaRunning { ollama.launchOllama() }
        else if ollama.isModelLoaded { ollama.stopModel() }
        else { ollama.startModel() }
    }

    private func resetState() {
        scoutPrompt = ""
        scoutTranscript = ""
        scoutHistory = []
        scoutLoading = false

        relayPrompt = ""
        relayPhase = .idle
        gatherBundle = nil
        relayResponse = nil
        showContextPanel = false
        relayError = nil
    }

    private func chooseProjectRoot() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose Project Root"

        guard panel.runModal() == .OK, let path = panel.url?.path else { return }
        selectProjectRoot(path)
    }

    private func selectProjectRoot(_ path: String) {
        guard let normalized = validatedDirectoryPath(path) else { return }
        selectedProjectRoot = normalized
        recentProjectRoots = deduplicatedRoots([normalized] + recentProjectRoots).prefix(6).map(\.self)
        persistProjectRoots()
    }

    private func clearProjectRoot() {
        selectedProjectRoot = ""
        storedLastProjectRoot = ""
    }

    private func hydrateProjectRoots() {
        recentProjectRoots = decodeRecentRoots()
        if selectedProjectRoot.isEmpty, let restored = validatedDirectoryPath(storedLastProjectRoot) {
            selectedProjectRoot = restored
        }
        if !selectedProjectRoot.isEmpty {
            recentProjectRoots = deduplicatedRoots([selectedProjectRoot] + recentProjectRoots).prefix(6).map(\.self)
        }
        persistProjectRoots()
    }

    private func persistProjectRoots() {
        storedLastProjectRoot = selectedProjectRoot
        storedRecentRootsJSON = encodeRecentRoots(recentProjectRoots)
    }

    private func runScout() {
        let prompt = scoutPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }

        scoutLoading = true
        scoutTranscript += "\n> \(prompt)\n\n"
        scoutPrompt = ""

        Task {
            do {
                let result = try await runPythonChat(prompt: prompt, history: scoutHistory)
                await MainActor.run {
                    scoutTranscript += (result.response ?? "") + "\n"
                    scoutHistory = result.history ?? []
                    scoutLoading = false
                    ollama.checkStatus()
                }
            } catch {
                await MainActor.run {
                    scoutTranscript += "⚠️ Error: \(error.localizedDescription)\n"
                    scoutLoading = false
                }
            }
        }
    }

    private func runRelay() {
        let prompt = relayPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }

        relayPrompt = ""
        gatherBundle = nil
        relayResponse = nil
        relayError = nil
        showContextPanel = false

        let shouldGather = promptLikelyNeedsGather(prompt)
        if shouldGather && selectedProjectRoot.isEmpty {
            relayPhase = .failed("Select a project root before relaying debugging or investigation prompts.")
            relayError = "Select a project root before relaying debugging or investigation prompts."
            return
        }

        Task {
            do {
                let bundle: GatherBundle
                if shouldGather {
                    await MainActor.run { relayPhase = .gathering }
                    let gathered = try await runGather(
                        prompt: prompt,
                        projectRoot: selectedProjectRoot,
                        recentRoots: recentProjectRoots
                    )
                    if let error = gathered.error {
                        throw SomaError(error)
                    }
                    bundle = gathered
                } else {
                    bundle = makeDirectBundle(prompt: prompt)
                }

                await MainActor.run {
                    gatherBundle = bundle
                    showContextPanel = bundle.routing_decision != "direct_pass_through"
                    relayPhase = .relaying
                }

                let response = try await runRelayScript(bundle: bundle)
                await MainActor.run {
                    relayResponse = response
                    relayPhase = .done
                    ollama.checkStatus()
                }
            } catch {
                await MainActor.run {
                    relayPhase = .failed(error.localizedDescription)
                    relayError = error.localizedDescription
                }
            }
        }
    }

    private func makeDirectBundle(prompt: String) -> GatherBundle {
        GatherBundle(
            mode: "gather",
            original_prompt: prompt,
            project_root: selectedProjectRoot.isEmpty ? nil : selectedProjectRoot,
            project_type: nil,
            routing_decision: "direct_pass_through",
            gather_reason: "Prompt did not look like a debugging or investigation request, so it was relayed directly.",
            confidence: 1.0,
            gathered_files: [:],
            evidence_items: [],
            error_lines: [],
            context_summary: "Prompt was forwarded directly without local evidence gathering.",
            open_questions: [],
            assumptions: [],
            enriched_prompt: prompt,
            error: nil
        )
    }

    private func promptLikelyNeedsGather(_ prompt: String) -> Bool {
        let lowered = prompt.lowercased()
        let tokens = [
            "bug",
            "broken",
            "build",
            "config",
            "crash",
            "debug",
            "error",
            "exception",
            "fail",
            "failing",
            "issue",
            "log",
            "not work",
            "problem",
            "script",
            "stack trace",
            "traceback",
        ]
        return tokens.contains(where: lowered.contains) || lowered.contains(".py") || lowered.contains(".swift")
    }

    // MARK: Persistence

    private func decodeRecentRoots() -> [String] {
        guard
            let data = storedRecentRootsJSON.data(using: .utf8),
            let decoded = try? JSONDecoder().decode([String].self, from: data)
        else {
            return []
        }
        return deduplicatedRoots(decoded.compactMap(validatedDirectoryPath))
    }

    private func encodeRecentRoots(_ roots: [String]) -> String {
        guard let data = try? JSONEncoder().encode(roots), let json = String(data: data, encoding: .utf8) else {
            return "[]"
        }
        return json
    }

    private func validatedDirectoryPath(_ path: String) -> String? {
        guard !path.isEmpty else { return nil }
        let expanded = NSString(string: path).expandingTildeInPath
        let normalized = URL(fileURLWithPath: expanded).resolvingSymlinksInPath().path
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: normalized, isDirectory: &isDirectory), isDirectory.boolValue else {
            return nil
        }
        return normalized
    }

    private func deduplicatedRoots(_ roots: [String]) -> [String] {
        var seen = Set<String>()
        return roots.filter { root in
            guard !seen.contains(root) else { return false }
            seen.insert(root)
            return true
        }
    }

    private func shortPath(_ path: String) -> String {
        let home = NSHomeDirectory()
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~/" + path.dropFirst(home.count + 1)
        }
        return path
    }

    // MARK: Script runners

    private func runPythonChat(
        prompt: String,
        history: [[String: AnyCodable]]
    ) async throws -> OllamaResponse {
        guard let script = Bundle.main.url(forResource: "scout_pipeline", withExtension: "py") else {
            throw SomaError("scout_pipeline.py not found in bundle")
        }
        let historyJSON = (try? String(data: JSONEncoder().encode(history), encoding: .utf8)) ?? "[]"
        let output = try await runScript(
            path: "/opt/homebrew/bin/python3",
            args: [script.path, prompt, historyJSON]
        )
        return try JSONDecoder().decode(OllamaResponse.self, from: output)
    }

    private func runGather(
        prompt: String,
        projectRoot: String,
        recentRoots: [String]
    ) async throws -> GatherBundle {
        guard let script = Bundle.main.url(forResource: "scout_pipeline", withExtension: "py") else {
            throw SomaError("scout_pipeline.py not found in bundle")
        }
        let recentRootsJSON = (try? String(data: JSONEncoder().encode(recentRoots), encoding: .utf8)) ?? "[]"
        let output = try await runScript(
            path: "/opt/homebrew/bin/python3",
            args: [
                script.path,
                prompt,
                "--mode", "gather",
                "--project-root", projectRoot,
                "--recent-roots-json", recentRootsJSON,
            ]
        )
        return try JSONDecoder().decode(GatherBundle.self, from: output)
    }

    private func runRelayScript(bundle: GatherBundle) async throws -> RelayResponse {
        guard let script = Bundle.main.url(forResource: "relay", withExtension: "py") else {
            throw SomaError("relay.py not found in bundle")
        }
        let bundleJSON = (try? String(data: JSONEncoder().encode(bundle), encoding: .utf8)) ?? "{}"
        let output = try await runScript(
            path: "/opt/homebrew/bin/python3",
            args: [script.path, bundleJSON]
        )
        return try JSONDecoder().decode(RelayResponse.self, from: output)
    }

    private func runScript(path: String, args: [String]) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = args

            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = (environment["PATH"] ?? "")
                + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
                + ":/Users/daliys/.nvm/versions/node/v22.21.0/bin"
            process.environment = environment

            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr

            do {
                try process.run()
                DispatchQueue.global(qos: .userInitiated).async {
                    let outputData = stdout.fileHandleForReading.readDataToEndOfFile()
                    let errorData = stderr.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()

                    if process.terminationStatus == 0 {
                        continuation.resume(returning: outputData)
                    } else {
                        let message = String(data: errorData, encoding: .utf8) ?? "Unknown error"
                        continuation.resume(throwing: SomaError(message))
                    }
                }
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}

// MARK: - Helpers

struct SomaError: LocalizedError {
    let msg: String

    init(_ msg: String) { self.msg = msg }

    var errorDescription: String? { msg }
}

#Preview {
    ContentView()
}
