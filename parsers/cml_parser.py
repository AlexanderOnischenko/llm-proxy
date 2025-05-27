
import yaml

from typing import List
from domain.models.cml import ContextChange

from llm.toolkit.output_normalizer import RawLLMOutput
from llm.toolkit.parsers import OutputParserException
from llm.parsers.yaml_parser import YamlBlockParser

from utils.crash_logger import crash_logger

class ContextMappingParser(YamlBlockParser):
    def __init__(self):
        super().__init__(key="contexts") 
    """
    Parser for LLM output in strict YAML format mapping features to context changes.
    Inherits from YamlBlockParser to extract the YAML block.
    """
    @crash_logger("bounded_context_parser")
    def parse(self, raw: RawLLMOutput) -> List[ContextChange]:
        # Extract the YAML block
        yaml_str = super().parse(raw)
        try:
            data = yaml.safe_load(yaml_str) or {}
        except yaml.YAMLError as e:
            raise OutputParserException(f"Invalid YAML: {e}")

        contexts = data.get("contexts", [])
        changes: List[ContextChange] = []
        for ctx in contexts:
            name = ctx.get("name")
            operation = ctx.get("operation")
            # Skip invalid or empty entries
            if not name or operation not in ("modify", "new"):
                continue

            change = ContextChange(
                name=name,
                operation=operation,
                usecases=ctx.get("usecases", []),
                aggregates=ctx.get("aggregates", []),
                annotations=ctx.get("annotations", {}),
                upstream=ctx.get("upstream", []),
                downstream=ctx.get("downstream", []),
            )
            changes.append(change)

        # Return empty list if no valid changes
        return changes