---
name: python-app-test
description: This app is used for testing passing environment variables and user input prompts in Python scripts. It demonstrates how to run a Python script that requires user interaction and environment variables using the `uv` tool.
metadata: {
  environment_variables: ["TEST_ENV"],
}
---

## How to run

This script requires user input.

```bash
uv run --with python-dotenv scripts/app.py
```

