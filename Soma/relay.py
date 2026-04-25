#!/usr/bin/env python3
"""
relay.py — Local Relay Connector
Reads the gather bundle from scout_pipeline.py and sends either the direct
prompt or the curated evidence pack to the local Ollama model.
"""
import json
import os
import sys
import urllib.request

MODEL = os.environ.get("SOMA_LOCAL_MODEL", "qwen3:4b")
MAX_PROMPT_CHARS = 28_000


def collect_files_used(bundle):
    evidence_items = bundle.get("evidence_items") or []
    if evidence_items:
        return [item.get("path") for item in evidence_items if item.get("path")]
    return list((bundle.get("gathered_files") or {}).keys())


def query_ollama(prompt: str) -> dict:
    # Use a system prompt to discourage verbose internal reasoning (Thinking)
    # and provide options to stabilize performance.
    payload = {
        "model": MODEL,
        "think": False,
        "messages": [
            {"role": "system", "content": "You are a concise engineering assistant. Use the compact evidence packet first. If evidence is insufficient, request only 1-3 exact missing files or commands. Do not add verbose reasoning."},
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "options": {
            "num_predict": 1024,
            "temperature": 0.3,
        }
    }
    request = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except Exception as exc:
        return {"error": str(exc)}


def relay(bundle_json_str: str) -> dict:
    try:
        bundle = json.loads(bundle_json_str)
    except Exception as exc:
        return {"error": f"Invalid bundle JSON: {exc}"}

    if "error" in bundle:
        return {"error": bundle["error"]}

    enriched = bundle.get("codex_packet") or bundle.get("enriched_prompt") or bundle.get("original_prompt", "")
    if not enriched:
        return {"error": "Bundle contains no prompt to relay"}

    if len(enriched) > MAX_PROMPT_CHARS:
        enriched = enriched[:MAX_PROMPT_CHARS] + "\n\n[... context truncated for length ...]"

    result = query_ollama(enriched)
    if "error" in result:
        return {"error": f"Local relay failed: {result['error']}"}

    message = result.get("message", {})
    response_text = message.get("content", "").strip()
    if not response_text:
        return {"error": "Local relay returned an empty response"}

    return {
        "response": response_text,
        "source": "llama_local",
        "model": MODEL,
        "routing_decision": bundle.get("routing_decision"),
        "enriched_prompt": enriched,
        "codex_packet": enriched,
        "estimated_tokens": bundle.get("estimated_tokens"),
        "files_used": collect_files_used(bundle),
        "errors_found": len(bundle.get("error_lines") or []),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: relay.py '<bundle_json>'"}))
        sys.exit(1)
    print(json.dumps(relay(sys.argv[1])))
