import AppKit
import Combine
import Foundation
import SwiftUI


struct ContentView: View {
    @StateObject private var ollama = OllamaManager()
    @StateObject private var viewModel = SomaViewModel()
    @State private var mode: AppMode = .relay

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
        .task {
            viewModel.hydrateProjectRootsIfNeeded()
        }
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Soma").font(.headline).bold()
                HStack(spacing: 5) {
                    Circle()
                        .fill(ollama.isOllamaRunning ? (ollama.isModelLoaded ? Color.green : Color.orange) : Color.red)
                        .frame(width: 7, height: 7)
                    Text(ollama.isOllamaRunning ? (ollama.isModelLoaded ? "Model ready" : "Ollama idle") : "Offline")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
            }
            Spacer()
            Picker("Mode", selection: $mode) {
                ForEach(AppMode.allCases) { mode in Text(mode.rawValue).tag(mode) }
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
            .onChange(of: mode) { _, _ in viewModel.resetState() }
            Spacer()
            Button(action: ollamaAction) {
                if ollama.isBusy { ProgressView().controlSize(.small).frame(width: 80) }
                else { Text(ollama.isOllamaRunning ? (ollama.isModelLoaded ? "Stop AI" : "Start AI") : "Launch").frame(width: 80) }
            }
            .buttonStyle(BorderedButtonStyle()).controlSize(.small).disabled(ollama.isBusy)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(Color(NSColor.windowBackgroundColor))
    }

    // MARK: - Scout View

