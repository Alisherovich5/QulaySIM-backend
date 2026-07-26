from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

# Money is a Decimal everywhere inside the service. The storefront's TypeScript
# contract declares these fields as `number`, so they are serialised as floats
# at the boundary only — never used for arithmetic in that form.
Money = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# Cached / localised payloads are plain JSON, not ORM rows: they round-trip
# through Redis, so they are dicts by the time a router returns them.
type JSONDict = dict[str, Any]
type JSONList = list[JSONDict]
