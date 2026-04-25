#!/usr/bin/env python3
"""
Soma Scout Pipeline
  --mode chat   (default) Interactive chat with Llama via MCP filesystem tools
  --mode gather            Intent-gated evidence gathering for Relay mode
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# -- Config -----------------------------------------------------------------
MODEL = os.environ.get("SOMA_LOCAL_MODEL", "qwen3:4b")
RANKER_MODEL = os.environ.get("SOMA_RANKER_MODEL", "qwen3:4b")
ANALYST_MODEL = os.environ.get("SOMA_ANALYST_MODEL", "gemma4:e4b")
CHAT_ALLOWED_DIRS = [
    path for path in [
        "/Users/daliys",
        "/Users/daliys/Downloads",
        "/Users/daliys/Daliys",
        "/Users/daliys/Library/Logs",
    ]
    if os.path.exists(path)
] or ["/Users/daliys"]

MAX_ERROR_LINES = 20
MAX_EVIDENCE_ITEMS = 8
MAX_DISCOVERED_FILES = 1500
MAX_FILE_BYTES = 160_000
MAX_PREVIEW_CHARS = 1_400
OLLAMA_SUMMARY_TIMEOUT = 45
DEFAULT_TOKEN_BUDGET = "balanced"
TOKEN_BUDGETS = {
    "fast": 2_500,
    "balanced": 6_000,
    "deep": 20_000,
}
ANALYSIS_DEPTHS = {"deterministic", "ranked", "analyst"}
CODEX_PACKET_TARGET_TOKENS = TOKEN_BUDGETS[DEFAULT_TOKEN_BUDGET]
DEFAULT_REPO_CACHE_DIR = Path.home() / "Library" / "Caches" / "Soma" / "repo_index"
NOISE_PATH_NAMES = {".DS_Store"}
NOISE_SUFFIXES = {".pyc", ".pyo"}

SKIP_DIRS = {
    ".git",
    ".build",
    ".idea",
    ".venv",
    "Assets.xcassets",
    "DerivedData",
    "Pods",
    "build",
    "dist",
    "node_modules",
    "venv",
    "xcuserdata",
    "__pycache__",
}

MANIFEST_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "setup.py",
    "setup.cfg",
    "Package.swift",
    "Podfile",
    "Cartfile",
    "Gemfile",
    "Makefile",
    "Dockerfile",
    ".env",
}

CONFIG_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".plist",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}

UNITY_EXTENSIONS = {
    ".asmdef",
    ".asset",
    ".controller",
    ".mat",
    ".meta",
    ".prefab",
    ".unity",
}

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".m",
    ".mm",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".zsh",
}

SCRIPT_EXTENSIONS = {".bat", ".command", ".ps1", ".py", ".rb", ".sh", ".zsh"}
LOG_EXTENSIONS = {".crash", ".err", ".log", ".out", ".stderr", ".stdout", ".trace"}
TEXT_EXTENSIONS = SOURCE_EXTENSIONS | CONFIG_EXTENSIONS | LOG_EXTENSIONS | UNITY_EXTENSIONS | {".md", ".txt"}

DEBUG_KEYWORDS = {
    "bug",
    "broken",
    "build",
    "config",
    "crash",
    "debug",
    "diagnose",
    "doesn't work",
    "doesnt work",
    "error",
    "exception",
    "fail",
    "failing",
    "failure",
    "issue",
    "log",
    "not work",
    "problem",
    "script",
    "stack trace",
    "traceback",
    "git",
    "status",
    "diff",
    "changes",
    "change",
    "changed",
    "changet",
    "modified",
    "recent",
    "last",
    "review",
    "regression",
    "unity",
}

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "but",
    "does",
    "for",
    "from",
    "how",
    "i",
    "is",
    "it",
    "my",
    "not",
    "of",
    "on",
    "please",
    "script",
    "that",
    "the",
    "this",
    "to",
    "what",
    "why",
    "with",
    "work",
}

# -- System prompts ----------------------------------------------------------
CHAT_SYSTEM = """You are Soma, a highly capable local AI scout with full access to the filesystem.
- list_directory: explore folders   - read_file: read files
- Output tool calls as valid JSON in a code block, e.g.:
```json
{"name": "list_directory", "arguments": {"path": "/Users/daliys"}}
```"""

OLLAMA_SUMMARY_SYSTEM = """You summarize pre-gathered debugging evidence for a larger model.
Return JSON only with this exact shape:
{
  "summary": "one or two sentences",
  "assumptions": ["..."],
  "open_questions": ["..."],
  "confidence": 0.0
}