    private var scoutView: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if viewModel.scoutTranscript.isEmpty {
                            emptyState(icon: "folder.badge.magnifyingglass", title: "Scout mode", subtitle: "Chat directly with qwen3:4b to explore your files")
                        } else {
                            Text(viewModel.scoutTranscript).font(.system(.body, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                        }
                        if viewModel.scoutLoading {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("Soma is scouting…").foregroundColor(.secondary).italic()
                            }.id("loading")
                        }
                    }.padding()
                }
                .background(Color(NSColor.textBackgroundColor).opacity(0.5)).cornerRadius(8).padding(.horizontal)
                .onChange(of: viewModel.scoutTranscript) { _, _ in proxy.scrollTo("loading", anchor: .bottom) }
            }
            inputBar(text: $viewModel.scoutPrompt, placeholder: "Ask Soma to find or read files…", disabled: viewModel.scoutLoading || !ollama.isOllamaRunning, buttonLabel: "Scout Files", icon: "magnifyingglass") {
                viewModel.runScout(ollama: ollama)
            }
        }
    }

    // MARK: - Relay View

    private var relayView: some View {
        VStack(spacing: 0) {
            projectRootPanel
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if viewModel.relayPhase == .idle && viewModel.gatherBundle == nil && viewModel.relayResponse == nil && viewModel.relayError == nil {
                        emptyState(icon: "doc.text.magnifyingglass", title: "Evidence compiler", subtitle: "Prepare compact Codex packets from project files, logs, and git changes.")
                    }
                    if viewModel.relayPhase == .gathering {
                        phaseCard(emoji: "📦", title: "Compiling evidence…", subtitle: "Scanning deterministic project signals, logs, symbols, and git summaries", color: .orange)
                    }
                    if viewModel.relayPhase == .relaying {
                        phaseCard(emoji: "🧠", title: "Running optional local analysis…", subtitle: "Using qwen3:4b on the compact packet only", color: .blue)
                    }
                    if let bundle = viewModel.gatherBundle, bundle.error == nil { bundlePanel(bundle) }
                    if let relay = viewModel.relayResponse { answerPanel(relay) }
                    if let relayError = viewModel.relayError { Text("⚠️ \(relayError)").foregroundColor(.red).padding() }

                    if !viewModel.activityLogs.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Button(action: { withAnimation { viewModel.showActivityLog.toggle() } }) {
                                HStack {
                                    Image(systemName: viewModel.showActivityLog ? "chevron.down" : "chevron.right")
                                    Text("📝 Activity Log (\(viewModel.activityLogs.count))").font(.subheadline.bold())
                                    Spacer()
                                    Button("Copy Log") { copyToClipboard(viewModel.activityLogs.joined(separator: "\n")) }.buttonStyle(.plain).font(.caption).foregroundColor(.blue)
                                }
                            }.buttonStyle(.plain)
                            if viewModel.showActivityLog {
                                VStack(alignment: .leading, spacing: 4) {
                                    ForEach(viewModel.activityLogs, id: \.self) { log in
                                        Text(log).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }.padding(10).background(Color.secondary.opacity(0.1)).cornerRadius(8)
                            }
                        }.padding(.top, 8)
                    }
                }.padding()
            }
            .background(Color(NSColor.textBackgroundColor).opacity(0.5)).cornerRadius(8).padding(.horizontal)
            inputBar(text: $viewModel.relayPrompt, placeholder: "Describe the bug or task; Soma will prepare a compact Codex packet", disabled: relayIsBusy, buttonLabel: "Prepare Packet", icon: "doc.text.magnifyingglass") {
                viewModel.runRelay(ollama: ollama)
            }
        }
    }

    private var projectRootPanel: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Project Root", systemImage: "folder").font(.subheadline.bold())
                Spacer()
                if !viewModel.recentProjectRoots.isEmpty {
                    Menu("Recent Roots") {
                        ForEach(viewModel.recentProjectRoots, id: \.self) { root in
                            Button(shortPath(root)) { viewModel.selectProjectRoot(root) }
                        }
                    }.controlSize(.small)
                }
                Button("Choose Folder", action: chooseProjectRoot).buttonStyle(BorderedProminentButtonStyle()).controlSize(.small)
                if !viewModel.selectedProjectRoot.isEmpty { Button("Clear", action: viewModel.clearProjectRoot).buttonStyle(BorderedButtonStyle()).controlSize(.small) }
            }
            if viewModel.selectedProjectRoot.isEmpty {
                Text("Select a project root for debugging, investigation, or log-driven packets.").font(.caption).foregroundColor(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Selected").font(.caption.bold()).foregroundColor(.secondary)
                    Text(viewModel.selectedProjectRoot).font(.caption).textSelection(.enabled)
                }
            }
            if !viewModel.recentProjectRoots.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(viewModel.recentProjectRoots, id: \.self) { root in
                            recentRootButton(root)
                        }
                    }.padding(.vertical, 2)
                }
            }
            Picker("Analysis", selection: $viewModel.analysisDepth) {
                ForEach(AnalysisDepth.allCases) { depth in
                    Text(depth.label).tag(depth)
                }
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
        }
        .padding(12).background(Color.secondary.opacity(0.06)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.secondary.opacity(0.15))).cornerRadius(10).padding(.horizontal).padding(.top)
    }

    // MARK: - Components

    private var relayIsBusy: Bool { viewModel.relayPhase == .gathering || viewModel.relayPhase == .relaying }

    private func emptyState(icon: String, title: String, subtitle: String) -> some View {
        VStack(spacing: 12) {
            Spacer(minLength: 60)
            Image(systemName: icon).font(.system(size: 44)).foregroundColor(.secondary.opacity(0.4))
            Text(title).font(.title3).bold()
            Text(subtitle).foregroundColor(.secondary).multilineTextAlignment(.center)
            Spacer()
        }.frame(maxWidth: .infinity)
    }

    private func phaseCard(emoji: String, title: String, subtitle: String, color: Color) -> some View {
        HStack(spacing: 14) {
            Text(emoji).font(.system(size: 30))
            VStack(alignment: .leading, spacing: 4) { Text(title).font(.headline); Text(subtitle).font(.caption).foregroundColor(.secondary) }
            Spacer(); ProgressView().controlSize(.regular)
        }.padding(14).background(color.opacity(0.08)).overlay(RoundedRectangle(cornerRadius: 10).stroke(color.opacity(0.25))).cornerRadius(10)
    }

    private func bundlePanel(_ bundle: GatherBundle) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: { withAnimation { viewModel.showContextPanel.toggle() } }) {
                HStack {
                    Image(systemName: viewModel.showContextPanel ? "chevron.down" : "chevron.right").foregroundColor(.secondary)
                    Text("📦 Codex Packet").font(.headline)
                    Spacer()
                    if bundle.codex_packet != nil || bundle.enriched_prompt != nil {
                        Button(action: { copyToClipboard(bundle.codex_packet ?? bundle.enriched_prompt ?? "") }) { Label("Copy Packet", systemImage: "doc.on.doc") }.buttonStyle(BorderedButtonStyle()).controlSize(.small)
                    }
                    if let summary = bundle.context_summary { Text(summary).font(.caption).foregroundColor(.secondary).lineLimit(2) }
                }
            }.buttonStyle(.plain)
            if viewModel.showContextPanel {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 10) {
                        if let routing = bundle.routing_decision { badge(text: routing.replacingOccurrences(of: "_", with: " ")) }
                        if let packetMode = bundle.packet_mode { badge(text: "mode \(packetMode)") }
                        if let analysisDepth = bundle.analysis_depth { badge(text: "depth \(analysisDepth)") }
                        if let projectType = bundle.project_type { badge(text: projectType) }
                        if let confidence = bundle.confidence { badge(text: String(format: "confidence %.2f", confidence)) }
                        if let tokenBudget = bundle.token_budget { badge(text: "budget \(tokenBudget)") }
                        if let estimatedTokens = bundle.estimated_tokens { badge(text: "~\(estimatedTokens) tokens") }
                    }
                    if let reason = bundle.gather_reason { labeledBlock(title: "Why This Route", text: reason) }
                    if let root = bundle.project_root { labeledBlock(title: "Project Root", text: root) }
                    if let gitStatus = bundle.git_status, !gitStatus.isEmpty { labeledBlock(title: "Git Status", text: gitStatus) }
                    if let diffSummary = bundle.git_diff_summary {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Git Diff Summary").font(.subheadline).bold()
                            diffSummaryView(diffSummary)
                        }
                    }
                    if let repoIndex = bundle.repo_index {
                        labeledBlock(title: "Repo Index", text: repoIndexSummary(repoIndex))
                    }
                    if let stages = bundle.analysis_stages, !stages.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Analysis Stages").font(.subheadline).bold()
                            ForEach(Array(stages.enumerated()), id: \.offset) { _, stage in
                                Text(stageSummary(stage)).font(.caption).foregroundColor(.secondary)
                            }
                        }
                    }
                    if let assumptions = bundle.assumptions, !assumptions.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Assumptions").font(.subheadline).bold()
                            ForEach(assumptions, id: \.self) { item in Text("• \(item)").font(.caption).foregroundColor(.secondary) }
                        }
                    }
                    if let evidence = bundle.evidence_items, !evidence.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Evidence (\(evidence.count))").font(.subheadline).bold()
                            ForEach(Array(evidence.enumerated()), id: \.offset) { _, item in evidenceRow(item) }
                        }
                    }
                    if let errors = bundle.error_lines, !errors.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Detected Errors (\(errors.count))").font(.subheadline).bold()
                            ForEach(errors.prefix(6), id: \.self) { line in Text(line).font(.system(.caption, design: .monospaced)).foregroundColor(.red) }
                        }
                    }
                    if let omitted = bundle.omitted_context, !omitted.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Omitted Context").font(.subheadline).bold()
                            ForEach(omitted.keys.sorted(), id: \.self) { key in
                                Text("\(key): \(omitted[key]?.displayValue ?? "")").font(.caption).foregroundColor(.secondary)
                            }
                        }
                    }
                    if let packet = bundle.codex_packet ?? bundle.enriched_prompt, !packet.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Packet Preview").font(.subheadline).bold()
                            Text(packet).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary).lineLimit(28).textSelection(.enabled).padding(8).background(Color(NSColor.textBackgroundColor)).cornerRadius(8)
                        }
                    }
                }.padding(10).background(Color(NSColor.controlBackgroundColor)).cornerRadius(8)
            }
        }.padding(12).background(Color.green.opacity(0.06)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.green.opacity(0.2))).cornerRadius(10)
    }

    private func evidenceRow(_ item: EvidenceItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) { Text(item.kind?.uppercased() ?? "FILE").font(.caption2.bold()).foregroundColor(.secondary); Text(URL(fileURLWithPath: item.path ?? "").lastPathComponent).font(.caption.bold()) }
            Text(item.path ?? "").font(.caption2).foregroundColor(.secondary).textSelection(.enabled)
            if let startLine = item.start_line {
                Text("Lines \(startLine)\(item.end_line.map { "-\($0)" } ?? "")").font(.caption2).foregroundColor(.secondary)
            }
            if let reason = item.reason { Text(reason).font(.caption) }
            if let symbols = item.symbols, !symbols.isEmpty {
                Text("Symbols: \(symbols.prefix(8).joined(separator: ", "))").font(.caption2).foregroundColor(.secondary)
            }
            if let refs = item.unity_refs, !refs.isEmpty {
                Text("Unity refs: \(refs.prefix(5).joined(separator: ", "))").font(.caption2).foregroundColor(.secondary)
            }
            if let preview = item.preview, !preview.isEmpty { Text(preview).font(.system(.caption, design: .monospaced)).foregroundColor(.secondary).lineLimit(8) }
        }.padding(8).frame(maxWidth: .infinity, alignment: .leading).background(Color(NSColor.textBackgroundColor)).cornerRadius(8)
    }

    private func answerPanel(_ relay: RelayResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("🧠 Local model says").font(.headline); Spacer()
                if let routing = relay.routing_decision { badge(text: routing.replacingOccurrences(of: "_", with: " ")) }
                if let model = relay.model { Label(model, systemImage: "cpu.fill").font(.caption).foregroundColor(.secondary) }
                else if let source = relay.source { Label(source, systemImage: "bolt.fill").font(.caption).foregroundColor(.secondary) }
            }
            Divider()
            if let response = relay.response { Text(response).font(.body).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
            if let error = relay.error { Text("Error: \(error)").foregroundColor(.red) }
        }.padding(14).background(Color.blue.opacity(0.06)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.blue.opacity(0.2))).cornerRadius(10)
    }

    private func diffSummaryView(_ summary: GitDiffSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Changed files: \(summary.changed_file_count ?? summary.changed_files?.count ?? 0), raw diff omitted: \(summary.raw_diff_chars_omitted ?? 0) chars")
                .font(.caption)
                .foregroundColor(.secondary)
            if let files = summary.changed_files, !files.isEmpty {
                ForEach(Array(files.prefix(8).enumerated()), id: \.offset) { _, file in
                    Text("- \(file.status ?? "?") \(file.path ?? "") \(file.added.map { "+\($0)" } ?? "")\(file.removed.map { "/-\($0)" } ?? "")")
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            }
            if let hunks = summary.hunks, !hunks.isEmpty {
                Divider()
                ForEach(Array(hunks.prefix(6).enumerated()), id: \.offset) { index, hunk in
                    Text("\(index + 1). \(hunk.file ?? "[unknown]")\(hunk.start_line.map { ":\($0)" } ?? "") (+\(hunk.added ?? 0)/-\(hunk.removed ?? 0))")
                        .font(.system(.caption2, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            }
        }
        .padding(8)
        .background(Color(NSColor.textBackgroundColor))
        .cornerRadius(8)
    }

    private func badge(text: String) -> some View {
        Text(text).font(.caption2.bold()).padding(.horizontal, 8).padding(.vertical, 4).background(Color.secondary.opacity(0.12)).cornerRadius(999)
    }

    @ViewBuilder
    private func recentRootButton(_ root: String) -> some View {
        if root == viewModel.selectedProjectRoot {
            Button(action: { viewModel.selectProjectRoot(root) }) { Text(shortPath(root)).lineLimit(1) }
                .buttonStyle(BorderedProminentButtonStyle())
                .controlSize(.small)
        } else {
            Button(action: { viewModel.selectProjectRoot(root) }) { Text(shortPath(root)).lineLimit(1) }
                .buttonStyle(BorderedButtonStyle())
                .controlSize(.small)
        }
    }

    private func labeledBlock(title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) { Text(title).font(.subheadline).bold(); Text(text).font(.caption).foregroundColor(.secondary).textSelection(.enabled) }
    }

    private func repoIndexSummary(_ index: RepoIndexSummary) -> String {
        [
            "Cache: \(index.cache_path ?? "[none]")",
            "Indexed files: \(index.indexed_file_count ?? 0)",
            "Changed index entries: \(index.changed_index_entries ?? 0)",
        ].joined(separator: "\n")
    }

    private func stageSummary(_ stage: AnalysisStage) -> String {
        var parts = [stage.stage ?? "stage", stage.status ?? "unknown"]
        if let model = stage.model { parts.append(model) }
        if let error = stage.error, !error.isEmpty { parts.append("error: \(error)") }
        if let notes = stage.notes, !notes.isEmpty { parts.append(notes.prefix(2).joined(separator: "; ")) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func inputBar(text: Binding<String>, placeholder: String, disabled: Bool, buttonLabel: String, icon: String, action: @escaping () -> Void) -> some View {
        VStack(spacing: 8) {
            ZStack(alignment: .topLeading) {
                if text.wrappedValue.isEmpty { Text(placeholder).foregroundColor(.secondary).padding(.leading, 5).padding(.top, 8).font(.body).allowsHitTesting(false) }
                TextEditor(text: text).font(.body).frame(minHeight: 60, maxHeight: 100).padding(4).background(Color.clear).onSubmit { if !disabled { action() } }
            }.background(Color(NSColor.controlBackgroundColor)).cornerRadius(6).overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.gray.opacity(0.2)))
            HStack {
                if mode == .relay { Button("Clear", action: viewModel.resetState).buttonStyle(BorderedButtonStyle()).controlSize(.small) }
                Spacer()
                Button(action: action) { HStack { Image(systemName: icon); Text(buttonLabel) }.bold().padding(.horizontal, 8) }.buttonStyle(BorderedProminentButtonStyle()).controlSize(.regular).disabled(disabled || text.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty).keyboardShortcut(.return, modifiers: .command)
            }
        }.padding()
    }

    private func ollamaAction() { if !ollama.isOllamaRunning { ollama.launchOllama() } else if ollama.isModelLoaded { ollama.stopModel() } else { ollama.startModel() } }
    private func chooseProjectRoot() {
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false; panel.prompt = "Choose Project Root"
        guard panel.runModal() == .OK, let path = panel.url?.path else { return }
        viewModel.selectProjectRoot(path)
    }
    private func shortPath(_ path: String) -> String {
        let home = NSHomeDirectory()
        if path == home { return "~" }
        if path.hasPrefix(home + "/") { return "~/" + path.dropFirst(home.count + 1) }
        return path
    }
    private func copyToClipboard(_ text: String) { let pb = NSPasteboard.general; pb.clearContents(); pb.setString(text, forType: .string) }
}


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
    let packet_mode: String?
    let analysis_depth: String?
    let analysis_stages: [AnalysisStage]?
    let preflight: [String: AnyCodable]?
    let model_analysis: [String: AnyCodable]?
    let gather_reason: String?
    let confidence: Double?
    let git_status: String?
    let git_diff: String?
    let git_diff_summary: GitDiffSummary?
    let repo_index: RepoIndexSummary?
    let gathered_files: [String: GatheredFile]?
    let evidence_items: [EvidenceItem]?
    let error_lines: [String]?
    let context_summary: String?
    let open_questions: [String]?
    let assumptions: [String]?
    let token_budget: String?
    let estimated_tokens: Int?
    let omitted_context: [String: AnyCodable]?
    let codex_packet: String?
    let enriched_prompt: String?
    let error: String?
}

