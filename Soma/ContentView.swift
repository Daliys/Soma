//
//  ContentView.swift
//  Soma
//
//  Created by Gemini CLI on 18/04/2026.
//

import SwiftUI
import Combine

// --- Codable Models for MCP & Ollama ---

struct ChatMessage: Codable, Sendable {
    let role: String
    let content: String?
    // tool_calls are handled internally by the Python script, 
    // but we might receive them in history. For simplicity, we just pass raw dictionaries back.
}

struct OllamaResponse: Codable, Sendable {
    let response: String?
    let history: [[String: AnyCodable]]?
    let error: String?
    
    // Custom coding keys or decoding for AnyCodable might be needed if history is complex
}

// Simple wrapper for mixed JSON types in the history array
struct AnyCodable: Codable, Sendable {
    let value: Any
    
    init(_ value: Any) { self.value = value }
    
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let x = try? container.decode(String.self) { value = x }
        else if let x = try? container.decode(Int.self) { value = x }
        else if let x = try? container.decode(Double.self) { value = x }
        else if let x = try? container.decode(Bool.self) { value = x }
        else if let x = try? container.decode([String: AnyCodable].self) { value = x }
        else if let x = try? container.decode([AnyCodable].self) { value = x }
        else { throw DecodingError.dataCorruptedError(in: container, debugDescription: "Wrong type") }
    }
    
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let x = value as? String { try container.encode(x) }
        else if let x = value as? Int { try container.encode(x) }
        else if let x = value as? Double { try container.encode(x) }
        else if let x = value as? Bool { try container.encode(x) }
        else if let x = value as? [String: AnyCodable] { try container.encode(x) }
        else if let x = value as? [AnyCodable] { try container.encode(x) }
    }
}

// --- Manager for Ollama Status ---

class OllamaManager: ObservableObject {
    @Published var isModelLoaded: Bool = false
    @Published var isOllamaRunning: Bool = false
    @Published var isBusy: Bool = false
    
    let modelName = "llama3.2:3b"
    private var timer: Timer?
    
    init() { startPolling() }
    
    func startPolling() {
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in self?.checkStatus() }
        checkStatus()
    }
    
    func checkStatus() {
        guard let url = URL(string: "http://localhost:11434/api/ps") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        
        URLSession.shared.dataTask(with: request) { data, _, error in
            DispatchQueue.main.async {
                if error != nil {
                    self.isOllamaRunning = false
                    self.isModelLoaded = false
                    return
                }
                guard let data = data else { return }
                self.isOllamaRunning = true
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let models = json["models"] as? [[String: Any]] {
                    self.isModelLoaded = models.contains { ($0["name"] as? String)?.lowercased().hasPrefix(self.modelName.lowercased()) == true }
                }
            }
        }.resume()
    }
    
    func launchOllamaApp() {
        isBusy = true
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ollama")
            process.arguments = ["serve"]
            try? process.run()
            Thread.sleep(forTimeInterval: 4.0)
            DispatchQueue.main.async {
                self.isBusy = false
                self.checkStatus()
            }
        }
    }
    
    func startModel() { sendGenerateRequest(keepAlive: -1) }
    func stopModel() { sendGenerateRequest(keepAlive: 0) }
    
    private func sendGenerateRequest(keepAlive: Int) {
        guard let url = URL(string: "http://localhost:11434/api/generate") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: Any] = ["model": modelName, "prompt": "", "keep_alive": keepAlive, "stream": false]
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        DispatchQueue.main.async { self.isBusy = true }
        URLSession.shared.dataTask(with: request) { _, _, _ in
            DispatchQueue.main.async {
                self.isBusy = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { self.checkStatus() }
            }
        }.resume()
    }
}

// --- Main View ---

