#!/usr/bin/env python3
"""
benchmark_ollama.py

Benchmarks Soma's current Ollama paths and records wall time plus Ollama timing
metadata so local model regressions are visible.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import scout_pipeline

DEFAULT_MODEL = "qwen3:4b"
MAX_PROMPT_CHARS = 28_000
RELAY_SYSTEM_PROMPT = (
    "You are a concise engineering assistant. Answer directly without verbose "
    "internal reasoning or 'thinking' blocks unless specifically requested."
)


def ollama_chat(model, messages, *, num_predict, temperature=0.3, timeout=180):
    payload = {
        "model": model,
        "think": False,
        "messages": messages,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    wall_seconds = time.perf_counter() - started

    message = data.get("message", {})
    return {
        "wall_seconds": round(wall_seconds, 3),
        "done_reason": data.get("done_reason"),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "prompt_eval_seconds": round((data.get("prompt_eval_duration") or 0) / 1_000_000_000, 3),
        "eval_count": data.get("eval_count"),
        "eval_seconds": round((data.get("eval_duration") or 0) / 1_000_000_000, 3),
        "total_seconds_reported": round((data.get("total_duration") or 0) / 1_000_000_000, 3),
        "load_seconds": round((data.get("load_duration") or 0) / 1_000_000_000, 3),
        "content_chars": len(message.get("content", "")),
        "thinking_chars": len(message.get("thinking", "")),
        "content_preview": (message.get("content", "") or "")[:240],
    }


def build_trimmed_bundle(bundle):
    trimmed = dict(bundle)
    trimmed["git_diff"] = None
    trimmed["git_diff_summary"] = bundle.get("git_diff_summary")
    trimmed["evidence_items"] = (bundle.get("evidence_items") or [])[:3]
    trimmed["gathered_files"] = {
        item["path"]: {"tool": item["kind"], "preview": (item["preview"] or "")[:300]}
        for item in trimmed["evidence_items"]
        if item.get("path")
    }
    trimmed["enriched_prompt"] = scout_pipeline.build_enriched_prompt(
        trimmed.get("original_prompt", ""), trimmed
    )
    trimmed["codex_packet"] = trimmed["enriched_prompt"]
    trimmed["estimated_tokens"] = scout_pipeline.estimate_tokens(trimmed["codex_packet"])
    return trimmed


async def run_gather(prompt, project_root, analysis_depth):
    started = time.perf_counter()
    recent_roots_json = json.dumps([project_root])
    script_path = str(Path(__file__).with_name("scout_pipeline.py"))
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    bundle = await asyncio.to_thread(
        lambda: json.loads(
            subprocess.run(
                [
                    sys.executable,
                    script_path,
                    prompt,
                    "--mode",
                    "gather",
                    "--project-root",
                    project_root,
                    "--recent-roots-json",
                    recent_roots_json,
                    "--token-budget",
                    "balanced",
                    "--analysis-depth",
                    analysis_depth,
                ],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            ).stdout
        )
    )
    return round(time.perf_counter() - started, 3), bundle


async def benchmark(model, prompt, project_root, analysis_depth):
    gather_seconds, bundle = await run_gather(prompt, project_root, analysis_depth)
    enriched_prompt = bundle.get("codex_packet") or bundle.get("enriched_prompt", "")
    trimmed_bundle = build_trimmed_bundle(bundle)
    trimmed_prompt = trimmed_bundle.get("enriched_prompt", "")

    tiny = ollama_chat(
        model,
        [
            {"role": "system", "content": "You are a concise engineering assistant."},
            {"role": "user", "content": "Say hello in one short sentence."},
        ],
        num_predict=32,
        timeout=60,
    )

    relay_full = ollama_chat(
        model,
        [
            {"role": "system", "content": RELAY_SYSTEM_PROMPT},
            {"role": "user", "content": enriched_prompt[:MAX_PROMPT_CHARS]},
        ],
        num_predict=128,
    )

    relay_trimmed = ollama_chat(
        model,
        [
            {"role": "system", "content": RELAY_SYSTEM_PROMPT},
            {"role": "user", "content": trimmed_prompt[:MAX_PROMPT_CHARS]},
        ],
        num_predict=128,
    )

    return {
        "model": model,
        "project_root": project_root,
        "prompt": prompt,
        "gather": {
            "wall_seconds": gather_seconds,
            "routing_decision": bundle.get("routing_decision"),
            "packet_mode": bundle.get("packet_mode"),
            "analysis_depth": bundle.get("analysis_depth"),
            "analysis_stages": bundle.get("analysis_stages"),
            "confidence": bundle.get("confidence"),
            "evidence_count": len(bundle.get("evidence_items") or []),
            "error_line_count": len(bundle.get("error_lines") or []),
            "git_diff_chars": len(bundle.get("git_diff") or ""),
            "raw_git_diff_chars_omitted": (bundle.get("git_diff_summary") or {}).get("raw_diff_chars_omitted"),
            "enriched_prompt_chars": len(enriched_prompt),
            "estimated_tokens": bundle.get("estimated_tokens"),
        },
        "tiny_prompt": tiny,
        "relay_full": {
            "prompt_chars": len(enriched_prompt),
            **relay_full,
        },
        "relay_trimmed": {
            "prompt_chars": len(trimmed_prompt),
            **relay_trimmed,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--prompt",
        default="The relay sometimes takes around 90 seconds. Check logs, git diff, and likely causes in this project.",
    )
    parser.add_argument(
        "--analysis-depth",
        default="deterministic",
        choices=sorted(scout_pipeline.ANALYSIS_DEPTHS),
    )
    args = parser.parse_args()

    report = asyncio.run(benchmark(args.model, args.prompt, str(Path(args.project_root).resolve()), args.analysis_depth))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