struct AnalysisStage: Codable, Sendable, Hashable {
    let stage: String?
    let model: String?
    let status: String?
    let error: String?
    let notes: [String]?
}

struct GitDiffSummary: Codable, Sendable, Hashable {
    let changed_files: [GitChangedFile]?
    let changed_file_count: Int?
    let hunks: [GitHunk]?
    let raw_diff_chars_omitted: Int?
}

struct GitChangedFile: Codable, Sendable, Hashable {
    let status: String?
    let path: String?
    let added: String?
    let removed: String?
}

struct GitHunk: Codable, Sendable, Hashable {
    let file: String?
    let start_line: Int?
    let end_line: Int?
    let added: Int?
    let removed: Int?
    let signals: [String]?
}

struct RepoIndexSummary: Codable, Sendable, Hashable {
    let cache_path: String?
    let indexed_file_count: Int?
    let changed_index_entries: Int?
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
    let start_line: Int?
    let end_line: Int?
    let symbols: [String]?
    let unity_refs: [String]?
}

struct RelayResponse: Codable, Sendable {
    let response: String?
    let source: String?
    let model: String?
    let routing_decision: String?
    let enriched_prompt: String?
    let codex_packet: String?
    let estimated_tokens: Int?
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

extension AnyCodable {
    var displayValue: String {
        if let value = value as? String { return value }
        if let value = value as? Int { return String(value) }
        if let value = value as? Double { return String(format: "%.2f", value) }
        if let value = value as? Bool { return value ? "true" : "false" }
        return String(describing: value)
    }
}

enum AppMode: String, CaseIterable, Identifiable {
    case scout = "🐶  Scout"
    case relay = "🔗  Relay"

