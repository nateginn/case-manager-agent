import ollama

client = ollama.Client(timeout=30)

print("=== Test 1: think=False ===")
try:
    resp = client.generate(
        model="glm-4.7-flash:latest",
        prompt="Reply with exactly two words: hello world",
        options={"num_predict": 20},
        think=False,
    )
    print("response:", repr(resp.response))
    print("thinking:", repr(getattr(resp, "thinking", None))[:100] if getattr(resp, "thinking", None) else "none")
    print("done_reason:", resp.done_reason)
except Exception as e:
    print("ERROR:", e)

print()
print("=== Test 2: large num_predict (no think param) ===")
try:
    resp = client.generate(
        model="glm-4.7-flash:latest",
        prompt="Reply with exactly two words: hello world",
        options={"num_predict": 500},
    )
    print("response:", repr(resp.response))
    print("thinking length:", len(getattr(resp, "thinking", "") or ""))
    print("done_reason:", resp.done_reason)
except Exception as e:
    print("ERROR:", e)
