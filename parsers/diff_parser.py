import yaml

from typing import List, Any
from dataclasses import dataclass

from llm.toolkit.output_normalizer import RawLLMOutput
from llm.toolkit.parsers import OutputParserException
from llm.parsers.yaml_parser import YamlBlockParser

from tests.crash_logger import crash_logger


@dataclass
class DiffOperation:
    operation: str        # 'add', 'modify', or 'conflict'
    target_path: str      # e.g. 'EntityName.fieldName' or 'EntityName'
    current_value: Any    # value before change
    new_value: Any        # value after change

class DiffParser(YamlBlockParser):
    """
    Parses LLM output in strict YAML format under the key 'diff'.
    Extracts a list of DiffOperation objects.
    Inherits from YamlBlockParser for YAML block extraction.
    """
    def __init__(self):
        super().__init__(key="diff")

    @crash_logger("diff_parser")
    def parse(self, raw: RawLLMOutput) -> List[DiffOperation]:
        # Extract the YAML block for 'diff'
        yaml_str = super().parse(raw)
        try:
            data = yaml.safe_load(yaml_str) or {}
        except yaml.YAMLError as e:
            raise OutputParserException(f"Invalid YAML: {e}")

        raw_ops = data.get("diff") or []
        operations: List[DiffOperation] = []
        for entry in raw_ops:
            op = entry.get("operation")
            path = entry.get("target_path")
            curr = entry.get("current_value")
            new = entry.get("new_value")
            # Validate minimal fields
            if not op or not path:
                continue
            operations.append(
                DiffOperation(
                    operation=op,
                    target_path=path,
                    current_value=curr,
                    new_value=new
                )
            )
        return operations
    

class DataModelDiffParser(DiffParser):
    """
    Extends DiffParser to validate DiffOperation.new_value correctness
    and raise human-readable errors on invalid entries.
    """
    @crash_logger("data_model_diff_parser")
    def parse(self, raw: str) -> List[DiffOperation]:
        # Parse base diff list
        ops = super().parse(raw)

        for op in ops:
            target = op.target_path
            if op.operation == 'add':
                nv = op.new_value
                if not isinstance(nv, dict):
                    raise OutputParserException(
                        f"Invalid new_value for 'add' on '{target}': expected object, got {type(nv).__name__}"
                    )
                # Ensure required keys for a new entity
                required = ['description', 'fields', 'relations']
                missing = [k for k in required if k not in nv]
                if missing:
                    raise OutputParserException(
                        f"Missing keys {missing} in new_value for 'add' on '{target}'"
                    )
            elif op.operation == 'modify':
                if op.new_value is None:
                    raise OutputParserException(
                        f"Missing new_value for 'modify' on '{target}'"
                    )
            # conflict operations are informational
        return ops

