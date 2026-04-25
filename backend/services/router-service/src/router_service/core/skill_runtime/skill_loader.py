from __future__ import annotations

from pathlib import Path
from typing import Any

from router_service.core.skill_runtime.markdown import (
    extract_heading_section,
    extract_json_machine_spec,
    parse_frontmatter,
)
from router_service.core.skill_runtime.models import (
    MarkdownDocument,
    SkillIndexEntry,
    SkillSpec,
    SlotDefinition,
    StepDefinition,
)


DEFAULT_SPEC_ROOT = Path(__file__).resolve().parent / "default_specs"


class SkillSpecLoader:
    """Filesystem-backed markdown Skill loader."""

    def __init__(self, spec_root: str | Path | None = None) -> None:
        self.spec_root = Path(spec_root).expanduser().resolve() if spec_root else DEFAULT_SPEC_ROOT.resolve()
        self._doc_cache: dict[Path, MarkdownDocument] = {}
        self._skill_cache: dict[str, SkillSpec] = {}

    def load_agent_document(self) -> MarkdownDocument:
        return self.load_document("agent.md")

    def load_agent_policy(self) -> dict[str, Any]:
        return extract_json_machine_spec(self.load_agent_document().body)

    def load_document(self, relative_path: str) -> MarkdownDocument:
        path = self._resolve_relative_path(relative_path)
        if path not in self._doc_cache:
            raw = path.read_text(encoding="utf-8")
            metadata, body = parse_frontmatter(raw)
            self._doc_cache[path] = MarkdownDocument(
                path=str(path.relative_to(self.spec_root)),
                metadata=metadata,
                body=body,
            )
        return self._doc_cache[path]

    def load_skill_index(self) -> list[SkillIndexEntry]:
        skills_dir = self.spec_root / "skills"
        entries: list[SkillIndexEntry] = []
        for path in sorted(skills_dir.glob("*.md")):
            doc = self.load_document(str(path.relative_to(self.spec_root)))
            metadata = doc.metadata
            machine = extract_json_machine_spec(doc.body)
            skill_id = str(metadata.get("skill_id") or path.stem)
            keywords = tuple(str(item) for item in metadata.get("keywords", []) if str(item))
            description = str(
                machine.get("description")
                or extract_heading_section(doc.body, "Candidate Card")
                or metadata.get("description")
                or ""
            ).strip()
            entries.append(
                SkillIndexEntry(
                    skill_id=skill_id,
                    name=str(metadata.get("name") or skill_id),
                    version=str(metadata.get("version") or "0.0.0"),
                    status=str(metadata.get("status") or "inactive"),
                    description=description,
                    keywords=keywords,
                    risk_level=str(metadata.get("risk_level") or "low"),
                    path=doc.path,
                )
            )
        return entries

    def load_skill(self, skill_id: str) -> SkillSpec:
        if skill_id in self._skill_cache:
            return self._skill_cache[skill_id]
        entry = next((item for item in self.load_skill_index() if item.skill_id == skill_id), None)
        if entry is None:
            raise ValueError(f"unknown skill: {skill_id}")

        doc = self.load_document(entry.path)
        machine = extract_json_machine_spec(doc.body)
        slots = tuple(_parse_slot(item) for item in machine.get("slots", []))
        steps = tuple(_parse_step(item) for item in machine.get("steps", []))
        references = tuple(str(item) for item in machine.get("references", []))
        declared_capabilities = tuple(str(item) for item in machine.get("allowed_capabilities", []))
        step_capabilities = tuple(
            str(step.config["capability"])
            for step in steps
            if step.kind == "api_call" and step.config.get("capability")
        )
        exception_messages = {
            str(key): str(value)
            for key, value in dict(machine.get("exception_messages", {})).items()
        }
        spec = SkillSpec(
            index=entry,
            slots=slots,
            steps=steps,
            references=references,
            allowed_capabilities=declared_capabilities or step_capabilities,
            exception_messages=exception_messages,
            raw_body=doc.body,
        )
        self._skill_cache[skill_id] = spec
        return spec

    def read_reference(self, relative_path: str) -> MarkdownDocument:
        return self.load_document(f"skills/{relative_path}" if relative_path.startswith("references/") else relative_path)

    def _resolve_relative_path(self, relative_path: str) -> Path:
        candidate = (self.spec_root / relative_path).resolve()
        if self.spec_root not in candidate.parents and candidate != self.spec_root:
            raise ValueError(f"path escapes skill spec root: {relative_path}")
        if not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        return candidate


def _parse_slot(item: object) -> SlotDefinition:
    if not isinstance(item, dict):
        raise ValueError("slot entries must be objects")
    name = str(item.get("name") or "")
    if not name:
        raise ValueError("slot name is required")
    return SlotDefinition(
        name=name,
        required=bool(item.get("required", True)),
        prompt=str(item.get("prompt") or f"请补充{name}。"),
        description=str(item.get("description") or ""),
        extractor=dict(item.get("extractor") or {}),
    )


def _parse_step(item: object) -> StepDefinition:
    if not isinstance(item, dict):
        raise ValueError("step entries must be objects")
    step_id = item.get("id")
    if not isinstance(step_id, int):
        raise ValueError("step id must be an integer")
    kind = str(item.get("type") or "")
    if not kind:
        raise ValueError("step type is required")
    return StepDefinition(
        step_id=step_id,
        kind=kind,
        config={str(key): value for key, value in item.items() if key not in {"id", "type"}},
    )
