import os
print("=== Embedding API ===")
for k in ["AI_BANK_MODEL_BASE_URL", "AI_BANK_MODEL_API_KEY", "AI_BANK_MODEL_API_KEY_FILE",
          "AI_BANK_EMBEDDING_MODEL", "AI_BANK_SEMANTIC_RECALL_SOURCE",
          "AI_BANK_SEMANTIC_RECALL_THRESHOLD", "AI_BANK_ONTO_BASE_URL"]:
    v = os.getenv(k, "")
    if v:
        print(f"  {k}: {v[:30]}..." if len(v) > 30 else f"  {k}: {v}")
    else:
        print(f"  {k}: (not set)")

print("\n=== LLM API ===")
for k in ["AI_BANK_LLM_BASE_URL", "AI_BANK_LLM_API_KEY", "AI_BANK_LLM_MODEL",
          "OPENAI_API_KEY", "OPENAI_BASE_URL", "DASHSCOPE_API_KEY",
          "AI_BANK_CHAT_MODEL", "AI_BANK_CHAT_BASE_URL"]:
    v = os.getenv(k, "")
    if v:
        print(f"  {k}: {v[:30]}..." if len(v) > 30 else f"  {k}: {v}")
    else:
        print(f"  {k}: (not set)")

print("\n=== .env files ===")
from pathlib import Path
for p in [Path(".env"), Path("../.env"), Path("../../.env")]:
    if p.exists():
        print(f"  Found: {p.resolve()}")
        lines = p.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if any(k in line.upper() for k in ["EMBEDDING", "LLM", "MODEL_BASE", "API_KEY", "CHAT"]):
                # mask values
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    if len(val) > 10:
                        print(f"    {key.strip()}={val[:10]}...")
                    else:
                        print(f"    {line}")
