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
