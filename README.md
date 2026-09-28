# OncoGemma v6

OncoGemma is an AI copilot that assists pathologists in grading H&E breast-cancer whole-slide images (WSIs).

v6 status: in progress; accuracy not yet measured. See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Documentation & Guidelines

- Technical Specifications: [docs/specs/00-program-overview.md](docs/specs/00-program-overview.md)
- Contributor and Agent Working Rules: [AGENTS.md](AGENTS.md)
- Session Status: [docs/STATUS.md](docs/STATUS.md)

## Development

The development environment is Windows PowerShell.

```powershell
# Run backend tests (from repo root; uses offline mocks and in-memory SQLite DB)
python -m pytest backend/tests -q -p no:cacheprovider

# Run targeted evaluation tests
python -m pytest backend/tests/eval -q -p no:cacheprovider

# Frontend build and type check
cd frontend; npm ci; npx tsc --noEmit; npm run build
```

## License

This project is licensed under the Apache License, Version 2.0 - see the [LICENSE](LICENSE) file for details.
