"""Shared transcript-entry factories for the hook contract tests."""


def user_text(text, sidechain=False):
    return {"type": "user", "isSidechain": sidechain,
            "message": {"role": "user", "content": text}}


def tool_use(name, tool_input, tool_use_id=None, sidechain=False):
    block = {"type": "tool_use", "name": name, "input": tool_input}
    if tool_use_id is not None:
        block["id"] = tool_use_id
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"role": "assistant", "content": [block]}}


def write_call(file_path, tool_use_id="toolu_write"):
    return tool_use("Write", {"file_path": file_path, "content": "x"},
                    tool_use_id=tool_use_id)


def tool_result(tool_use_id="toolu_write", is_error=False):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                         "content": "ok", "is_error": is_error}],
        },
    }


def assistant_usage(ctx_tokens):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": ctx_tokens, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 10},
        },
    }


# --- Codex rollout fixtures (payload-envelope schema) ---

def codex_user_message(text):
    return {"type": "event_msg", "timestamp": "2026-07-20T22:51:00.000Z",
            "payload": {"type": "user_message", "message": text,
                        "images": [], "local_images": [], "text_elements": []}}


def codex_custom_tool_call(name, input_text, call_id="call_codex"):
    return {"type": "response_item", "timestamp": "2026-07-20T22:51:01.000Z",
            "payload": {"type": "custom_tool_call", "id": "ctc_x", "status": "completed",
                        "call_id": call_id, "name": name, "input": input_text}}


def codex_function_call(name, arguments, call_id="call_codex_fn"):
    return {"type": "response_item", "timestamp": "2026-07-20T22:51:02.000Z",
            "payload": {"type": "function_call", "id": "fc_x", "call_id": call_id,
                        "name": name, "arguments": arguments}}


def codex_tool_output(call_id="call_codex"):
    return {"type": "response_item", "timestamp": "2026-07-20T22:51:03.000Z",
            "payload": {"type": "custom_tool_call_output", "id": "ctco_x",
                        "call_id": call_id,
                        "output": [{"type": "input_text", "text": "ok"}]}}


def codex_token_count(input_tokens, cached=0):
    return {"type": "event_msg", "timestamp": "2026-07-20T22:51:04.000Z",
            "payload": {"type": "token_count",
                        "info": {"last_token_usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": cached,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 10, "total_tokens": input_tokens + 10},
                            "model_context_window": 258400},
                        "rate_limits": {}}}
