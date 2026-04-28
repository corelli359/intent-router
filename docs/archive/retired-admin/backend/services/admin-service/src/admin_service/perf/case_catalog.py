from __future__ import annotations

import json
from importlib import resources

from admin_service.perf.models import PerfTestCaseDefinition, PerfTestCaseNotFoundError


class PerfTestCaseCatalog:
    def __init__(self, cases: list[PerfTestCaseDefinition]) -> None:
        self._cases_by_id = {case.case_id: case for case in cases}

    @classmethod
    def from_default_resource(cls) -> "PerfTestCaseCatalog":
        raw_payload = (
            resources.files("admin_service.perf.cases")
            .joinpath("default_cases.json")
            .read_text(encoding="utf-8")
        )
        payload = json.loads(raw_payload)
        if not isinstance(payload, list):
            raise RuntimeError("admin perf test cases resource must be a JSON array")
        return cls([PerfTestCaseDefinition.model_validate(item) for item in payload])

    def list_cases(self) -> list[PerfTestCaseDefinition]:
        return [
            self._cases_by_id[case_id].model_copy(deep=True)
            for case_id in sorted(self._cases_by_id)
        ]

    def get_case(self, case_id: str) -> PerfTestCaseDefinition:
        case = self._cases_by_id.get(case_id)
        if case is None:
            raise PerfTestCaseNotFoundError(f"perf test case not found: {case_id}")
        return case.model_copy(deep=True)
