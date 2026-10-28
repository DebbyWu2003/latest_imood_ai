"""Streaming llama.cpp client (OpenAI-style SSE); strips Qwen3 think blocks."""
import json
import urllib.request


def llm_deltas(CFG, user_text):
    """Stream the reply for user_text: persona as system prompt, temperature / top_p / max_tokens from the config; yields text deltas."""
    body = {"messages": [{"role": "system", "content": CFG["persona"]}, {"role": "user", "content": user_text}],
            "stream": True, "temperature": CFG["llm"]["temperature"], "top_p": CFG["llm"]["top_p"],
            "max_tokens": CFG["llm"]["max_tokens"]}
    req = urllib.request.Request(CFG["llm"]["url"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    in_think = False
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
            # never forward a (possibly empty) Qwen3 think block
            while d:
                if in_think:
                    i = d.find("</think>")
                    if i < 0: d = ""; break
                    d = d[i + 8:]; in_think = False
                else:
                    i = d.find("<think>")
                    if i < 0: yield d; break
                    if i: yield d[:i]
                    d = d[i + 7:]; in_think = True
