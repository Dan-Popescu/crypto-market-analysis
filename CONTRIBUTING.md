# Conventions

- Install dependencies with `uv sync --frozen --extra dev`, then run
  `uv run --frozen python tools/check.py` on the PC or in the cloud workspace.
- Include verification results for the final code version in each pull request.
  GitHub Actions is intentionally disabled; checks run in the working environment.
- Keep provider parsing, storage, research, visualization and transports separate.
- Any API access must be public and bounded. No paid API dependency, trading credentials or order execution.
- Never mix native quote currencies, synthetic and real modes, or actual and projected funding.
- Time is UTC epoch seconds internally. Distinguish event time from ingestion availability.
- A missing value remains missing. Report unsupported capabilities and partial source coverage.
- Financial estimates require chronological validation and explicit experimental/insufficient-data states.
- Test observable interfaces with independent examples, including look-ahead, duplicates, partial days and failures.
- Do not add credentials, databases, build outputs or virtual environments to source control.
