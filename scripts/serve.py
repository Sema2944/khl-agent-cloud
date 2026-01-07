# scripts/serve.py
from __future__ import annotations

import os
import uvicorn


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    uvicorn.run(
        "src.service:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