    var id: String { rawValue }
}

enum AnalysisDepth: String, CaseIterable, Identifiable, Codable {
    case deterministic
    case ranked
    case analyst

    var id: String { rawValue }
    var label: String {
        switch self {
        case .deterministic: return "Deterministic"
        case .ranked: return "Ranker"
        case .analyst: return "Analyst"
        }
    }
}

enum RelayPhase: Equatable {
    case idle
    case gathering
    case relaying
    case done
    case failed(String)
}

struct SomaError: LocalizedError {
    let msg: String
    init(_ msg: String) { self.msg = msg }
    var errorDescription: String? { msg }
}

final class OllamaManager: ObservableObject {
    @Published var isModelLoaded = false
    @Published var isOllamaRunning = false
    @Published var isBusy = false

    let modelName = "qwen3:4b"
    private var timer: Timer?

    init() { startPolling() }

    func startPolling() {
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.checkStatus()
        }
        checkStatus()
    }

    func checkStatus() {
        guard let url = URL(string: "http://127.0.0.1:11434/api/ps") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2

        URLSession.shared.dataTask(with: request) { data, _, error in
            DispatchQueue.main.async {
                if error != nil {
                    self.updateStatus(isRunning: false, isLoaded: false)
                    return
                }

                var isLoaded = false
                if
                    let data,
                    let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    let models = json["models"] as? [[String: Any]]
                {
                    isLoaded = models.contains {
                        ($0["name"] as? String)?.lowercased().hasPrefix(self.modelName.lowercased()) == true
                    }
                }
                self.updateStatus(isRunning: true, isLoaded: isLoaded)
            }
        }.resume()
    }