struct ContentView: View {
    @StateObject private var ollamaManager = OllamaManager()
    @State private var prompt: String = ""
    @State private var chatTranscript: String = ""
    @State private var history: [[String: AnyCodable]] = [] // Conversation history
    @State private var isLoading: Bool = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 5) {
                Text("Soma Scout Orchestrator")
                    .font(.title2).bold()
                
                HStack {
                    Circle()
                        .fill(ollamaManager.isOllamaRunning ? (ollamaManager.isModelLoaded ? Color.green : Color.orange) : Color.red)
                        .frame(width: 8, height: 8)
                    Text("AI: \(ollamaManager.modelName)").font(.caption).bold()
                    Text(ollamaManager.isOllamaRunning ? (ollamaManager.isModelLoaded ? "(Running)" : "(Idle)") : "(Offline)").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button(action: { history = []; chatTranscript = ""; errorMessage = nil }) {
                        Image(systemName: "trash")
                        Text("Clear Chat")
                    }.buttonStyle(.bordered).controlSize(.small).disabled(chatTranscript.isEmpty)
                    
                    Divider().frame(height: 15).padding(.horizontal, 4)
                    
                    Button(action: {
                        if !ollamaManager.isOllamaRunning { ollamaManager.launchOllamaApp() }
                        else if ollamaManager.isModelLoaded { ollamaManager.stopModel() }
                        else { ollamaManager.startModel() }
                    }) {
                        if ollamaManager.isBusy { ProgressView().controlSize(.small).frame(width: 90) }
                        else { Text(ollamaManager.isOllamaRunning ? (ollamaManager.isModelLoaded ? "Stop AI" : "Start AI") : "Launch Ollama").frame(width: 90) }
                    }.buttonStyle(.bordered).controlSize(.small).disabled(ollamaManager.isBusy)
                }
                .padding(.horizontal).padding(.vertical, 8)
                .background(Color.gray.opacity(0.1)).cornerRadius(8)
            }.padding()

            // Chat Window
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if chatTranscript.isEmpty {
                            VStack {
                                Spacer(minLength: 50)
                                Image(systemName: "folder.badge.person.crop").font(.system(size: 40)).foregroundColor(.secondary.opacity(0.5))
                                Text("Soma is ready to scout your files").foregroundColor(.secondary).italic()
                                Spacer()
                            }.frame(maxWidth: .infinity)
                        } else {
                            Text(chatTranscript).font(.system(.body, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                        }
                        if isLoading {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("Soma is scouting...").foregroundColor(.secondary).italic()
                            }.id("loadingIndicator")
                        }
                    }.padding()
                }
                .background(Color(NSColor.textBackgroundColor).opacity(0.5)).cornerRadius(8).padding(.horizontal)
                .onChange(of: chatTranscript) { _, _ in proxy.scrollTo("loadingIndicator", anchor: .bottom) }
            }

            // Input
            VStack(spacing: 8) {
                TextEditor(text: $prompt).font(.body).frame(minHeight: 60, maxHeight: 100).padding(4)
                    .background(Color(NSColor.controlBackgroundColor)).cornerRadius(4).overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.gray.opacity(0.2)))
                
                Button(action: runPipeline) {
                    HStack { Image(systemName: "magnifyingglass"); Text("Scout Files") }.bold().frame(maxWidth: .infinity).padding(.vertical, 6)
                }.buttonStyle(.borderedProminent).disabled(isLoading || prompt.trimmingCharacters(in: .whitespaces).isEmpty || !ollamaManager.isOllamaRunning)
                
                if let error = errorMessage { Text(error).font(.caption).foregroundColor(.red) }
            }.padding()
        }.frame(minWidth: 550, minHeight: 700)
    }

    private func runPipeline() {
        let currentPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !currentPrompt.isEmpty else { return }
        isLoading = true
        errorMessage = nil
        chatTranscript += "\n> \(currentPrompt)\n\n"
        prompt = ""

        Task {
            do {
                let result = try await executePythonScript(with: currentPrompt, history: history)
                await MainActor.run {
                    self.chatTranscript += (result.response ?? "") + "\n"
                    self.history = result.history ?? []
                    self.isLoading = false
                    self.ollamaManager.checkStatus()
                }
            } catch {
                await MainActor.run {
                    self.errorMessage = "Error: \(error.localizedDescription)"
                    self.isLoading = false
                }
            }
        }
    }

    nonisolated private func executePythonScript(with prompt: String, history: [[String: AnyCodable]]) async throws -> OllamaResponse {
        guard let scriptURL = Bundle.main.url(forResource: "scout_pipeline", withExtension: "py") else {
            throw NSError(domain: "SomaError", code: 404, userInfo: [NSLocalizedDescriptionKey: "scout_pipeline.py not found"])
        }
        let historyJson = try String(data: JSONEncoder().encode(history), encoding: .utf8) ?? "[]"

        return try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            // Explicitly use Homebrew Python where we installed 'mcp'
            process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/python3")
            process.arguments = [scriptURL.path, prompt, historyJson]
            let pipe = Pipe()
            let errorPipe = Pipe()
            process.standardOutput = pipe
            process.standardError = errorPipe
            
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = (env["PATH"] ?? "") + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/Users/daliys/.nvm/versions/node/v22.21.0/bin"
            process.environment = env

            do {
                try process.run()
                DispatchQueue.global(qos: .userInitiated).async {
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    let errorData = errorPipe.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if process.terminationStatus == 0 {
                        do {
                            let result = try JSONDecoder().decode(OllamaResponse.self, from: data)
                            continuation.resume(returning: result)
                        } catch {
                            continuation.resume(throwing: error)
                        }
                    } else {
                        let errorOutput = String(data: errorData, encoding: .utf8) ?? "Unknown Error"
                        continuation.resume(throwing: NSError(domain: "SomaError", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: errorOutput]))
                    }
                }
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}

#Preview { ContentView() }