Rules:
- Use only the provided evidence.
- Keep assumptions and open_questions concise.
- confidence must be between 0 and 1.
"""


# -- Chat helpers ------------------------------------------------------------
def extract_tool_calls(content):
    tool_calls = []
    for match in re.finditer(
        r'\{[^{}]*"name"\s*:\s*"(?P<name>\w+)"[^{}]*"(?:parameters|arguments)"\s*:\s*(?P<params>\{[^{}]*\})[^{}]*\}',
        content,
        re.DOTALL,
    ):
        try:
            params = json.loads(match.group("params"))
            tool_calls.append(
                {"id": "call_fb", "function": {"name": match.group("name"), "arguments": params}}
            )
        except Exception:
            pass
    if tool_calls:
        return tool_calls

    for block in re.findall(r"```(?:json)?\n(.*?)\n```", content, re.DOTALL):
        try:
            decoded = json.loads(block)
            items = decoded if isinstance(decoded, list) else [decoded]
            for item in items:
                if isinstance(item, dict) and "name" in item:
                    args = item.get("arguments") or item.get("parameters") or {}
                    tool_calls.append(
                        {"id": "call_fb", "function": {"name": item["name"], "arguments": args}}
                    )
        except Exception:
            pass
    if tool_calls:
        return tool_calls

    try:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            decoded = json.loads(content[start : end + 1])
            if "name" in decoded:
                args = decoded.get("arguments") or decoded.get("parameters") or {}
                if not args and "path" in decoded:
                    args = {"path": decoded["path"]}
                tool_calls.append(
                    {"id": "call_fb", "function": {"name": decoded["name"], "arguments": args}}
                )
    except Exception:
        pass
    return tool_calls


async def query_ollama(messages, tools=None, timeout=120):
    return await query_ollama_model(MODEL, messages, tools=tools, timeout=timeout)


async def query_ollama_model(model, messages, tools=None, timeout=120, num_predict=None):
    data = {"model": model, "think": False, "messages": messages, "stream": False}
    if tools:
        data["tools"] = tools
    if num_predict:
        data["options"] = {"num_predict": num_predict, "temperature": 0.1}
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def fix_path(path, allowed_dirs):
    if path.startswith("/"):
        return path
    for root in allowed_dirs:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(allowed_dirs[0], path)


def content_str(tool_result):
    if hasattr(tool_result, "content"):
        return "\n".join(item.text for item in tool_result.content if hasattr(item, "text"))
    return str(tool_result)


def get_server_params(allowed_dirs=None):
    npx = shutil.which("npx") or "npx"
    return StdioServerParameters(
        command=npx,
        args=["-y", "@modelcontextprotocol/server-filesystem"] + (allowed_dirs or CHAT_ALLOWED_DIRS),
    )


async def get_ollama_tools(session):
    response = await session.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema,
            },
        }
        for tool in response.tools
    ]


async def run_chat(user_prompt, history):
    system = {"role": "system", "content": CHAT_SYSTEM}
    messages = [system] + history + [{"role": "user", "content": user_prompt}]

    try:
        async with stdio_client(get_server_params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await get_ollama_tools(session)

                response = await query_ollama(messages, tools)
                if "error" in response:
                    print(json.dumps(response))
                    return

                message = response.get("message", {})
                content = message.get("content", "")
                tool_calls = message.get("tool_calls", []) or extract_tool_calls(content)

                if tool_calls:
                    messages.append(message)
                    for tool_call in tool_calls:
                        name = tool_call["function"]["name"]
                        args = tool_call["function"]["arguments"]
                        tool_call_id = tool_call.get("id", "call_default")
                        try:
                            if "path" in args:
                                args["path"] = fix_path(args["path"], CHAT_ALLOWED_DIRS)
                            result = await session.call_tool(name, args)
                            output = content_str(result)
                        except Exception as exc:
                            output = f"Error: {exc}"
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": name,
                                "content": output,
                            }
                        )

                    final = await query_ollama(messages)
                    if "error" in final:
                        print(json.dumps(final))
                    else:
                        print(
                            json.dumps(
                                {
                                    "response": final["message"]["content"],
                                    "history": messages + [final["message"]],
                                }
                            )
                        )
                else:
                    print(json.dumps({"response": content, "history": messages + [message]}))
    except Exception as exc:
        print(json.dumps({"error": f"MCP Error: {exc}"}))


def get_git_status(project_root):
    try:
        res = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5
        )
        if res.returncode == 0:
            lines = []
            for line in res.stdout.splitlines():
                if line.startswith("## "):
                    lines.append(line)
                    continue
                path = line[3:].strip() if len(line) > 3 else line.strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[-1]
                if is_noise_path(path):
                    continue
                lines.append(line)
            status = "\n".join(lines).strip()
            return status if status else "Clean (No changes detected)"
    except Exception:
        pass
    return None


def summarize_diff_hunks(diff_text):
    hunks = []
    current_file = None
    current_hunk = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_file = line.split(" b/", 1)[-1] if " b/" in line else line
            current_hunk = None
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start = int(match.group(1)) if match else None
            length = int(match.group(2) or "1") if match else None
            current_hunk = {
                "file": current_file,
                "start_line": start,
                "end_line": start + max(length - 1, 0) if start else None,
                "added": 0,
                "removed": 0,
                "signals": [],
            }
            hunks.append(current_hunk)
            continue
        if not current_hunk or not line or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            current_hunk["added"] += 1
        elif line.startswith("-"):
            current_hunk["removed"] += 1
        lowered = line.lower()
        if any(token in lowered for token in ("error", "exception", "todo", "fixme", "public ", "func ", "class ", "struct ", "def ")):
            signal = line[1:].strip() if line[:1] in "+-" else line.strip()
            if signal and len(current_hunk["signals"]) < 3:
                current_hunk["signals"].append(signal[:140])

    return hunks


def rank_diff_hunks(hunks, terms, max_hunks=8):
    def score_hunk(hunk):
        score = 0
        file_name = (hunk.get("file") or "").lower()
        signals = " ".join(hunk.get("signals") or []).lower()
        haystack = f"{file_name} {signals}"

        for term in terms:
            if term in file_name:
                score += 20
            if term in signals:
                score += 12

        if file_name.endswith((".py", ".swift", ".cs", ".js", ".ts")):
            score += 8
        if any(token in file_name for token in ("relay", "scout", "pipeline", "contentview", "player", "controller")):
            score += 8
        if any(token in haystack for token in ("error", "exception", "model", "token", "prompt", "diff", "log")):
            score += 6
        score += min((hunk.get("added") or 0) + (hunk.get("removed") or 0), 12)
        return score

    return sorted(hunks, key=score_hunk, reverse=True)[:max_hunks]


def get_git_diff_summary(project_root, terms=None):
    try:
        name_status = subprocess.run(
            ["git", "diff", "HEAD", "--name-status"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10
        )
        numstat = subprocess.run(
            ["git", "diff", "HEAD", "--numstat"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10
        )
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--unified=0", "--no-ext-diff"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10
        )
        if name_status.returncode != 0:
            return None

        changed_files = []
        for line in name_status.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                path = parts[-1]
                if not is_noise_path(path):
                    changed_files.append({"status": parts[0], "path": path})

        stats_by_path = {}
        if numstat.returncode == 0:
            for line in numstat.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    stats_by_path[parts[2]] = {"added": parts[0], "removed": parts[1]}

        for item in changed_files:
            item.update(stats_by_path.get(item["path"], {}))

        raw_diff = diff.stdout if diff.returncode == 0 else ""
        hunks = [
            hunk for hunk in summarize_diff_hunks(raw_diff)
            if not is_noise_path(hunk.get("file") or "")
        ]
        return {
            "changed_files": changed_files[:40],
            "changed_file_count": len(changed_files),
            "hunks": rank_diff_hunks(hunks, terms or []),
            "raw_diff_chars_omitted": len(raw_diff),
        }
    except Exception:
        pass
    return None


# -- Gather helpers ----------------------------------------------------------
def normalize_path(path):
    return str(Path(path).expanduser().resolve())


def is_noise_path(path):
    path = Path(path)
    return path.name in NOISE_PATH_NAMES or path.suffix.lower() in NOISE_SUFFIXES or "__pycache__" in path.parts


def rel_path(path, project_root):
    try:
        return str(Path(path).resolve().relative_to(Path(project_root).resolve()))
    except Exception:
        return str(path)


def dedupe_strings(items):
    seen = set()
    out = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def find_errors(text):
    out = []
    for line in text.splitlines():
        upper = line.upper()
        if any(token in upper for token in ["ERROR", "EXCEPTION", "FATAL", "TRACEBACK", "CRASH"]):
            stripped = line.strip()
            if len(stripped) > 5:
                out.append(stripped)
    return out


def prompt_terms(prompt):
    return [
        token
        for token in re.findall(r"[a-z0-9_./-]+", prompt.lower())
        if len(token) > 2 and token not in STOP_WORDS
    ]


def packet_mode_for_prompt(prompt):
    lowered = prompt.lower()
    if re.search(r"\b(review|regression|bugs?|buggy|do we have bugs|problems?|risk|risks)\b", lowered):
        return "review"
    if re.search(r"\b(implement|implementation|add|create|modify|update|fix|build)\b", lowered):
        return "implementation"
    if re.search(r"\b(change|changed|changes|changet|modified|recent|last|what changed|diff|git|status)\b", lowered):
        return "changes"
    if re.search(r"\b(debug|crash|error|exception|fail|failing|failure|log|traceback|not work|broken|diagnose|slow|latency)\b", lowered):
        return "debug"
    return "direct"


def classify_prompt_intent(prompt):
    lowered = prompt.lower()
    packet_mode = packet_mode_for_prompt(prompt)
    score = 0
    matches = []
    for keyword in DEBUG_KEYWORDS:
        if keyword in lowered:
            score += 2
            matches.append(keyword)

    if re.search(r"\b(line|stack|trace|traceback|stderr|stdout)\b", lowered):
        score += 2
    if re.search(r"\.(py|sh|swift|js|ts|log|json|toml|yaml|yml|plist)\b", lowered):
        score += 2
    if "/" in prompt:
        score += 1

    if packet_mode != "direct":
        score += 2

    needs_gather = score >= 2
    if needs_gather:
        reason = (
            f"Prompt looks like a debugging/investigation request ({', '.join(matches[:3])})."
            if matches
            else "Prompt references code, logs, or failure symptoms that benefit from local evidence."
        )
    else:
        reason = "No evidence gathered; packet contains only the prompt."

    return {"needs_gather": needs_gather, "reason": reason, "packet_mode": packet_mode, "confidence": min(1.0, 0.45 + score * 0.08)}


def parse_recent_roots(raw_json):
    try:
        decoded = json.loads(raw_json or "[]")
    except Exception:
        decoded = []
    roots = []
    for item in decoded:
        if isinstance(item, str) and os.path.isdir(os.path.expanduser(item)):
            roots.append(normalize_path(item))
    return dedupe_strings(roots)


def detect_project_type(project_root):
    root = Path(project_root)
    names = {child.name for child in root.iterdir()} if root.exists() else set()

    if "Assets" in names and "ProjectSettings" in names:
        return "unity", "Detected Unity project markers (`Assets` and `ProjectSettings`)."
    if "Package.swift" in names or any(name.endswith(".xcodeproj") or name.endswith(".xcworkspace") for name in names):
        return "swift", "Detected Swift/Xcode markers in the project root."
    if (
        "pyproject.toml" in names
        or "requirements.txt" in names
        or "Pipfile" in names
        or any(child.suffix == ".py" for child in root.iterdir())
    ):
        return "python", "Detected Python manifests or Python source files."
    if "package.json" in names or "pnpm-lock.yaml" in names or "yarn.lock" in names:
        return "javascript", "Detected JavaScript/TypeScript package manifests."
    return "unknown", "No strong project markers detected; using generic file heuristics."


def should_skip_dir(name):
    return name.startswith(".") and name not in {".config", ".github"} or name in SKIP_DIRS


def categorize_path(path):
    name = path.name
    suffix = path.suffix.lower()

    if name in MANIFEST_NAMES or name == "project.pbxproj" or name.endswith(".xcodeproj") or name.endswith(".xcworkspace"):
        return "manifest"
    if suffix in UNITY_EXTENSIONS:
        return "unity"
    if suffix in LOG_EXTENSIONS or "log" in name.lower() or name.lower().startswith(("ollama_", "stderr", "stdout")):
        return "log"
    if suffix in SCRIPT_EXTENSIONS or (not suffix and os.access(path, os.X_OK)):
        return "script"
    if suffix in SOURCE_EXTENSIONS:
        return "source"
    if suffix in CONFIG_EXTENSIONS:
        return "config"
    return None


def iter_project_files(project_root):
    discovered = []
    for base, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [name for name in dirnames if not should_skip_dir(name)]
        for filename in filenames:
            path = Path(base) / filename
            if path.is_symlink():
                continue
            if is_noise_path(path):
                continue
            if len(discovered) >= MAX_DISCOVERED_FILES:
                return discovered
            category = categorize_path(path)
            if category:
                discovered.append(
                    {
                        "path": str(path),
                        "name": filename,
                        "category": category,
                        "mtime": path.stat().st_mtime,
                    }
                )
    return discovered


def cache_key_for_root(project_root):
    return hashlib.sha256(normalize_path(project_root).encode()).hexdigest()[:24]


def index_cache_path(project_root):
    return DEFAULT_REPO_CACHE_DIR / f"{cache_key_for_root(project_root)}.json"


def file_digest(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def extract_symbols(path, text):
    suffix = Path(path).suffix.lower()
    patterns = []
    if suffix == ".cs":
        patterns = [
            r"\b(?:class|struct|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"\b(?:public|private|protected|internal|static|virtual|override|async|\s)+\s*[A-Za-z_][A-Za-z0-9_<>,\[\]?]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        ]
    elif suffix == ".swift":
        patterns = [
            r"\b(?:class|struct|enum|protocol|actor)\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"\bfunc\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        ]
    elif suffix == ".py":
        patterns = [r"^\s*(?:class|def)\s+([A-Za-z_][A-Za-z0-9_]*)" ]
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = [
            r"\b(?:class|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
            r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(",
        ]

    symbols = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            symbols.append(match.group(1))
            if len(symbols) >= 12:
                return dedupe_strings(symbols)
    return dedupe_strings(symbols)


def extract_unity_refs(path, text):
    if Path(path).suffix.lower() not in UNITY_EXTENSIONS:
        return []
    refs = []
    for guid in re.findall(r"guid:\s*([0-9a-fA-F]{32})", text):
        refs.append(f"guid:{guid}")
        if len(refs) >= 12:
            break
    if "m_Script:" in text:
        refs.insert(0, "contains MonoBehaviour script reference")
    if "Missing" in text or "missing" in text:
        refs.insert(0, "contains missing-reference text")
    return dedupe_strings(refs)[:12]


def build_repo_index(project_root, discovered):
    cache_path = index_cache_path(project_root)
    cache = {}
    try:
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except Exception:
        cache = {}

    indexed_files = []
    files_cache = cache.get("files", {})
    new_files_cache = {}
    changed_count = 0

    for item in discovered:
        path = item["path"]
        try:
            stat = os.stat(path)
        except OSError:
            continue
        cache_id = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
        cached = files_cache.get(path)
        if cached and cached.get("cache_id") == cache_id:
            indexed = cached
        else:
            changed_count += 1
            text = ""
            if Path(path).suffix.lower() in TEXT_EXTENSIONS or item["category"] in {"source", "script", "config", "manifest", "unity"}:
                text = read_text_file(path)
            indexed = {
                "cache_id": cache_id,
                "path": path,
                "category": item["category"],
                "size": stat.st_size,
                "mtime": item["mtime"],
                "digest": file_digest(path),
                "symbols": extract_symbols(path, text),
                "unity_refs": extract_unity_refs(path, text),
            }
        new_files_cache[path] = indexed
        indexed_files.append(indexed)

    next_cache = {
        "project_root": normalize_path(project_root),
        "updated_at": int(os.path.getmtime(project_root)) if os.path.exists(project_root) else 0,
        "files": new_files_cache,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(next_cache))
    except Exception:
        pass

    return {
        "cache_path": str(cache_path),
        "indexed_file_count": len(indexed_files),
        "changed_index_entries": changed_count,
        "files": indexed_files,
    }


def extract_explicit_paths(prompt, project_root):
    project_root = normalize_path(project_root)
    candidates = []
    for match in re.findall(r"(/[A-Za-z0-9._/\-]+)", prompt):
        path = os.path.expanduser(match.rstrip(".,:)"))
        if not os.path.exists(path):
            continue
        normalized = normalize_path(path)
        if is_noise_path(normalized):
            continue
        candidates.append(normalized)
    return dedupe_strings(candidates)


def file_rank(item, terms, intent, project_type, packet_mode="debug", changed_paths=None, explicit_paths=None, error_paths=None):
    score = 0
    changed_paths = changed_paths or set()
    explicit_paths = explicit_paths or set()
    error_paths = error_paths or set()
    lowered_name = item["name"].lower()
    lowered_path = item["path"].lower()
    category = item["category"]
    normalized = normalize_path(item["path"])
    rel = item.get("relative_path") or item["path"]

    if normalized in explicit_paths or item["path"] in explicit_paths:
        score += 200
    if rel in changed_paths or item["path"] in changed_paths or normalized in changed_paths:
        score += 120 if packet_mode in {"changes", "review", "implementation"} else 50
    if item["path"] in error_paths or normalized in error_paths:
        score += 130 if packet_mode == "debug" else 45

    if category == "manifest":
        score += 18 if packet_mode in {"changes", "review"} else 70
    if category == "log":
        score += 60 if packet_mode == "debug" else 18
    if category == "unity":
        score += 60 if project_type == "unity" else 20
    if category == "script":
        score += 45 if "script" in terms or "script" in intent["reason"].lower() else 25
    if category == "source":
        score += 45 if packet_mode in {"changes", "review", "implementation"} else 25
    if category == "config":
        score += 18

    if project_type == "unity":
        if item["path"].endswith((".cs", ".asmdef", ".unity", ".prefab")):
            score += 28
    elif project_type == "swift":
        if lowered_name == "package.swift" or lowered_name.endswith(".xcodeproj"):
            score += 25
        if item["path"].endswith(".swift"):
            score += 18
    elif project_type == "python":
        if lowered_name in {"pyproject.toml", "requirements.txt", "setup.py"}:
            score += 25
        if item["path"].endswith(".py"):
            score += 18
    elif project_type == "javascript":
        if lowered_name in {"package.json", "pnpm-lock.yaml", "yarn.lock"}:
            score += 25
        if item["path"].endswith((".js", ".jsx", ".ts", ".tsx")):
            score += 18

    for term in terms:
        if term in lowered_name:
            score += 18
        elif term in lowered_path:
            score += 8
        if term in " ".join(item.get("symbols") or []).lower():
            score += 22

    recency = max(0, item["mtime"])
    score += min(int(recency / 10_000_000), 15)
    return score


def read_text_file(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES)
        return data.decode("utf-8", errors="replace")
    except Exception as exc:
        return f"[Unable to read file: {exc}]"


def excerpt_for_text(text, terms):
    if not text:
        return "", None, None
    lines = text.splitlines()
    lowered = text.lower()
    for term in terms:
        idx = lowered.find(term)
        if idx != -1:
            start = max(0, idx - 250)
            end = min(len(text), idx + MAX_PREVIEW_CHARS)
            start_line = text[:start].count("\n") + 1
            end_line = text[:end].count("\n") + 1
            return text[start:end].strip(), start_line, end_line
    preview = text[:MAX_PREVIEW_CHARS].strip()
    end_line = min(len(lines), max(1, preview.count("\n") + 1)) if preview else None
    return preview, 1 if preview else None, end_line


def excerpt_for_log(text, terms):
    lines = text.splitlines()
    error_lines = [line for line in lines if find_errors(line)]
    if error_lines:
        return "\n".join(error_lines[:12])[:MAX_PREVIEW_CHARS], None, None

    lowered_lines = [line.lower() for line in lines]
    for term in terms:
        for idx, line in enumerate(lowered_lines):
            if term in line:
                start = max(0, idx - 8)
                end = min(len(lines), idx + 12)
                return "\n".join(lines[start:end])[:MAX_PREVIEW_CHARS], start + 1, end
    start = max(0, len(lines) - 80)
    return "\n".join(lines[start:])[:MAX_PREVIEW_CHARS], start + 1 if lines else None, len(lines) if lines else None


def build_reason(item, project_type, terms):
    name = item["name"]
    category = item["category"]

    if category == "manifest":
        return f"Included as a primary project manifest for the detected {project_type} project."
    if category == "log":
        return "Included because recent logs are often the fastest signal for debugging prompts."
    if category == "unity":
        return f"Included as Unity serialized/project evidence (`{name}`)."
    if category == "script":
        return f"Included as a likely execution entry point or script candidate (`{name}`)."
    if category == "config":
        return f"Included as a configuration file that may control runtime behavior (`{name}`)."

    for term in terms:
        if term in name.lower():
            return f"Included because its filename matches the prompt term `{term}`."
    return "Included as a likely relevant source file based on project type and recency."


def evidence_item_from_path(path, category, reason, terms, indexed=None):
    text = read_text_file(path)
    preview, start_line, end_line = excerpt_for_log(text, terms) if category == "log" else excerpt_for_text(text, terms)
    return {
        "path": path,
        "kind": category,
        "reason": reason,
        "preview": preview,
        "start_line": start_line,
        "end_line": end_line,
        "symbols": (indexed or {}).get("symbols") or extract_symbols(path, text),
        "unity_refs": (indexed or {}).get("unity_refs") or extract_unity_refs(path, text),
    }


def select_evidence(project_root, prompt, project_type, repo_index=None, preflight=None):
    terms = prompt_terms(prompt)
    intent = classify_prompt_intent(prompt)
    packet_mode = (preflight or {}).get("packet_mode") or intent["packet_mode"]
    changed_paths = set((preflight or {}).get("changed_paths") or [])
    explicit_paths = set((preflight or {}).get("explicit_paths") or [])
    error_paths = set((preflight or {}).get("error_paths") or [])
    if repo_index:
        discovered = [
            {
                "path": item["path"],
                "relative_path": rel_path(item["path"], project_root),
                "name": Path(item["path"]).name,
                "category": item["category"],
                "mtime": item.get("mtime", 0),
                "symbols": item.get("symbols") or [],
                "unity_refs": item.get("unity_refs") or [],
            }
            for item in repo_index.get("files", [])
        ]
        indexed_by_path = {item["path"]: item for item in repo_index.get("files", [])}
    else:
        discovered = iter_project_files(project_root)
        indexed_by_path = {}
    scored = sorted(
        discovered,
        key=lambda item: file_rank(
            item,
            terms,
            intent,
            project_type,
            packet_mode=packet_mode,
            changed_paths=changed_paths,
            explicit_paths=explicit_paths,
            error_paths=error_paths,
        ),
        reverse=True,
    )

    evidence = []
    seen_paths = set()
    category_limits = {"manifest": 2, "log": 2, "script": 2, "source": 3, "config": 2, "unity": 3, "notes": 1}
    category_counts = {key: 0 for key in category_limits}

    for item in scored:
        category = item["category"]
        if category_counts.get(category, 0) >= category_limits.get(category, 0):
            continue
        if item["path"] in seen_paths:
            continue
        seen_paths.add(item["path"])
        category_counts[category] = category_counts.get(category, 0) + 1
        evidence.append(
            evidence_item_from_path(
                item["path"],
                category,
                build_reason(item, project_type, terms),
                terms,
                indexed_by_path.get(item["path"]),
            )
        )
        if len(evidence) >= MAX_EVIDENCE_ITEMS:
            break

    return evidence


def gather_external_evidence(prompt, project_root, terms):
    extras = []
    for path in extract_explicit_paths(prompt, project_root):
        if not os.path.isfile(path):
            continue
        category = categorize_path(Path(path)) or "notes"
        extras.append(
            evidence_item_from_path(
                path,
                category,
                "Included because the prompt explicitly referenced this external path.",
                terms,
            )
        )
    return extras


def build_preflight(prompt, project_root, project_type, discovered, repo_index, git_status, git_diff_summary):
    intent = classify_prompt_intent(prompt)
    explicit_paths = extract_explicit_paths(prompt, project_root)
    changed_files = (git_diff_summary or {}).get("changed_files") or []
    changed_paths = {
        item.get("path")
        for item in changed_files
        if item.get("path") and not is_noise_path(item.get("path"))
    }
    changed_paths.update(
        normalize_path(Path(project_root) / path)
        for path in list(changed_paths)
        if path and not str(path).startswith("/")
    )
    error_paths = set()
    log_candidates = []
    for item in discovered:
        if item.get("category") != "log":
            continue
        preview = excerpt_for_log(read_text_file(item["path"]), prompt_terms(prompt))[0]
        errors = find_errors(preview)
        if errors:
            error_paths.add(normalize_path(item["path"]))
        log_candidates.append({"path": item["path"], "errors": errors[:MAX_ERROR_LINES]})

    candidate_paths = [
        item.get("path")
        for item in sorted(
            repo_index.get("files", []),
            key=lambda entry: (entry.get("mtime") or 0),
            reverse=True,
        )[:30]
    ]
    return {
        "intent": intent,
        "packet_mode": intent["packet_mode"],
        "confidence": intent["confidence"],
        "terms": prompt_terms(prompt),
        "explicit_paths": explicit_paths,
        "changed_files": changed_files,
        "changed_paths": sorted(changed_paths),
        "git_status": git_status,
        "git_diff_summary": git_diff_summary,
        "log_candidates": log_candidates[:5],
        "error_paths": sorted(error_paths),
        "candidate_paths": [path for path in candidate_paths if path and not is_noise_path(path)],
        "project_type": project_type,
    }


def fallback_summary(prompt, project_root, project_type, evidence_items, error_lines, packet_mode="debug"):
    assumptions = []
    open_questions = []

    script_candidates = [item for item in evidence_items if item["kind"] == "script"]
    if "script" in prompt.lower() and script_candidates:
        assumptions.append(
            f"Assumed `{Path(script_candidates[0]['path']).name}` is the most relevant script based on ranking."
        )
        if len(script_candidates) > 1:
            open_questions.append("Multiple script candidates were found; confirm the exact entry point if needed.")

    if not error_lines:
        open_questions.append("No explicit error lines were found in the selected excerpts.")
    if not any(item["kind"] == "log" for item in evidence_items):
        open_questions.append("No repo-local logs were found; a runtime log path may still be needed.")

    summary = (
        f"Prepared a {packet_mode} packet with {len(evidence_items)} targeted evidence item(s) from "
        f"the {project_type} project at `{project_root}`."
    )
    confidence = 0.72 if error_lines else 0.58
    return {
        "summary": summary,
        "assumptions": assumptions[:3],
        "open_questions": open_questions[:3],
        "confidence": confidence,
    }


def should_use_model_summary(model_summary):
    if not model_summary:
        return False
    summary_text = (model_summary.get("summary") or "").lower()
    if model_summary.get("confidence", 0) < 0.35:
        return False
    if "not working as expected" in summary_text or "seems" in summary_text:
        return False
    return True


def extract_json_object(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


async def summarize_with_ollama(prompt, project_root, project_type, evidence_items, error_lines):
    summary_payload = {
        "prompt": prompt,
        "project_root": project_root,
        "project_type": project_type,
        "evidence": [
            {
                "path": item["path"],
                "kind": item["kind"],
                "reason": item["reason"],
                "preview": item["preview"][:700],
            }
            for item in evidence_items
        ],
        "error_lines": error_lines[:MAX_ERROR_LINES],
    }

    response = await query_ollama(
        [
            {"role": "system", "content": OLLAMA_SUMMARY_SYSTEM},
            {"role": "user", "content": json.dumps(summary_payload)},
        ],
        timeout=OLLAMA_SUMMARY_TIMEOUT,
    )
    if "error" in response:
        return None

    content = response.get("message", {}).get("content", "")
    decoded = extract_json_object(content)
    if not isinstance(decoded, dict):
        return None

    confidence = decoded.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = 0.55

    return {
        "summary": decoded.get("summary") or "",
        "assumptions": decoded.get("assumptions") or [],
        "open_questions": decoded.get("open_questions") or [],
        "confidence": max(0.0, min(1.0, float(confidence))),
    }


def ranker_payload(prompt, preflight, evidence_items):
    return {
        "prompt": prompt,
        "packet_mode": preflight.get("packet_mode"),
        "terms": preflight.get("terms") or [],
        "candidates": [
            {
                "id": index,
                "path": item.get("path"),
                "kind": item.get("kind"),
                "reason": item.get("reason"),
                "symbols": item.get("symbols") or [],
                "preview": (item.get("preview") or "")[:220],
            }
            for index, item in enumerate(evidence_items)
        ],
    }


async def rank_evidence_with_model(prompt, preflight, evidence_items):
    if not evidence_items:
        return evidence_items, {"stage": "ranker", "model": RANKER_MODEL, "status": "skipped"}
    system = (
        "Rank small evidence candidates for a Codex packet. Return JSON only: "
        "{\"ordered_ids\":[0,1],\"notes\":[\"...\"]}. Use only candidate ids."
    )
    response = await query_ollama_model(
        RANKER_MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(ranker_payload(prompt, preflight, evidence_items))},
        ],
        timeout=25,
        num_predict=180,
    )
    if "error" in response:
        return evidence_items, {"stage": "ranker", "model": RANKER_MODEL, "status": "failed", "error": response["error"]}
    decoded = extract_json_object(response.get("message", {}).get("content", ""))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("ordered_ids"), list):
        return evidence_items, {"stage": "ranker", "model": RANKER_MODEL, "status": "failed", "error": "invalid ranker JSON"}

    ordered = []
    seen = set()
    for raw_id in decoded.get("ordered_ids", []):
        if not isinstance(raw_id, int) or raw_id < 0 or raw_id >= len(evidence_items) or raw_id in seen:
            continue
        seen.add(raw_id)
        ordered.append(evidence_items[raw_id])
    ordered.extend(item for index, item in enumerate(evidence_items) if index not in seen)
    return ordered, {
        "stage": "ranker",
        "model": RANKER_MODEL,
        "status": "ok",
        "notes": decoded.get("notes") or [],
    }


async def analyze_packet_with_model(prompt, preflight, evidence_items, error_lines):
    payload = {
        "prompt": prompt,
        "packet_mode": preflight.get("packet_mode"),
        "evidence": [
            {
                "path": item.get("path"),
                "kind": item.get("kind"),
                "reason": item.get("reason"),
                "preview": (item.get("preview") or "")[:500],
            }
            for item in evidence_items
        ],
        "error_lines": error_lines[:MAX_ERROR_LINES],
    }
    system = (
        "Analyze a compact evidence packet. Return JSON only with "
        "{\"hypotheses\":[\"...\"],\"missing_context\":[\"...\"]}. Do not invent facts."
    )
    response = await query_ollama_model(
        ANALYST_MODEL,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload)},
        ],
        timeout=45,
        num_predict=280,
    )
    if "error" in response:
        return None, {"stage": "analyst", "model": ANALYST_MODEL, "status": "failed", "error": response["error"]}
    decoded = extract_json_object(response.get("message", {}).get("content", ""))
    if not isinstance(decoded, dict):
        return None, {"stage": "analyst", "model": ANALYST_MODEL, "status": "failed", "error": "invalid analyst JSON"}
    return decoded, {"stage": "analyst", "model": ANALYST_MODEL, "status": "ok"}


def estimate_tokens(text):
    return max(1, int(len(text) / 4))


def format_line_range(item):
    if item.get("start_line") and item.get("end_line"):
        return f":{item['start_line']}-{item['end_line']}"
    if item.get("start_line"):
        return f":{item['start_line']}"
    return ""


def format_git_diff_summary(summary):
    if not summary:
        return []

    lines = [
        f"Changed files: {summary.get('changed_file_count', 0)}",
        f"Raw diff omitted: {summary.get('raw_diff_chars_omitted', 0)} chars",
    ]
    changed_files = summary.get("changed_files") or []
    if changed_files:
        lines.append("Changed file list:")
        for item in changed_files[:20]:
            stats = ""
            if item.get("added") is not None or item.get("removed") is not None:
                stats = f" (+{item.get('added', '?')}/-{item.get('removed', '?')})"
            lines.append(f"- {item.get('status', '?')} {item.get('path', '')}{stats}")
        if summary.get("changed_file_count", 0) > len(changed_files[:20]):
            lines.append(f"- ... {summary.get('changed_file_count', 0) - len(changed_files[:20])} more changed files omitted")

    hunks = summary.get("hunks") or []
    if hunks:
        lines.append("Top changed hunks:")
        for index, hunk in enumerate(hunks, start=1):
            line_range = ""
            if hunk.get("start_line"):
                line_range = f":{hunk['start_line']}-{hunk.get('end_line') or hunk['start_line']}"
            lines.append(
                f"{index}. {hunk.get('file', '[unknown]')}{line_range} "
                f"(+{hunk.get('added', 0)}/-{hunk.get('removed', 0)})"
            )
            for signal in hunk.get("signals") or []:
                lines.append(f"   signal: {signal}")
    return lines


def format_preflight(preflight):
    if not preflight:
        return []
    lines = [
        f"- Mode: {preflight.get('packet_mode', 'direct')}",
        f"- Intent confidence: {preflight.get('confidence', 0):.2f}",
    ]
    if preflight.get("explicit_paths"):
        lines.append(f"- Explicit paths: {len(preflight['explicit_paths'])}")
    if preflight.get("changed_files"):
        lines.append(f"- Changed files considered: {len(preflight['changed_files'])}")
    if preflight.get("log_candidates"):
        lines.append(f"- Log candidates considered: {len(preflight['log_candidates'])}")
    return lines


def format_model_analysis(model_analysis):
    if not model_analysis:
        return []
    lines = []
    hypotheses = model_analysis.get("hypotheses") or []
    missing = model_analysis.get("missing_context") or []
    if hypotheses:
        lines.append("Local analyst hypotheses:")
        lines.extend(f"- {item}" for item in hypotheses[:4])
    if missing:
        lines.append("Local analyst missing context:")
        lines.extend(f"- {item}" for item in missing[:4])
    return lines


def build_omitted_context(bundle):
    omitted = dict(bundle.get("omitted_context") or {})
    diff_summary = bundle.get("git_diff_summary") or {}
    if diff_summary.get("raw_diff_chars_omitted"):
        omitted["raw_git_diff_chars"] = diff_summary["raw_diff_chars_omitted"]
    repo_index = bundle.get("repo_index") or {}
    indexed_count = repo_index.get("indexed_file_count")
    evidence_count = len(bundle.get("evidence_items") or [])
    if indexed_count is not None:
        omitted["indexed_files_not_in_packet"] = max(0, indexed_count - evidence_count)
    return omitted


def build_codex_packet(user_prompt, bundle, token_budget=DEFAULT_TOKEN_BUDGET):
    max_tokens = TOKEN_BUDGETS.get(token_budget, TOKEN_BUDGETS[DEFAULT_TOKEN_BUDGET])
    evidence_items = list(bundle.get("evidence_items") or [])
    preview_chars = 900

    while True:
        parts = [
            "Goal:",
            user_prompt.strip(),
            "",
            "Use only the evidence below first. If insufficient, ask for exactly 1-3 missing files or commands.",
            "",
            "Known facts:",
            f"- Project root: {bundle.get('project_root') or '[not selected]'}",
            f"- Project type: {bundle.get('project_type') or 'unknown'}",
            f"- Route: {bundle.get('routing_decision')}",
            f"- Packet mode: {bundle.get('packet_mode', 'direct')}",
            f"- Analysis depth: {bundle.get('analysis_depth', 'deterministic')}",
            f"- Confidence: {bundle.get('confidence', 0):.2f}",
            f"- Token budget: {token_budget} <= {max_tokens} estimated tokens",
        ]

        preflight_lines = format_preflight(bundle.get("preflight"))
        if preflight_lines:
            parts.extend(["", "Preflight:"])
            parts.extend(preflight_lines)

        if bundle.get("context_summary"):
            parts.extend(["", "Summary:", bundle["context_summary"]])

        assumptions = bundle.get("assumptions") or []
        if assumptions:
            parts.append("")
            parts.append("Assumptions:")
            parts.extend(f"- {item}" for item in assumptions[:5])

        git_status = bundle.get("git_status")
        if git_status:
            parts.extend(["", "Git status:", git_status])

        diff_lines = format_git_diff_summary(bundle.get("git_diff_summary"))
        if diff_lines:
            parts.extend(["", "Git diff summary:"])
            parts.extend(diff_lines)

        error_lines = bundle.get("error_lines") or []
        if error_lines:
            parts.extend(["", "Normalized errors:"])
            parts.extend(f"- {line}" for line in error_lines[:MAX_ERROR_LINES])

        if evidence_items:
            parts.extend(["", "Evidence:"])
            for index, item in enumerate(evidence_items, start=1):
                line_range = format_line_range(item)
                parts.extend(
                    [
                        f"{index}. {item.get('path', '[unknown]')}{line_range} [{item.get('kind', 'file')}]",
                        f"   Reason: {item.get('reason', '')}",
                    ]
                )
                if item.get("symbols"):
                    parts.append(f"   Symbols: {', '.join(item['symbols'][:8])}")
                if item.get("unity_refs"):
                    parts.append(f"   Unity refs: {', '.join(item['unity_refs'][:5])}")
                preview = (item.get("preview") or "")[:preview_chars].strip()
                parts.extend(["   Snippet:", indent_block(preview or "[No preview available]", "   ")])

        analysis_lines = format_model_analysis(bundle.get("model_analysis"))
        if analysis_lines:
            parts.extend(["", "Optional local analysis:"])
            parts.extend(analysis_lines)

        open_questions = bundle.get("open_questions") or []
        if open_questions:
            parts.extend(["", "Open questions:"])
            parts.extend(f"- {item}" for item in open_questions[:4])

        omitted = build_omitted_context(bundle)
        if omitted:
            parts.extend(["", "Omitted context:"])
            parts.extend(f"- {key}: {value}" for key, value in omitted.items())

        parts.extend(
            [
                "",
                "Expected Codex behavior:",
                "- Diagnose from this packet first.",
                "- Request at most 1-3 extra files/commands if blocked.",
                "- Do not refactor unrelated code.",
            ]
        )

        packet = "\n".join(parts).strip()
        if estimate_tokens(packet) <= max_tokens or (len(evidence_items) <= 3 and preview_chars <= 450):
            return packet
        if preview_chars > 450:
            preview_chars -= 150
        else:
            evidence_items = evidence_items[: max(3, len(evidence_items) - 1)]


def indent_block(text, prefix):
    return "\n".join(prefix + line for line in text.splitlines())


def build_enriched_prompt(user_prompt, bundle):
    return build_codex_packet(user_prompt, bundle, bundle.get("token_budget", DEFAULT_TOKEN_BUDGET))


def bundle_for_direct_pass(prompt, reason, project_root=None, token_budget=DEFAULT_TOKEN_BUDGET, analysis_depth="deterministic", preflight=None):
    packet = prompt.strip()
    return {
        "mode": "gather",
        "original_prompt": prompt,
        "project_root": project_root,
        "project_type": None,
        "routing_decision": "direct_pass_through",
        "packet_mode": "direct",
        "analysis_depth": analysis_depth,
        "analysis_stages": [{"stage": "preflight", "status": "direct"}],
        "preflight": preflight,
        "gather_reason": reason,
        "confidence": 1.0,
        "gathered_files": {},
        "evidence_items": [],
        "error_lines": [],
        "context_summary": "No evidence gathered; packet contains only the prompt.",
        "open_questions": [],
        "assumptions": [],
        "git_status": None,
        "git_diff": None,
        "git_diff_summary": None,
        "repo_index": None,
        "token_budget": token_budget,
        "estimated_tokens": estimate_tokens(packet),
        "omitted_context": {},
        "codex_packet": packet,
        "enriched_prompt": packet,
    }


async def run_gather(
    user_prompt,
    project_root,
    recent_roots_json,
    token_budget=DEFAULT_TOKEN_BUDGET,
    use_local_summary=False,
    analysis_depth="deterministic",
):
    if analysis_depth not in ANALYSIS_DEPTHS:
        analysis_depth = "deterministic"
    intent = classify_prompt_intent(user_prompt)
    recent_roots = parse_recent_roots(recent_roots_json)

    if not intent["needs_gather"]:
        preflight = {
            "intent": intent,
            "packet_mode": "direct",
            "confidence": intent["confidence"],
            "terms": prompt_terms(user_prompt),
            "explicit_paths": [],
            "changed_files": [],
            "changed_paths": [],
            "log_candidates": [],
            "error_paths": [],
            "candidate_paths": [],
        }
        print(json.dumps(bundle_for_direct_pass(user_prompt, intent["reason"], project_root, token_budget, analysis_depth, preflight)))
        return

    if not project_root:
        print(json.dumps({"error": "This prompt needs project context. Select a project root before relaying it."}))
        return

    try:
        project_root = normalize_path(project_root)
    except Exception as exc:
        print(json.dumps({"error": f"Invalid project root: {exc}"}))
        return

    if not os.path.isdir(project_root):
        print(json.dumps({"error": f"Project root does not exist: {project_root}"}))
        return

    terms = prompt_terms(user_prompt)
    project_type, type_reason = detect_project_type(project_root)
    git_status = get_git_status(project_root)
    git_diff_summary = get_git_diff_summary(project_root, terms)

    discovered = iter_project_files(project_root)
    repo_index = build_repo_index(project_root, discovered)
    preflight = build_preflight(user_prompt, project_root, project_type, discovered, repo_index, git_status, git_diff_summary)
    explicit_items = gather_external_evidence(user_prompt, project_root, terms)
    evidence_items = explicit_items + select_evidence(project_root, user_prompt, project_type, repo_index, preflight)
    deduped_evidence = []
    seen = set()
    for item in evidence_items:
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        deduped_evidence.append(item)
        if len(deduped_evidence) >= MAX_EVIDENCE_ITEMS:
            break
    evidence_items = deduped_evidence

    error_lines = dedupe_strings(
        [
            error
            for item in evidence_items
            if item.get("kind") == "log"
            for error in find_errors(item.get("preview", ""))
        ]
    )[:MAX_ERROR_LINES]

    analysis_stages = [{"stage": "preflight", "status": "ok"}, {"stage": "deterministic", "status": "ok"}]

    if analysis_depth in {"ranked", "analyst"}:
        ranked_items, rank_stage = await rank_evidence_with_model(user_prompt, preflight, evidence_items)
        evidence_items = ranked_items
        analysis_stages.append(rank_stage)

    model_analysis = None
    if analysis_depth == "analyst":
        model_analysis, analyst_stage = await analyze_packet_with_model(user_prompt, preflight, evidence_items, error_lines)
        analysis_stages.append(analyst_stage)

    summary = fallback_summary(user_prompt, project_root, project_type, evidence_items, error_lines, preflight["packet_mode"])
    if use_local_summary:
        model_summary = await summarize_with_ollama(
            user_prompt, project_root, project_type, evidence_items, error_lines
        )
        if should_use_model_summary(model_summary):
            summary["summary"] = model_summary.get("summary") or summary["summary"]
            summary["assumptions"] = dedupe_strings(
                summary.get("assumptions", []) + list(model_summary.get("assumptions") or [])
            )[:4]
            summary["open_questions"] = dedupe_strings(
                summary.get("open_questions", []) + list(model_summary.get("open_questions") or [])
            )[:4]
            summary["confidence"] = max(summary.get("confidence", 0.55), model_summary.get("confidence", 0.55))

    if type_reason not in summary["assumptions"]:
        summary["assumptions"] = [type_reason] + list(summary.get("assumptions") or [])
    if recent_roots and project_root not in recent_roots:
        summary["assumptions"].append("Selected project root was used as the authoritative scope for gathering.")

    bundle = {
        "mode": "gather",
        "original_prompt": user_prompt,
        "project_root": project_root,
        "project_type": project_type,
        "routing_decision": "gathered_and_relayed",
        "packet_mode": preflight["packet_mode"],
        "analysis_depth": analysis_depth,
        "analysis_stages": analysis_stages,
        "preflight": {
            key: value
            for key, value in preflight.items()
            if key not in {"changed_paths", "error_paths", "candidate_paths"}
        },
        "model_analysis": model_analysis,
        "gather_reason": intent["reason"],
        "confidence": summary.get("confidence", 0.55),
        "git_status": git_status,
        "git_diff": None,
        "git_diff_summary": git_diff_summary,
        "repo_index": {
            "cache_path": repo_index.get("cache_path"),
            "indexed_file_count": repo_index.get("indexed_file_count"),
            "changed_index_entries": repo_index.get("changed_index_entries"),
        },
        "token_budget": token_budget,
        "gathered_files": {
            item["path"]: {"tool": item["kind"], "preview": item["preview"][:300]}
            for item in evidence_items
        },
        "evidence_items": evidence_items,
        "error_lines": error_lines,
        "context_summary": summary.get("summary") or "",
        "open_questions": dedupe_strings(summary.get("open_questions") or [])[:3],
        "assumptions": dedupe_strings(summary.get("assumptions") or [])[:4],
        "omitted_context": {
            "discovered_files": len(discovered),
            "selected_evidence_items": len(evidence_items),
            "local_summary_model_used": bool(use_local_summary),
            "analysis_depth": analysis_depth,
        },
    }
    bundle["codex_packet"] = build_codex_packet(user_prompt, bundle, token_budget)
    bundle["estimated_tokens"] = estimate_tokens(bundle["codex_packet"])
    bundle["enriched_prompt"] = bundle["codex_packet"]
    print(json.dumps(bundle))


# -- Entry point -------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", help="User prompt")
    parser.add_argument("history", nargs="?", default="[]", help="JSON history (chat mode)")
    parser.add_argument("--mode", default="chat", choices=["chat", "gather"])
    parser.add_argument("--project-root", default="", help="Primary project root for gather mode")
    parser.add_argument(
        "--recent-roots-json",
        default="[]",
        help="JSON array of recent project roots for gather mode context",
    )
    parser.add_argument(
        "--token-budget",
        default=DEFAULT_TOKEN_BUDGET,
        choices=sorted(TOKEN_BUDGETS.keys()),
        help="Maximum Codex packet budget tier for gather mode",
    )
    parser.add_argument(
        "--use-local-summary",
        action="store_true",
        help="Optionally ask the local model for a tiny summary after deterministic gathering",
    )
    parser.add_argument(
        "--analysis-depth",
        default="deterministic",
        choices=sorted(ANALYSIS_DEPTHS),
        help="Optional local analysis depth after deterministic preflight",
    )
    args = parser.parse_args()

    if args.mode == "gather":
        asyncio.run(
            run_gather(
                args.prompt,
                args.project_root,
                args.recent_roots_json,
                args.token_budget,
                args.use_local_summary,
                args.analysis_depth,
            )
        )
    else:
        history = []
        try:
            history = json.loads(args.history)
        except Exception:
            pass
        asyncio.run(run_chat(args.prompt, history))
