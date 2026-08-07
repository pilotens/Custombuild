from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from .models import TemplateDefinition


@lru_cache(maxsize=1)
def load_bookcase_template() -> TemplateDefinition:
    resource = files("custombuild_templates.data").joinpath("bookcase.v1.json")
    return TemplateDefinition.model_validate(json.loads(resource.read_text(encoding="utf-8")))
