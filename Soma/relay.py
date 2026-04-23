#!/usr/bin/env python3
"""
relay.py — Local Relay Connector
Reads the gather bundle from scout_pipeline.py and sends either the direct
prompt or the curated evidence pack to the local Ollama model.
"""
import json
import sys
import urllib.request

MODEL = "llama3.2:3b"
MAX_PROMPT_CHARS = 120_000


def collect_files_used(bundle):
    evidence_items = bundle.get("evidence_items") or []
    if evidence_items:
        return [item.get("path") for item in evidence_items if item.get("path")]
    return list((bundle.get("gathered_files") or {}).keys())


def query_ollama(prompt: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
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

    enriched = bundle.get("enriched_prompt") or bundle.get("original_prompt", "")
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
        "routing_decision": bundle.get("routing_decision"),
        "enriched_prompt": enriched,
        "files_used": collect_files_used(bundle),
        "errors_found": len(bundle.get("error_lines") or []),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: relay.py '<bundle_json>'"}))
        sys.exit(1)
    print(json.dumps(relay(sys.argv[1])))