    private func updateStatus(isRunning: Bool, isLoaded: Bool) {
        if isOllamaRunning != isRunning {
            isOllamaRunning = isRunning
        }
        if isModelLoaded != isLoaded {
            isModelLoaded = isLoaded
        }
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
        guard let url = URL(string: "http://127.0.0.1:11434/api/generate") else { return }
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

@MainActor
final class SomaViewModel: ObservableObject {
    private let lastProjectRootKey = "relay.lastProjectRoot"
    private let recentProjectRootsKey = "relay.recentProjectRoots"
    private var hasHydratedProjectRoots = false

    @Published var scoutPrompt = ""
    @Published var scoutTranscript = ""
    @Published var scoutHistory: [[String: AnyCodable]] = []
    @Published var scoutLoading = false

    @Published var relayPrompt = ""
    @Published var relayPhase: RelayPhase = .idle
    @Published var gatherBundle: GatherBundle?
    @Published var relayResponse: RelayResponse?
    @Published var showContextPanel = false
    @Published var relayError: String?
    @Published var selectedProjectRoot = ""
    @Published var recentProjectRoots: [String] = []
    @Published var analysisDepth: AnalysisDepth = .deterministic

    @Published var activityLogs: [String] = []
    @Published var showActivityLog = false

    init() {}

    func resetState() {
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
        activityLogs = []
    }

    func selectProjectRoot(_ path: String) {
        guard let normalized = validatedDirectoryPath(path) else { return }
        selectedProjectRoot = normalized
        recentProjectRoots = deduplicatedRoots([normalized] + recentProjectRoots).prefix(6).map(\.self)
        persistProjectRoots()
    }

    func clearProjectRoot() {
        selectedProjectRoot = ""
        UserDefaults.standard.set("", forKey: lastProjectRootKey)
    }

    func hydrateProjectRootsIfNeeded() {
        guard !hasHydratedProjectRoots else { return }
        hasHydratedProjectRoots = true

        recentProjectRoots = decodeRecentRoots()
        let storedLastProjectRoot = UserDefaults.standard.string(forKey: lastProjectRootKey) ?? ""
        if selectedProjectRoot.isEmpty, let restored = validatedDirectoryPath(storedLastProjectRoot) {
            selectedProjectRoot = restored
        }
        if !selectedProjectRoot.isEmpty {
            recentProjectRoots = deduplicatedRoots([selectedProjectRoot] + recentProjectRoots).prefix(6).map(\.self)
        }
        persistProjectRoots()
    }

    private func persistProjectRoots() {
        UserDefaults.standard.set(selectedProjectRoot, forKey: lastProjectRootKey)
        UserDefaults.standard.set(encodeRecentRoots(recentProjectRoots), forKey: recentProjectRootsKey)
    }

    private func decodeRecentRoots() -> [String] {
        let storedRecentRootsJSON = UserDefaults.standard.string(forKey: recentProjectRootsKey) ?? "[]"
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

    func logActivity(_ message: String, duration: Double? = nil) {
        let timestamp = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        var log = "[\(timestamp)] \(message)"
        if let duration = duration {
            log += String(format: " (%.2fs)", duration)
        }
        activityLogs.append(log)
    }

    func runScout(ollama: OllamaManager) {
        let prompt = scoutPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }

        scoutLoading = true
        scoutTranscript += "\n> \(prompt)\n\n"
        scoutPrompt = ""
        logActivity("Starting Scout: \(prompt)")
        let startTime = Date()

        Task {
            do {
                logActivity("Calling scout_pipeline.py...")
                let stepStart = Date()
                let result = try await runPythonChat(prompt: prompt, history: scoutHistory)
                let stepDuration = Date().timeIntervalSince(stepStart)

                await MainActor.run {
                    logActivity("Received response from qwen3:4b", duration: stepDuration)
                    scoutTranscript += (result.response ?? "") + "\n"
                    scoutHistory = result.history ?? []
                    scoutLoading = false
                    ollama.checkStatus()
                    logActivity("Scout total time", duration: Date().timeIntervalSince(startTime))
                }
            } catch {
                await MainActor.run {
                    logActivity("Scout failed: \(error.localizedDescription)")
                    scoutTranscript += "⚠️ Error: \(error.localizedDescription)\n"
                    scoutLoading = false
                }
            }
        }
    }

    func runRelay(ollama: OllamaManager) {
        let prompt = relayPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }

        relayPrompt = ""
        gatherBundle = nil
        relayResponse = nil
        relayError = nil
        showContextPanel = false
        activityLogs = []
        logActivity("Starting Relay: \(prompt)")
        let startTime = Date()

        Task {
            do {
                relayPhase = .gathering
                let rootLabel = selectedProjectRoot.isEmpty ? "no selected root" : selectedProjectRoot
                logActivity("Preparing packet via Python router (\(rootLabel))...")
                let stepStart = Date()
                let bundle = try await runGather(
                    prompt: prompt,
                    projectRoot: selectedProjectRoot,
                    recentRoots: recentProjectRoots
                )
                let stepDuration = Date().timeIntervalSince(stepStart)

                if let error = bundle.error {
                    throw SomaError(error)
                }
                logActivity("Prepared \(bundle.packet_mode ?? "unknown") packet with \(bundle.evidence_items?.count ?? 0) items. Confidence: \(bundle.confidence ?? 0)", duration: stepDuration)

                await MainActor.run {
                    gatherBundle = bundle
                    showContextPanel = true
                    relayPhase = .done
                    ollama.checkStatus()
                    logActivity("Prepared Codex packet (~\(bundle.estimated_tokens ?? 0) tokens)")
                    logActivity("Evidence compile total time", duration: Date().timeIntervalSince(startTime))
                }
            } catch {
                await MainActor.run {
                    logActivity("Relay failed: \(error.localizedDescription)")
                    relayPhase = .failed(error.localizedDescription)
                    relayError = error.localizedDescription
                }
            }
        }
    }

    // MARK: Script runners

    private func runPythonChat(prompt: String, history: [[String: AnyCodable]]) async throws -> OllamaResponse {
        guard let script = Bundle.main.url(forResource: "scout_pipeline", withExtension: "py") else {
            throw SomaError("scout_pipeline.py not found in bundle")
        }
        let historyJSON = (try? String(data: JSONEncoder().encode(history), encoding: .utf8)) ?? "[]"
        let output = try await runScript(path: "/opt/homebrew/bin/python3", args: [script.path, prompt, historyJSON])
        return try JSONDecoder().decode(OllamaResponse.self, from: output)
    }

    private func runGather(prompt: String, projectRoot: String, recentRoots: [String]) async throws -> GatherBundle {
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
                "--token-budget", "balanced",
                "--analysis-depth", analysisDepth.rawValue,
            ]
        )
        return try JSONDecoder().decode(GatherBundle.self, from: output)
    }

    private func runRelayScript(bundle: GatherBundle) async throws -> RelayResponse {
        guard let script = Bundle.main.url(forResource: "relay", withExtension: "py") else {
            throw SomaError("relay.py not found in bundle")
        }
        let bundleJSON = (try? String(data: JSONEncoder().encode(bundle), encoding: .utf8)) ?? "{}"
        let output = try await runScript(path: "/opt/homebrew/bin/python3", args: [script.path, bundleJSON])
        return try JSONDecoder().decode(RelayResponse.self, from: output)
    }

    private func runScript(path: String, args: [String]) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = args
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = (environment["PATH"] ?? "") + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/Users/daliys/.nvm/versions/node/v22.21.0/bin"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            let stdout = Pipe(), stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr
            do {
                try process.run()
                DispatchQueue.global(qos: .userInitiated).async {
                    let outputData = stdout.fileHandleForReading.readDataToEndOfFile()
                    let errorData = stderr.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if process.terminationStatus == 0 { continuation.resume(returning: outputData) }
                    else { continuation.resume(throwing: SomaError(String(data: errorData, encoding: .utf8) ?? "Unknown error")) }
                }
            } catch { continuation.resume(throwing: error) }
        }
    }
}
