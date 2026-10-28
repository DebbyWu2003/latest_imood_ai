"""Streaming llama.cpp client (OpenAI-style SSE); strips Qwen3 think blocks."""
import json
import urllib.request


def llm_deltas(config, user_text):
    """Stream the reply for user_text: persona as system prompt, temperature / top_p / max_tokens from the config; yields text deltas."""
    body = {"messages": [{"role": "system", "content": config["persona"]}, {"role": "user", "content": user_text}],
            "stream": True, "temperature": config["llm"]["temperature"], "top_p": config["llm"]["top_p"],
            "max_tokens": config["llm"]["max_tokens"]}
    request = urllib.request.Request(config["llm"]["url"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    in_think = False
    with urllib.request.urlopen(request) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            content = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            # never forward a (possibly empty) Qwen3 think block
            while content:
                if in_think:
                    tag_index = content.find("</think>")
                    if tag_index < 0:
                        content = ""
                        break
                    content = content[tag_index + 8:]
                    in_think = False
                else:
                    tag_index = content.find("<think>")
                    if tag_index < 0:
                        yield content
                        break
                    if tag_index:
                        yield content[:tag_index]
                    content = content[tag_index + 7:]
                    in_think = True
