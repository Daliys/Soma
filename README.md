# Soma: The Local AI Scout 🕵️‍♂️

Soma is a high-performance, privacy-first macOS application that orchestrates local LLMs (via Ollama) and the Model Context Protocol (MCP) to provide a seamless "AI Scout" experience. Unlike traditional chat interfaces, Soma has "eyes" on your local filesystem, allowing it to explore, read, and summarize your files without ever sending data to the cloud.

## 🌟 Key Features

- **Local Intelligence**: Powered by `llama3.2:3b` running natively via Ollama.
- **MCP Integration**: Uses the official Model Context Protocol to securely access local directories.
- **Privacy First**: All processing happens on your machine. No telemetry, no cloud inference.
- **Continuous Chat**: Maintains context and history for long-running investigations.
- **Intelligent Path Correction**: Automatically resolves relative file names to absolute paths within allowed roots.
- **Binary File Handling**: Capable of scanning through `.docx`, `.pdf`, and other binary formats to extract readable text.

## 🏗 Architecture

Soma is built with a decoupled architecture:

1.  **SwiftUI Frontend**: A modern, native macOS app that manages the chat UI, Ollama process monitoring, and asynchronous script execution.
2.  **Python Scout Pipeline**: A sophisticated backend script (`scout_pipeline.py`) that acts as an **MCP Client**.
3.  **MCP Filesystem Server**: Orchestrated via `npx`, providing a standardized interface for file operations.
4.  **Ollama API**: Handles the local inference and tool-calling logic for Llama 3.2.

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

3.  **Download the Model**:
    ```bash
    ollama pull llama3.2:3b
    ```

4.  **Open in Xcode**:
    Open `Soma.xcodeproj`, ensure the Team/Signing is configured, and hit **Cmd + R**.

## 🛠 Usage

### Scouting Files
You can ask Soma to find or read files in your `Downloads` or `Project` folders:
- *"List the files in my Downloads folder."*
- *"Find 'Contract.docx' and tell me what the expiration date is."*
- *"Search the project folder for any Python scripts and summarize them."*

### Ollama Management
The UI includes a status monitor for Ollama:
- **Green**: Ollama is running and the model is loaded in memory.
- **Orange**: Ollama is running, but the model is idle/unloaded.
- **Red**: Ollama is offline.
- Use the **Start/Stop AI** buttons to manage your system resources.

## 🔒 Security & Permissions

Soma operates with the following security considerations:
- **Sandboxing**: The app has App Sandbox disabled to allow execution of shell scripts and access to the local filesystem.
- **Restricted Roots**: The MCP server is explicitly restricted to `/Users/daliys/Downloads` and the project directory. It cannot wander into your system files.

## 📜 License

This project is licensed under the MIT License - see the LICENSE file for details.

---

*Built with ❤️ by Daliys and Soma.*
