# Repository Guidelines

## Project Structure & Module Organization
This repository is currently unscaffolded: there is no application code, test suite, or build configuration in the root yet. Keep top-level files limited to shared project metadata such as `README.md`, `AGENTS.md`, and `.gitignore`. As code is added, use a predictable layout:

- `src/` for application or library code
- `tests/` for automated tests mirroring `src/`
- `assets/` for static files
- `scripts/` for repeatable developer or CI utilities

If you introduce a new top-level directory, document its purpose in `README.md`.

## Build, Test, and Development Commands
No build, test, or local-run commands are configured yet. When bootstrapping the project, expose a small set of root-level commands and keep them stable. Preferred examples:

- `make dev` or `npm run dev` for local development
- `make test` or `npm test` for the full test suite
- `make lint` or `npm run lint` for static checks

Document the chosen command set in `README.md` and make CI use the same entry points.

## Coding Style & Naming Conventions
Match conventions to the primary language once it is selected, but keep naming consistent from the start. Use `kebab-case` for shell scripts and folder names, `snake_case` for Python modules, and `PascalCase` for classes or UI components. Use 2-space indentation for JSON, YAML, and JavaScript/TypeScript; use 4 spaces for Python. Add formatter and linter configs early and run them before opening a PR.

## Testing Guidelines
Place tests under `tests/` and keep file names framework-appropriate, such as `*.test.ts`, `*.spec.ts`, or `test_*.py`. New features should ship with tests, and bug fixes should include a regression test. If you add a test runner, document coverage expectations and the exact command used to execute tests locally.

## Commit & Pull Request Guidelines
There is no git history in this workspace yet, so no repository-specific commit style is established. Use short, imperative commit messages; Conventional Commit prefixes such as `feat:`, `fix:`, and `docs:` are preferred. Pull requests should summarize scope, list setup or config changes, link related issues, and include screenshots or terminal output when behavior changes are visible.

## Security & Configuration Tips
Do not commit secrets, local credentials, or machine-specific config. Add runtime variables to `.env.example` when introduced, and ignore real `.env` files by default.
