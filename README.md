# Soma: Local Evidence Compiler

Soma is a privacy-first macOS tool for preparing compact Codex-ready evidence packets from local projects. Its default path is deterministic: scan files, logs, symbols, Unity assets, and git state; then emit a budgeted packet instead of sending raw diffs, raw logs, or whole files to a model.

## Key Features

- **Evidence Compiler**: Builds concise packets with goal, known facts, git summary, selected snippets, omitted context, and next inspection guidance.
- **Token Budgets**: Supports `fast`, `balanced`, and `deep` packet tiers. The app defaults to `balanced`.
- **No Full Diff by Default**: Git changes are summarized by file list, hunk metadata, signals, and omitted raw-diff character count.
- **Repo Index Cache**: Caches deterministic file metadata, symbols, Unity references, and hashes under `~/Library/Caches/Soma/repo_index`.
- **Optional Local Model**: Uses `qwen3:4b` for Scout mode and optional local analysis, but not on the critical evidence path.
- **MCP Integration**: Scout mode can still use the official Model Context Protocol filesystem server for interactive local exploration.
- **Privacy First**: All processing happens on your machine. No telemetry, no cloud inference.

## Architecture

Soma is built with a decoupled architecture:

1. **SwiftUI Frontend**: Native macOS UI for choosing project roots, preparing packets, and copying context into Codex.
2. **Python Scout Pipeline**: `scout_pipeline.py` performs deterministic evidence gathering and packet generation.
3. **Repo Index Cache**: Stores file metadata and extracted symbols by project root for faster repeated runs.
4. **Optional Ollama Layer**: `qwen3:4b` is the default local helper. `gemma4:e4b` remains a larger fallback outside the default path.
5. **MCP Filesystem Server**: Scout mode can still use `npx @modelcontextprotocol/server-filesystem`.

## 🚀 Getting Started

### Prerequisites

- **macOS**: 13.0 or later.
- **Ollama**: Installed and running (`brew install ollama`).
- **Python 3.14+**: Installed via Homebrew (`brew install python`).
- **Node.js/npm**: Required for running MCP servers via `npx`.

### Installation

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/yourusername/Soma.git
    cd Soma
    ```

2.  **Install Python Dependencies**:
    ```bash
    pip3 install mcp --break-system-packages
    ```

3.  **Download the Default Local Model**:
    ```bash
    ollama pull qwen3:4b
    ```

4.  **Open in Xcode**:
    Open `Soma.xcodeproj`, ensure the Team/Signing is configured, and hit **Cmd + R**.

## Usage

### Preparing Codex Packets
Select a project root, describe the bug or task, and press **Prepare Packet**. Soma will:

- classify whether the prompt needs local evidence
- scan deterministic project signals
- summarize git status and diff hunks without including raw diff
- extract line-ranged snippets, symbols, Unity references, and log errors
- report estimated tokens and omitted context
- generate a packet designed to paste into Codex

### Scout Mode
Scout mode remains available for direct local model exploration through MCP. It uses `qwen3:4b` by default and should be treated as optional support, not the main debugging path.

### Benchmarking
Run the benchmark harness from the repo root:

```bash
/opt/homebrew/bin/python3 Soma/benchmark_ollama.py --model qwen3:4b
```

The important metrics are gather wall time, packet token count, omitted raw diff chars, and local model time on the compact packet.

### Tests
Run the deterministic packet regression tests:

```bash
/opt/homebrew/bin/python3 -m unittest tests/test_scout_pipeline.py
```

## 🔒 Security & Permissions

Soma operates with the following security considerations:
- **Sandboxing**: The app has App Sandbox disabled to allow execution of shell scripts and access to the local filesystem.
- **Restricted Roots**: The MCP server is explicitly restricted to `/Users/daliys/Downloads` and the project directory. It cannot wander into your system files.

## 📜 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

*Built with ❤️ by Daliys and Soma.*
