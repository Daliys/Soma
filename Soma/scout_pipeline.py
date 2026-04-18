#!/usr/bin/env python3
import asyncio
import json
import sys
import urllib.request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Define the model and directories
MODEL = "llama3.2:3b"
# Expanding allowed directories to give comprehensive access as requested
import os
# Only include directories that actually exist to prevent MCP server from crashing
ALLOWED_DIRS = [d for d in ["/Users/daliys", "/", "/Users/daliys/Downloads", "/Users/daliys/Daliys/Swift/Soma"] if os.path.exists(d)]
if not ALLOWED_DIRS:
    ALLOWED_DIRS = ["/"] # Fallback if none exist

def extract_tool_calls(content):
    import re
    tool_calls = []

    # Method 1: Look for regex matches of {"name": "...", "arguments": {...}} or {"name": "...", "parameters": {...}}
    matches = re.finditer(r'\{[^{}]*"name"\s*:\s*"(?P<name>\w+)"[^{}]*"(?:parameters|arguments)"\s*:\s*(?P<params>\{[^{}]*\})[^{}]*\}', content, re.DOTALL)
    for match in matches:
        name = match.group("name")
        params_str = match.group("params")
        try:
            params = json.loads(params_str)
            tool_calls.append({"id": "call_fallback", "function": {"name": name, "arguments": params}})
        except:
            pass

    if tool_calls: return tool_calls

    # Method 2: Code blocks
    json_blocks = re.findall(r'```(?:json)?\n(.*?)\n```', content, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "name" in data:
                args = data.get("arguments") or data.get("parameters") or {}
                tool_calls.append({"id": "call_fallback", "function": {"name": data["name"], "arguments": args}})
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item:
                        args = item.get("arguments") or item.get("parameters") or {}
                        tool_calls.append({"id": "call_fallback", "function": {"name": item["name"], "arguments": args}})
        except:
            pass

    if tool_calls: return tool_calls

    # Method 3: Bare JSON block spanning the text
    try:
        start = content.find('{')
        end = content.rfind('}')
        if start != -1 and end != -1 and start < end:
            data = json.loads(content[start:end+1])
            if isinstance(data, dict) and "name" in data:
                args = data.get("arguments") or data.get("parameters") or {}
                # Sometimes Llama emits the path argument at the top level
                if not args and "path" in data:
                    args = {"path": data["path"]}
                tool_calls.append({"id": "call_fallback", "function": {"name": data["name"], "arguments": args}})
    except:
        pass

    return tool_calls

async def query_ollama(messages, tools=None):
    url = "http://localhost:11434/api/chat"
    data = {
        "model": MODEL,
        "messages": messages,
        "stream": False
    }
    if tools:
        data["tools"] = tools
        
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), 
                                 headers={'Content-Type': 'application/json'})
    
    try:
        # Use a longer timeout for LLM generation
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return {"error": f"Ollama Error: {e}"}

async def run_scout():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: scout_pipeline.py <prompt> [history_json]"}))
        return

    user_prompt = sys.argv[1]
    
    # Load history if provided
    history = []
    if len(sys.argv) > 2:
        try:
            history = json.loads(sys.argv[2])
        except:
            pass
    
    # Define the Scout Persona - less eager, more precise
    system_msg = {
        "role": "system", 
        "content": """You are Soma, a highly capable local AI scout with full access to the user's filesystem.
Your primary job is to find, list, read, and analyze files based on the user's requests.
- When asked to list, look, find, or explore, ALWAYS use the 'list_directory' tool with a path (e.g., "/Users/daliys/Downloads" or "/").
- When asked to read, summarize, or analyze a file, ALWAYS use the 'read_file' tool with the exact absolute path.
- YOU MUST output tool calls in valid JSON format! Example:
```json
{"name": "list_directory", "arguments": {"path": "/Users/daliys"}}
```
or
```json
{"name": "read_file", "arguments": {"path": "/Users/daliys/Downloads/file.txt"}}
```
- If you can't find a file, don't hallucinate; first use `list_directory` to look around."""
    }
    
    # Prepend system message and combine with history
    messages = [system_msg] + history + [{"role": "user", "content": user_prompt}]

    # Define the MCP server parameters (Filesystem server)
    # We will use 'npx' from the PATH instead of hardcoding the user's local path
    # in case this script runs on a different environment
    import shutil
    npx_path = shutil.which("npx") or "npx"
    server_params = StdioServerParameters(
        command=npx_path,
        args=["-y", "@modelcontextprotocol/server-filesystem"] + ALLOWED_DIRS
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # Fetch tools and convert to Ollama format
                tools_resp = await session.list_tools()
                ollama_tools = []
                for t in tools_resp.tools:
                    ollama_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.inputSchema
                        }
                    })

                # --- Chat Loop with Tool Calling ---
                ollama_resp = await query_ollama(messages, ollama_tools)
                
                if "error" in ollama_resp:
                    print(json.dumps(ollama_resp))
                    return

                assistant_msg = ollama_resp.get("message", {})
                content = assistant_msg.get("content", "")
                tool_calls = assistant_msg.get("tool_calls", [])

                # FALLBACK: Llama 3.2:3b often puts tool calls in 'content' as text JSON
                if not tool_calls and "{" in content:
                    tool_calls = extract_tool_calls(content)

                if tool_calls:
                    # Append assistant's request to messages
                    messages.append(assistant_msg)
                    
                    # Execute each tool call
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        tool_args = tc["function"]["arguments"]
                        tool_call_id = tc.get("id", "call_default")
                        
                        try:
                            # Path Correction Logic
                            import os
                            if "path" in tool_args:
                                path = tool_args["path"]
                                # If it's just a filename, try to find it
                                if not path.startswith("/"):
                                    for root in ALLOWED_DIRS:
                                        test_path = os.path.join(root, path)
                                        if os.path.exists(test_path):
                                            tool_args["path"] = test_path
                                            break
                                    else:
                                        # Default to Downloads if not found
                                        tool_args["path"] = os.path.join(ALLOWED_DIRS[0], path)

                            tool_result = await session.call_tool(tool_name, tool_args)
                            
                            content_str = ""
                            if hasattr(tool_result, 'content'):
                                content_str = "\n".join([c.text for c in tool_result.content if hasattr(c, 'text')])
                            
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": content_str
                            })
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": f"Error executing tool: {e}"
                            })

                    # Final call to Ollama
                    final_resp = await query_ollama(messages)
                    if "error" in final_resp:
                        print(json.dumps(final_resp))
                    else:
                        print(json.dumps({
                            "response": final_resp["message"]["content"],
                            "history": messages + [final_resp["message"]]
                        }))
                else:
                    # No tool calls
                    print(json.dumps({
                        "response": content,
                        "history": messages + [assistant_msg]
                    }))

    except Exception as e:
        print(json.dumps({"error": f"MCP Client Error: {e}"}))

if __name__ == "__main__":
    asyncio.run(run_scout())
