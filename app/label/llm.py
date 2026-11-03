"""Small structured-output helper with per-stage model choice and token accounting."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Labelling is classification over a small context: a mid-size model at medium effort
# is plenty, and is an order of magnitude cheaper than the old Opus/high pipeline.
LABEL_MODEL = os.environ.get("ANTHROPIC_MODEL_LABEL", "claude-sonnet-5-5")
LABEL_EFFORT = os.environ.get("ANTHROPIC_EFFORT_LABEL", "medium")

SYSTEM = (
    "You label structural-model data for an Australian residential structural engineer "
    "(Class 1 and 10 buildings, NCC and Australian Standards). You are given geometry "
    "that has already been measured from the architectural PDF; do not re-measure or "
    "re-draw it. Answer only the questions asked, using the IDs given. When the documents "
    "do not settle a question, say so in the reason and give your best engineering "
    "judgement with low confidence rather than inventing detail."
)


@dataclass
class Usage:
    """Token counts for one call."""

    label: str
    model: str
    input_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output_tokens: int = 0


@dataclass
class UsageTotals:
    """Accumulates every call made for a job; shown in the UI and saved in review.json."""

    calls: list[Usage] = field(default_factory=list)

    def add(self, u: Usage) -> None:
        self.calls.append(u)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens + c.cache_write + c.cache_read for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    def as_dict(self) -> dict:
        return {
            "calls": [c.__dict__ for c in self.calls],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class LabelError(RuntimeError):
    pass


def image_block(png: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(png).decode()},
    }


def pdf_block(pdf: bytes, title: str) -> dict:
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf).decode()},
        "title": title,
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _client():
    import anthropic  # imported lazily so the deterministic path never needs the SDK

    return anthropic.Anthropic()


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in response")
    return text[start : end + 1]


def call_structured(
    content: list[dict],
    out: type[T],
    label: str,
    totals: UsageTotals,
    max_tokens: int = 8000,
    model: str = LABEL_MODEL,
    effort: str = LABEL_EFFORT,
) -> T:
    """One structured-output request validated into `out`.

    Falls back to plain JSON (validated locally) if the API rejects the schema.
    """
    import anthropic

    client = _client()
    schema = out.model_json_schema()
    messages = [{"role": "user", "content": content}]

    def request(msgs: list[dict], with_schema: bool) -> str:
        output_config: dict = {"effort": effort}
        if with_schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            output_config=output_config,
            messages=msgs,
        ) as stream:
            message = stream.get_final_message()
        u = message.usage
        usage = Usage(
            label=label,
            model=message.model,
            input_tokens=u.input_tokens,
            cache_write=u.cache_creation_input_tokens or 0,
            cache_read=u.cache_read_input_tokens or 0,
            output_tokens=u.output_tokens,
        )
        totals.add(usage)
        log.info("Claude usage %s", usage)
        if message.stop_reason == "refusal":
            raise LabelError(f"Claude declined the request ({label}).")
        if message.stop_reason == "max_tokens":
            raise LabelError(f"Claude's response was cut off ({label}).")
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if not text:
            raise LabelError(f"Claude returned no text ({label}).")
        return text

    try:
        return out.model_validate_json(request(messages, True))
    except anthropic.BadRequestError as e:
        if "grammar" not in str(e).lower() and "schema" not in str(e).lower():
            raise

    instruction = (
        "Respond with ONLY a single JSON object (no prose, no code fences) that validates "
        "against this JSON schema. Every property is required.\n" + json.dumps(schema)
    )
    messages = [{"role": "user", "content": [*content, text_block(instruction)]}]
    last_error: Exception | None = None
    for _ in range(2):
        text = request(messages, False)
        try:
            return out.model_validate_json(_extract_json(text))
        except (ValueError, ValidationError) as err:
            last_error = err
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That JSON was invalid: {err}. Reply with the corrected JSON object only."},
            ]
    raise LabelError(f"Claude's JSON did not match the expected structure: {last_error}")


def api_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
