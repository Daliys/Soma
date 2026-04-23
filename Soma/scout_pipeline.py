#!/usr/bin/env python3
"""
Soma Scout Pipeline
  --mode chat   (default) Interactive chat with Llama via MCP filesystem tools
  --mode gather            Intent-gated evidence gathering for Relay mode
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# -- Config -----------------------------------------------------------------
MODEL = "llama3.2:3b"
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
TEXT_EXTENSIONS = SOURCE_EXTENSIONS | CONFIG_EXTENSIONS | LOG_EXTENSIONS | {".md", ".txt"}

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
    data = {"model": MODEL, "messages": messages, "stream": False}
    if tools:
        data["tools"] = tools
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


# -- Gather helpers ----------------------------------------------------------
def normalize_path(path):
    return str(Path(path).expanduser().resolve())


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


def classify_prompt_intent(prompt):
    lowered = prompt.lower()
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

    needs_gather = score >= 2
    if needs_gather:
        reason = (
            f"Prompt looks like a debugging/investigation request ({', '.join(matches[:3])})."
            if matches
            else "Prompt references code, logs, or failure symptoms that benefit from local evidence."
        )
    else:
        reason = "Prompt reads like a general question, so Relay can forward it directly."

    return {"needs_gather": needs_gather, "reason": reason}


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


def extract_explicit_paths(prompt, project_root):
    project_root = normalize_path(project_root)
    candidates = []
    for match in re.findall(r"(/[A-Za-z0-9._/\-]+)", prompt):
        path = os.path.expanduser(match.rstrip(".,:)"))
        if not os.path.exists(path):
            continue
        normalized = normalize_path(path)
        if normalized.startswith(project_root):
            continue
        candidates.append(normalized)
    return dedupe_strings(candidates)


def file_rank(item, terms, intent, project_type):
    score = 0
    lowered_name = item["name"].lower()
    lowered_path = item["path"].lower()
    category = item["category"]

    if category == "manifest":
        score += 70
    if category == "log":
        score += 50 if intent["needs_gather"] else 10
    if category == "script":
        score += 45 if "script" in terms or "script" in intent["reason"].lower() else 25
    if category == "source":
        score += 25
    if category == "config":
        score += 18

    if project_type == "swift":
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
        return ""
    lowered = text.lower()
    for term in terms:
        idx = lowered.find(term)
        if idx != -1:
            start = max(0, idx - 250)
            end = min(len(text), idx + MAX_PREVIEW_CHARS)
            return text[start:end].strip()
    return text[:MAX_PREVIEW_CHARS].strip()


def excerpt_for_log(text, terms):
    lines = text.splitlines()
    error_lines = [line for line in lines if find_errors(line)]
    if error_lines:
        return "\n".join(error_lines[:12])[:MAX_PREVIEW_CHARS]

    lowered_lines = [line.lower() for line in lines]
    for term in terms:
        for idx, line in enumerate(lowered_lines):
            if term in line:
                start = max(0, idx - 8)
                end = min(len(lines), idx + 12)
                return "\n".join(lines[start:end])[:MAX_PREVIEW_CHARS]
    return "\n".join(lines[-80:])[:MAX_PREVIEW_CHARS]


def build_reason(item, project_type, terms):
    name = item["name"]
    category = item["category"]

    if category == "manifest":
        return f"Included as a primary project manifest for the detected {project_type} project."
    if category == "log":
        return "Included because recent logs are often the fastest signal for debugging prompts."
    if category == "script":
        return f"Included as a likely execution entry point or script candidate (`{name}`)."
    if category == "config":
        return f"Included as a configuration file that may control runtime behavior (`{name}`)."

    for term in terms:
        if term in name.lower():
            return f"Included because its filename matches the prompt term `{term}`."
    return "Included as a likely relevant source file based on project type and recency."


def evidence_item_from_path(path, category, reason, terms):
    text = read_text_file(path)
    preview = excerpt_for_log(text, terms) if category == "log" else excerpt_for_text(text, terms)
    return {
        "path": path,
        "kind": category,
        "reason": reason,
        "preview": preview,
    }


def select_evidence(project_root, prompt, project_type):
    terms = prompt_terms(prompt)
    discovered = iter_project_files(project_root)
    scored = sorted(
        discovered,
        key=lambda item: file_rank(item, terms, classify_prompt_intent(prompt), project_type),
        reverse=True,
    )

    evidence = []
    seen_paths = set()
    category_limits = {"manifest": 2, "log": 2, "script": 2, "source": 3, "config": 2, "notes": 1}
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
            )
        )
        if len(evidence) >= MAX_EVIDENCE_ITEMS:
            break

    return evidence


def gather_external_evidence(prompt, project_root, terms):
    extras = []
    for path in extract_explicit_paths(prompt, project_root):
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


def fallback_summary(prompt, project_root, project_type, evidence_items, error_lines):
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
        f"Gathered {len(evidence_items)} targeted evidence item(s) from the {project_type} project at "
        f"`{project_root}` to support the debugging request."
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


def build_enriched_prompt(user_prompt, bundle):
    parts = [
        "=" * 68,
        "CURATED EVIDENCE PACK FROM SOMA SCOUT",
        "=" * 68,
        "",
        "TASK SUMMARY",
        f"- User request: {user_prompt}",
        f"- Routing decision: {bundle['routing_decision']}",
        f"- Gather reason: {bundle['gather_reason']}",
    ]

    if bundle.get("project_root"):
        parts.extend(
            [
                "",
                "PROJECT CONTEXT",
                f"- Root: {bundle['project_root']}",
                f"- Type: {bundle.get('project_type', 'unknown')}",
                f"- Confidence: {bundle.get('confidence', 0):.2f}",
            ]
        )

    if bundle.get("context_summary"):
        parts.extend(["", "SCOUT SUMMARY", bundle["context_summary"]])

    assumptions = bundle.get("assumptions") or []
    if assumptions:
        parts.extend(["", "ASSUMPTIONS"])
        parts.extend(f"- {item}" for item in assumptions)

    evidence_items = bundle.get("evidence_items") or []
    if evidence_items:
        parts.extend(["", "KEY EVIDENCE"])
        for index, item in enumerate(evidence_items, start=1):
            parts.extend(
                [
                    f"{index}. {item['path']} [{item['kind']}]",
                    f"Reason: {item['reason']}",
                    "Excerpt:",
                    item["preview"] or "[No preview available]",
                    "",
                ]
            )

    error_lines = bundle.get("error_lines") or []
    if error_lines:
        parts.extend(["NORMALIZED ERROR LINES"])
        parts.extend(f"- {line}" for line in error_lines[:MAX_ERROR_LINES])
        parts.append("")

    open_questions = bundle.get("open_questions") or []
    if open_questions:
        parts.extend(["OPEN QUESTIONS"])
        parts.extend(f"- {item}" for item in open_questions)
        parts.append("")

    parts.extend(
        [
            "FINAL REQUEST FOR LOCAL MODEL",
            "Use the evidence above to answer the user's question. Prefer concrete diagnosis, next debugging steps, and cite which evidence drove the conclusion. If evidence is insufficient, say what additional file or log is missing.",
        ]
    )
    return "\n".join(parts).strip()


def bundle_for_direct_pass(prompt, reason, project_root=None):
    return {
        "mode": "gather",
        "original_prompt": prompt,
        "project_root": project_root,
        "project_type": None,
        "routing_decision": "direct_pass_through",
        "gather_reason": reason,
        "confidence": 1.0,
        "gathered_files": {},
        "evidence_items": [],
        "error_lines": [],
        "context_summary": "Prompt was forwarded directly without local evidence gathering.",
        "open_questions": [],
        "assumptions": [],
        "enriched_prompt": prompt,
    }


async def run_gather(user_prompt, project_root, recent_roots_json):
    intent = classify_prompt_intent(user_prompt)
    recent_roots = parse_recent_roots(recent_roots_json)

    if not intent["needs_gather"]:
        print(json.dumps(bundle_for_direct_pass(user_prompt, intent["reason"], project_root)))
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

    project_type, type_reason = detect_project_type(project_root)
    terms = prompt_terms(user_prompt)

    evidence_items = select_evidence(project_root, user_prompt, project_type)
    evidence_items.extend(gather_external_evidence(user_prompt, project_root, terms))
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

    model_summary = await summarize_with_ollama(
        user_prompt, project_root, project_type, evidence_items, error_lines
    )
    summary = fallback_summary(user_prompt, project_root, project_type, evidence_items, error_lines)
    if should_use_model_summary(model_summary):
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
        "gather_reason": intent["reason"],
        "confidence": summary.get("confidence", 0.55),
        "gathered_files": {
            item["path"]: {"tool": item["kind"], "preview": item["preview"][:300]}
            for item in evidence_items
        },
        "evidence_items": evidence_items,
        "error_lines": error_lines,
        "context_summary": summary.get("summary") or "",
        "open_questions": dedupe_strings(summary.get("open_questions") or [])[:3],
        "assumptions": dedupe_strings(summary.get("assumptions") or [])[:4],
    }
    bundle["enriched_prompt"] = build_enriched_prompt(user_prompt, bundle)
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
    args = parser.parse_args()

    if args.mode == "gather":
        asyncio.run(run_gather(args.prompt, args.project_root, args.recent_roots_json))
    else:
        history = []
        try:
            history = json.loads(args.history)
        except Exception:
            pass
        asyncio.run(run_chat(args.prompt, history))
