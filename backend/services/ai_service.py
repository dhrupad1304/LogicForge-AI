
import json
import re
import time
from typing import Callable, Optional
import urllib.error
import urllib.request

from ai.prompts import (
    REPAIR_KEYS_BOTH,
    REPAIR_KEYS_TESTBENCH_ONLY,
    REPAIR_PROMPT,
    REPAIR_SCOPE_BOTH,
    REPAIR_SCOPE_TESTBENCH_ONLY,
    TESTBENCH_GENERATION_PROMPT,
    VERILOG_PROMPT,
    fill_prompt,
)
import config
from services.simulation_service import blank_comments, compile_only, simulate


OLLAMA_URL = config.OLLAMA_URL
OLLAMA_MODEL = config.OLLAMA_MODEL

# Ollama's default context window (2048-4096 tokens) is too small for the
# prompt plus a full design and testbench, which silently truncates input.
OLLAMA_CONTEXT = 8192

# How many times the AI may repair code that does not compile or run.
MAX_REPAIR_ATTEMPTS = 3

FENCED_BLOCK = re.compile(r"```[^\n]*\n([\s\S]*?)```")
CODE_START = re.compile(r"^\s*(module\b|`|//|/\*|\(\*)")
MODULE_NAME = re.compile(r"\bmodule\s+([A-Za-z_][\w$]*)")


class AIProviderError(RuntimeError):
    """
    The AI provider itself failed: unreachable, key rejected, rate limited,
    unknown model or timed out. Retrying with another prompt cannot help, so
    the pipeline reports these directly instead of trying fallbacks.
    """


def clean_verilog(code: str) -> str:
    """
    Remove Markdown code fences and leading prose from generated Verilog.
    Code that already starts with Verilog is returned unchanged.
    """

    code = code.strip()

    fenced = FENCED_BLOCK.search(code)
    if fenced:
        code = fenced.group(1).strip()
    elif code.startswith("```"):
        # Unterminated fence: drop the opening line only.
        code = "\n".join(code.splitlines()[1:]).strip()

    lines = code.splitlines()
    while lines and lines[0].strip() and not CODE_START.match(lines[0]):
        # e.g. "Here is the corrected testbench:"
        if not any(MODULE_NAME.search(line) for line in lines[1:]):
            break
        lines = lines[1:]

    return "\n".join(lines).strip()


def _ollama_request(
    system_prompt: str,
    user_prompt: str,
    options: dict,
    timeout: int,
    required_fields: list[str],
    task: str,
) -> dict:
    """
    Send a chat request to Ollama and return the parsed JSON object.

    Every failure (connection, HTTP, timeout, malformed or incomplete JSON)
    is raised as RuntimeError so the API returns a readable 502 instead of
    an unhandled 500.
    """

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "format": "json",
        "stream": False,
        "options": options,
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")

    except urllib.error.HTTPError as exc:
        error_details = exc.read().decode("utf-8", errors="replace")
        raise AIProviderError(
            f"Ollama HTTP error {exc.code}: {error_details}"
        ) from exc

    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise AIProviderError(
                f"{task} timed out. Please try again."
            ) from exc
        raise AIProviderError(
            "Cannot connect to Ollama. Make sure Ollama is running."
        ) from exc

    except TimeoutError as exc:
        raise AIProviderError(f"{task} timed out. Please try again.") from exc

    try:
        response_data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama returned an unreadable response.") from exc

    message = response_data.get("message") if isinstance(response_data, dict) else None
    content = message.get("content", "") if isinstance(message, dict) else ""
    done_reason = response_data.get("done_reason", "unknown") if isinstance(response_data, dict) else "unknown"

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Ollama returned an empty response for {task.lower()}.")

    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        hint = " The response was cut off." if done_reason == "length" else ""
        raise RuntimeError(
            f"Ollama returned malformed JSON for {task.lower()}.{hint}"
        ) from exc

    if not isinstance(result, dict):
        raise RuntimeError(
            f"Ollama response for {task.lower()} must be a JSON object."
        )

    for field in required_fields:
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise RuntimeError(
                f"AI response is missing a valid '{field}' field."
            )

    return result


MODULE_BLOCK = re.compile(
    r"\bmodule\s+([A-Za-z_][\w$]*)[\s\S]*?\bendmodule\b"
)
ERROR_LOCATION = re.compile(r"\b(design|testbench)\.v:(\d+):")
PORT_WIDTH_WARNING = re.compile(
    r"^.*Port \d+ \((?P<port>\w+)\) of module (?P<module>\w+) expects "
    r"(?P<expects>\d+) bit\(s\), given (?P<given>\d+)\..*$",
    re.MULTILINE,
)


DECLARATION = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>reg|wire)\b\s*(?P<range>\[[^\]]*\])?\s*"
    r"(?P<names>[A-Za-z_][\w\s,]*);",
    re.MULTILINE,
)
COMPARISON = re.compile(
    r"\b([A-Za-z_]\w*)\s*(?:!==|===|!=|==)\s*([A-Za-z_]\w*)\b(?!\s*[\[(])"
)


def _declarations(testbench_code: str) -> dict:
    """name -> (kind, range text without spaces, "" for 1 bit)."""

    found = {}
    for match in DECLARATION.finditer(blank_comments(testbench_code)):
        range_text = re.sub(r"\s+", "", match.group("range") or "")
        for name in match.group("names").split(","):
            name = name.strip()
            if name:
                found.setdefault(name, (match.group("kind"), range_text))
    return found


def _redeclare(testbench_code: str, signal: str, range_text: str) -> Optional[str]:
    """
    Give `signal` its own reg/wire declaration with `range_text` ("" for one
    bit), splitting it out of a list declaration if needed. Returns None when
    the declaration cannot be found.
    """

    for match in DECLARATION.finditer(blank_comments(testbench_code)):
        names = [n.strip() for n in match.group("names").split(",")]
        if signal not in names:
            continue

        indent, kind = match.group("indent"), match.group("kind")
        replacement = f"{indent}{kind} {range_text + ' ' if range_text else ''}{signal};"
        others = [n for n in names if n != signal]
        if others:
            old_range = match.group("range")
            replacement = (
                f"{indent}{kind} {old_range + ' ' if old_range else ''}"
                f"{', '.join(others)};\n" + replacement
            )
        return (
            testbench_code[:match.start()] + replacement
            + testbench_code[match.end():]
        )

    return None


def _fix_port_widths(testbench_code: str, warnings: str) -> tuple[str, list[str]]:
    """
    Fix "Port N (x) of module m expects A bit(s), given B" by redeclaring
    the testbench signal connected to .x(...) with A bits. Only handles a
    plain identifier declared with reg/wire; anything else is left to the AI.
    """

    notes = []
    for warning in PORT_WIDTH_WARNING.finditer(warnings):
        port = warning.group("port")
        width = int(warning.group("expects"))

        connection = re.search(
            rf"\.{port}\s*\(\s*([A-Za-z_]\w*)\s*\)", blank_comments(testbench_code)
        )
        if not connection:
            continue
        signal = connection.group(1)

        patched = _redeclare(
            testbench_code, signal, f"[{width - 1}:0]" if width > 1 else ""
        )
        if patched is None:
            continue

        testbench_code = patched
        notes.append(
            f"Redeclared testbench signal '{signal}' as {width} bit(s) to "
            f"match port '{port}' of {warning.group('module')}."
        )

    return testbench_code, notes


def _fix_expected_widths(testbench_code: str) -> tuple[str, list[str]]:
    """
    Make each expected-value reg as wide as the DUT output wire it is
    compared with. A too-narrow expected reg (e.g. `reg expected_count;`
    against `wire [3:0] count`) compiles silently and produces false FAILs.
    """

    declarations = _declarations(testbench_code)
    wanted: dict[str, set] = {}

    for left, right in COMPARISON.findall(blank_comments(testbench_code)):
        for actual, expected in ((left, right), (right, left)):
            actual_decl = declarations.get(actual)
            expected_decl = declarations.get(expected)
            if (
                actual_decl and expected_decl
                and actual_decl[0] == "wire" and expected_decl[0] == "reg"
                and actual_decl[1] != expected_decl[1]
            ):
                wanted.setdefault(expected, set()).add(actual_decl[1])

    notes = []
    for signal, ranges in wanted.items():
        if len(ranges) != 1:
            continue  # compared against outputs of different widths
        range_text = next(iter(ranges))
        patched = _redeclare(testbench_code, signal, range_text)
        if patched is None:
            continue
        testbench_code = patched
        notes.append(
            f"Resized expected-value reg '{signal}' to "
            f"{range_text or '1 bit'} to match the output it is compared with."
        )

    return testbench_code, notes


CLOCK_PORT = re.compile(r"\binput\s+(?:wire\s+)?(?:reg\s+)?(clk|clock)\b")


VERILOG_KEYWORDS = {"input", "output", "inout", "wire", "reg", "signed"}


def _fix_assign_to_reg(module_code: str) -> tuple[str, list[str]]:
    """
    Turn `output reg y` / `reg y;` into wire when y is driven only by a
    continuous assign. Verilog-2005 rejects assign to a reg; the small model
    makes this mistake often and rarely repairs it.
    """

    notes = []
    body = blank_comments(module_code)
    without_assigns = re.sub(r"\bassign\b[^;]*;", " ", body)

    for target in dict.fromkeys(re.findall(r"\bassign\s+([A-Za-z_]\w*)", body)):
        # Also assigned procedurally? Then it must stay a reg.
        if re.search(rf"\b{target}\s*(?:\[[^\]]*\]\s*)?<?=(?!=)", without_assigns):
            continue

        port = re.search(
            rf"\boutput\s+reg\b(?P<rest>\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?){target}\s*"
            rf"(?P<after>[,);])",
            body,
        )
        if port:
            # `output reg a, b` would change b too; only fix when b is
            # not a bare continuation of the same declaration.
            following = body[port.end():]
            continuation = re.match(r"\s*([A-Za-z_]\w*)\s*[,);]", following)
            if port.group("after") == "," and continuation \
                    and continuation.group(1) not in VERILOG_KEYWORDS:
                continue
            start = port.start() + body[port.start():].index("reg")
            module_code = module_code[:start] + "wire" + module_code[start + 3:]
            body = body[:start] + "wire" + body[start + 3:]
            notes.append(f"Changed output '{target}' from reg to wire (driven by assign).")
            continue

        internal = re.search(
            rf"^(?P<indent>[ \t]*)reg\b(?P<rest>\s*(?:\[[^\]]*\]\s*)?){target}\s*;",
            body,
            re.MULTILINE,
        )
        if internal:
            start = internal.start() + len(internal.group("indent"))
            module_code = module_code[:start] + "wire" + module_code[start + 3:]
            body = body[:start] + "wire" + body[start + 3:]
            notes.append(f"Changed signal '{target}' from reg to wire (driven by assign).")

    return module_code, notes


def _undriven_outputs(module_code: str) -> list[str]:
    """
    Output ports that never appear after the port list: no assign, no
    always-block assignment, no submodule connection. Such a design compiles
    but every output is z, so every test "fails" for a meaningless reason.
    """

    body = blank_comments(module_code)
    header_end = re.search(r"\bmodule\s+[\w$]+\s*(?:#\s*\([^;]*?\))?\s*\([^;]*\)\s*;", body)
    if not header_end:
        return []
    header, logic = body[:header_end.end()], body[header_end.end():]

    outputs = []
    for decl in re.finditer(
        r"\boutput\b\s*(?:wire|reg)?\s*(?:signed\s*)?(?:\[[^\]]*\]\s*)?([^;)]*?)(?=\binput\b|\boutput\b|\binout\b|\)\s*;|;)",
        header + logic,
    ):
        for name in decl.group(1).split(","):
            name = name.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                outputs.append(name)

    return [
        name for name in dict.fromkeys(outputs)
        if not re.search(
            rf"(?:\bassign\s+{name}\b|\b{name}\s*(?:\[[^\]]*\]\s*)?<?=(?!=)|\(\s*{name}\s*\)|\{{[^}}]*\b{name}\b)",
            logic,
        )
    ]


def _fix_procedural_outputs(module_code: str) -> tuple[str, list[str]]:
    """
    Declare an output as `output reg` when it is assigned inside an always
    block but declared as a plain output / output wire (the reverse of
    _fix_assign_to_reg). Only single-name declarations are rewritten.
    """

    notes = []
    body = blank_comments(module_code)
    procedural = re.sub(r"\bassign\b[^;]*;", " ", body)
    # Names assigned through a concatenation: {cout, sum} = ...
    concat_targets = {
        name
        for group in re.findall(r"\{([^{}]*)\}\s*<?=(?!=)", procedural)
        for name in re.findall(r"[A-Za-z_]\w*", group)
    }
    for decl in list(re.finditer(
        r"\boutput\s+(?P<wire>wire\s+)?(?P<range>(?:signed\s*)?\[[^\]]*\]\s*)?"
        r"(?P<name>[A-Za-z_]\w*)\s*(?=[,);])",
        body,
    ))[::-1]:
        name = decl.group("name")
        if name in ("reg", "wire", "signed"):
            continue
        # Assigned with = or <= somewhere outside continuous assigns?
        if name not in concat_targets and not re.search(
            rf"\b{name}\s*(?:\[[^\]]*\]\s*)?<?=(?!=)", procedural
        ):
            continue
        if re.search(rf"\bassign\s+{name}\b", body):
            continue
        rng = decl.group("range") or ""
        replacement = f"output reg {rng}{name}"
        module_code = module_code[:decl.start()] + replacement + module_code[decl.end():]
        notes.append(f"Declared output '{name}' as reg (assigned in an always block).")
    return module_code, notes[::-1]


def _balance_endmodule(code: str, label: str) -> tuple[str, list[str]]:
    """Append endmodule when a file has more `module` than `endmodule`."""

    body = blank_comments(code)
    missing = len(re.findall(r"\bmodule\b", body)) - len(re.findall(r"\bendmodule\b", body))
    if missing <= 0:
        return code, []
    return (
        code.rstrip() + "\nendmodule\n" * missing,
        [f"Added missing endmodule to the {label}."],
    )


def _ensure_clock(module_code: str, testbench_code: str) -> tuple[str, list[str]]:
    """
    Add `always #5 clk = ~clk;` when the design has a clock input, the
    testbench waits on clock edges, but nothing ever toggles the clock.
    Without it the simulation stalls silently at the first @(posedge clk).
    """

    match = CLOCK_PORT.search(blank_comments(module_code))
    if not match:
        return testbench_code, []

    clock = match.group(1)
    body = blank_comments(testbench_code)
    waits_on_clock = re.search(rf"@\s*\(\s*(?:pos|neg)edge\s+{clock}\b", body)
    toggles_clock = re.search(
        rf"\b{clock}\s*=\s*(?:~|!)\s*{clock}\b|\bforever\b", body
    )
    initial = re.search(r"^[ \t]*initial\b", body, re.MULTILINE)

    if not waits_on_clock or toggles_clock or not initial:
        return testbench_code, []

    patched = (
        testbench_code[:initial.start()]
        + f"    always #5 {clock} = ~{clock};\n\n"
        + testbench_code[initial.start():]
    )
    return patched, [f"Added missing clock generator: always #5 {clock} = ~{clock};"]


# Models that accept server-side refusal fallbacks (fallbacks="default").
_FALLBACK_MODELS = {
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
    "claude-sonnet-5-5",
}
_anthropic_client = None


def _anthropic_request(
    system_prompt: str,
    user_prompt: str,
    fields: list[str],
    task: str,
) -> dict:
    """
    Ask Claude for a JSON object with exactly `fields` (all strings), using
    structured outputs so the response is always valid JSON. Every failure
    is raised as RuntimeError so the API returns a readable 502.
    """

    global _anthropic_client
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The anthropic package is not installed. Run: pip install anthropic"
        ) from exc

    if _anthropic_client is None:
        # Credentials come from ANTHROPIC_API_KEY (or an `ant auth login`
        # profile); never hard-code a key here.
        _anthropic_client = anthropic.Anthropic()

    schema = {
        "type": "object",
        "properties": {field: {"type": "string"} for field in fields},
        "required": list(fields),
        "additionalProperties": False,
    }
    request = dict(
        model=config.ANTHROPIC_MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        output_config={
            "effort": config.ANTHROPIC_EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
    )

    try:
        if config.ANTHROPIC_MODEL in _FALLBACK_MODELS:
            # On a policy decline the API retries on a suitable model.
            response = _anthropic_client.beta.messages.create(
                **request,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        else:
            response = _anthropic_client.messages.create(**request)
    except anthropic.AuthenticationError as exc:
        raise AIProviderError(
            "Claude API credentials are missing or invalid. Set "
            "ANTHROPIC_API_KEY in backend/.env and restart the backend."
        ) from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIProviderError("This Claude API key cannot use the configured model.") from exc
    except anthropic.NotFoundError as exc:
        raise AIProviderError(
            f"Claude model {config.ANTHROPIC_MODEL!r} was not found. "
            "Check LOGICFORGE_ANTHROPIC_MODEL."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise AIProviderError("The Claude API rate limit was reached. Try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise AIProviderError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIProviderError("Cannot reach the Claude API. Check your internet connection.") from exc

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the {task.lower()} request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"The Claude response for {task.lower()} was cut off.")

    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Claude returned malformed JSON for {task.lower()}.") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Claude response for {task.lower()} must be a JSON object.")
    return result


class AIDisabledError(AIProviderError):
    """AI features are switched off on this server (no provider / no key)."""


# Free tiers limit requests per minute: wait this long at most for a slot.
RATE_LIMIT_MAX_WAIT = 30
RATE_LIMIT_RETRIES = 2


def _extract_json_object(text: str) -> Optional[dict]:
    """Parse a JSON object, tolerating Markdown fences or text around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                value = json.loads(text[start:end + 1])
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _openai_compatible_request(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    task: str,
) -> dict:
    """
    Chat-completions request to an OpenAI-compatible API (Groq, Gemini,
    OpenRouter, ...). Asks for JSON mode; retries without it if the provider
    rejects that option, and waits briefly on free-tier rate limits.
    """

    provider = config.AI_PROVIDER_NAME or "AI provider"
    url = f"{config.AI_BASE_URL}/chat/completions"
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.AI_API_KEY}",
        # Some providers (OpenRouter) use these to identify the app.
        "X-Title": "LogicForge",
    }

    attempts_left = RATE_LIMIT_RETRIES
    while True:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 400 and "response_format" in payload and (
                "response_format" in detail or "json" in detail.lower()
            ):
                payload.pop("response_format")  # provider/model lacks JSON mode
                continue
            if exc.code == 429 and attempts_left > 0:
                retry_after = exc.headers.get("retry-after", "")
                wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 10.0
                if wait <= RATE_LIMIT_MAX_WAIT:
                    attempts_left -= 1
                    time.sleep(wait)
                    continue
            if exc.code == 429:
                raise AIProviderError(
                    f"The {provider} free-tier rate limit was reached. Wait a minute and try again."
                ) from exc
            if exc.code in (401, 403):
                raise AIProviderError(
                    f"The {provider} API key was rejected. Check LOGICFORGE_AI_API_KEY."
                ) from exc
            if exc.code == 404:
                raise AIProviderError(
                    f"{provider} does not offer the model {config.AI_MODEL!r}. Set "
                    "LOGICFORGE_AI_MODEL to a model from the provider's model list."
                ) from exc
            raise AIProviderError(f"{provider} API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise AIProviderError(f"{task} timed out. Please try again.") from exc
            raise AIProviderError(f"Cannot reach the {provider} API.") from exc
        except TimeoutError as exc:
            raise AIProviderError(f"{task} timed out. Please try again.") from exc

    try:
        content = body["choices"][0]["message"]["content"] or ""
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{provider} returned an unexpected response.") from exc

    result = _extract_json_object(content)
    if result is None:
        hint = " The response was cut off." if finish == "length" else ""
        raise RuntimeError(f"{provider} returned malformed JSON for {task.lower()}.{hint}")
    return result


def _call_model(
    system_prompt: str,
    user_prompt: str,
    options: dict,
    timeout: int,
    required_fields: list[str],
    task: str,
    optional_fields: Optional[list[str]] = None,
) -> dict:
    """
    Send one JSON-returning request to the configured model provider.

    required_fields must be non-empty strings; optional_fields may be empty
    or missing (they are returned as "").
    """

    optional_fields = optional_fields or []

    reason = config.ai_disabled_reason()
    if reason:
        raise AIDisabledError(reason)

    if config.AI_PROVIDER in config.OPENAI_COMPATIBLE_PRESETS:
        result = _openai_compatible_request(
            system_prompt,
            user_prompt,
            temperature=float(options.get("temperature", 0.2)),
            max_tokens=int(options.get("num_predict") or 4096),
            timeout=max(timeout, 120),
            task=task,
        )
        for field in required_fields:
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise RuntimeError(f"AI response is missing a valid '{field}' field.")
    elif config.AI_PROVIDER == "anthropic":
        # Sampling options (temperature, num_ctx, ...) are Ollama-only.
        result = _anthropic_request(
            system_prompt, user_prompt, required_fields + optional_fields, task
        )
        for field in required_fields:
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise RuntimeError(f"AI response is missing a valid '{field}' field.")
    else:
        result = _ollama_request(
            system_prompt, user_prompt, options, timeout, required_fields, task
        )

    for field in optional_fields:
        value = result.get(field)
        result[field] = value if isinstance(value, str) else ""
    return result


def _design_module_names(module_code: str) -> list[str]:
    return MODULE_NAME.findall(blank_comments(module_code))


def _looks_like_testbench(name: str, body: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("tb")
        or lowered.endswith("_tb")
        or "testbench" in lowered
        or bool(re.search(r"\$display|\$finish|\binitial\b", body))
    )


def _module_blocks(code: str) -> list[tuple[str, int, int, str]]:
    """(name, start, end, body) for each module, ignoring comments."""

    stripped = blank_comments(code)
    return [
        (m.group(1), m.start(), m.end(), stripped[m.start():m.end()])
        for m in MODULE_BLOCK.finditer(stripped)
    ]


def _remove_blocks(code: str, blocks: list[tuple[str, int, int, str]]) -> str:
    for _, start, end, _ in sorted(blocks, key=lambda b: b[1], reverse=True):
        code = code[:start] + code[end:]
    return re.sub(r"\n{3,}", "\n\n", code).strip()


def _separate_modules(
    module_code: str,
    testbench_code: str,
    module_locked: bool,
) -> tuple[str, str, list[str]]:
    """
    Deterministically undo a common AI mistake: a module defined in both
    files (testbench pasted into design.v, or design pasted into the
    testbench). Each duplicated module is kept only in the file it belongs
    to. A user-supplied (locked) design is never modified.
    """

    notes = []
    design_blocks = _module_blocks(module_code)
    tb_blocks = _module_blocks(testbench_code)
    design_names = {b[0] for b in design_blocks}
    tb_names = {b[0] for b in tb_blocks}
    duplicated = design_names & tb_names

    if not duplicated:
        return module_code, testbench_code, notes

    drop_from_design = [
        b for b in design_blocks
        if b[0] in duplicated and _looks_like_testbench(b[0], b[3])
    ]
    drop_from_tb = [
        b for b in tb_blocks
        if b[0] in duplicated and not _looks_like_testbench(b[0], b[3])
    ]

    # Never leave design.v empty, and never touch a user's design.
    if (
        not module_locked
        and drop_from_design
        and len(drop_from_design) < len(design_blocks)
    ):
        module_code = _remove_blocks(module_code, drop_from_design)
        notes.append(
            "Removed testbench module(s) "
            + ", ".join(b[0] for b in drop_from_design)
            + " from the design file."
        )

    if drop_from_tb and len(drop_from_tb) < len(tb_blocks):
        testbench_code = _remove_blocks(testbench_code, drop_from_tb)
        notes.append(
            "Removed duplicate design module(s) "
            + ", ".join(b[0] for b in drop_from_tb)
            + " from the testbench."
        )

    return module_code, testbench_code, notes


def _annotate_errors(errors: str, module_code: str, testbench_code: str) -> str:
    """
    Append numbered excerpts of the lines iverilog complained about. A small
    model cannot count lines, so "testbench.v:34" alone is not actionable.
    """

    files = {
        "design": module_code.splitlines(),
        "testbench": testbench_code.splitlines(),
    }
    locations = []
    for match in ERROR_LOCATION.finditer(errors):
        key = (match.group(1), int(match.group(2)))
        if key not in locations:
            locations.append(key)

    excerpts = []
    for file_key, line_no in locations[:6]:
        lines = files[file_key]
        if not 1 <= line_no <= len(lines):
            continue
        excerpt = [f"{file_key}.v around line {line_no}:"]
        for number in range(max(1, line_no - 2), min(len(lines), line_no + 2) + 1):
            marker = ">>" if number == line_no else "  "
            excerpt.append(f"{marker} {number:4} | {lines[number - 1]}")
        excerpts.append("\n".join(excerpt))

    if not excerpts:
        return errors

    return errors.rstrip() + "\n\nOFFENDING LINES (>> marks the error line):\n" \
        + "\n\n".join(excerpts)


def _find_problems(
    module_code: str,
    testbench_code: str,
    module_locked: bool,
) -> dict:
    """
    Compile and dry-run a design + testbench.

    Returns {"problems", "fatal", "compiled", "testbench_code", "simulation"}:
    - problems: text to send to the AI for repair, or "" when clean.
    - fatal: the problem cannot be fixed by repairing the testbench/design
      (no compiler, or a user-supplied design that does not compile).
    - testbench_code: the testbench after deterministic counter repair.
    - simulation: the simulate() result when the code ran.

    Failing test cases are NOT problems: they are genuine results and are
    reported, never "repaired".
    """

    report = {"problems": "", "fatal": False, "compiled": False,
              "testbench_code": testbench_code, "simulation": None}

    if module_locked:
        design_check = compile_only(module_code)
        if not design_check["success"]:
            report["fatal"] = True
            report["problems"] = (
                "The design module itself does not compile, so no testbench "
                "can run against it. Fix the design first:\n"
                + (design_check["errors"] or "")
            )
            return report
    else:
        design_blocks = _module_blocks(module_code)
        if design_blocks and all(
            _looks_like_testbench(name, body) for name, _, _, body in design_blocks
        ):
            report["problems"] = (
                "module_code contains a testbench ("
                + ", ".join(b[0] for b in design_blocks)
                + ") instead of the design module. module_code must contain "
                "ONLY the synthesizable design: module <name>(ports); "
                "assign/always logic; endmodule. No initial, $display or "
                "$finish in module_code."
            )
            return report

    # AI-written code must be plain Verilog-2005; a user's own design may
    # legitimately use SystemVerilog, so it keeps -g2012.
    for label, code in (("module_code", module_code), ("testbench_code", testbench_code)):
        if not is_verilog_source(code):
            report["problems"] = (
                f"{label} does not contain Verilog (no module ... endmodule). "
                f"It starts with: {code.strip()[:120]!r}. Return the complete "
                "Verilog source code, never an explanation sentence."
            )
            return report

    if not module_locked:
        undriven = _undriven_outputs(module_code)
        if undriven:
            report["problems"] = (
                "The design module never drives output(s) "
                + ", ".join(undriven)
                + ". module_code must contain the actual logic, e.g. "
                "`assign y = ...;` or an always block that assigns each output."
            )
            return report

    compiled = compile_only(module_code, testbench_code, strict=not module_locked)
    report["testbench_code"] = compiled["testbench_code"]
    testbench_code = compiled["testbench_code"]

    if compiled.get("tool_missing"):
        report["fatal"] = True
        report["problems"] = compiled["errors"]
        return report

    # Mismatched port widths only warn, but silently truncate or pad
    # signals and produce bogus results. Fix them in code first, whether or
    # not the compile also had real errors.
    compiler_text = (compiled.get("errors") or "") + (compiled.get("warnings") or "")
    if PORT_WIDTH_WARNING.search(compiler_text):
        fixed_tb, notes = _fix_port_widths(testbench_code, compiler_text)
        if notes:
            compiled = compile_only(module_code, fixed_tb, strict=not module_locked)
            testbench_code = compiled["testbench_code"]
            report["testbench_code"] = testbench_code
            report["notes"] = notes

    if not compiled["success"]:
        report["problems"] = "Compilation errors:\n" + _annotate_errors(
            compiled["errors"], module_code, testbench_code
        )
        return report

    report["compiled"] = True
    warnings = compiled.get("warnings") or ""

    if PORT_WIDTH_WARNING.search(warnings):
        details = "\n".join(
            f"- design port '{w.group('port')}' is {w.group('expects')} bit(s), "
            f"but the testbench connects a {w.group('given')}-bit signal."
            for w in PORT_WIDTH_WARNING.finditer(warnings)
        )
        report["problems"] = (
            "Port width mismatch between the testbench and the design. "
            "Declare each testbench reg/wire with exactly the same width as "
            "the design port it connects to:\n" + details + "\n\n"
            + _annotate_errors(
                "\n".join(m.group(0) for m in PORT_WIDTH_WARNING.finditer(warnings)),
                module_code, testbench_code,
            )
        )
        return report

    # "full_adder dut (" or "full_adder #(...) dut (" somewhere in the tb.
    design_names = _design_module_names(module_code)
    testbench_body = blank_comments(testbench_code)
    if design_names and not any(
        re.search(
            rf"\b{re.escape(name)}\s*(?:#\s*\([^;]*?\))?\s*[A-Za-z_][\w$]*\s*\(",
            testbench_body,
        )
        for name in design_names
    ):
        report["problems"] = (
            "The testbench never instantiates the design module "
            f"'{design_names[0]}'. Instantiate it as: "
            f"{design_names[0]} dut (.port(signal), ...);"
        )
        return report

    result = simulate(module_code=module_code, testbench_code=testbench_code)
    report["simulation"] = result
    verdict = result.get("verdict")

    if verdict in ("PASS", "FAIL"):
        return report

    if verdict == "NO_TESTS":
        report["problems"] = (
            "The testbench ran but executed zero test cases "
            "(Total tests: 0). Increment total_tests for every test."
        )
    elif verdict == "UNVERIFIED" and "$finish called" not in (result.get("output") or ""):
        report["problems"] = (
            "The simulation stopped before reaching $finish and printed no "
            "summary. Usually the testbench waits forever, e.g. on "
            "@(posedge clk) without `always #5 clk = ~clk;`, or a wait() "
            "whose condition never becomes true.\nOutput was:\n"
            + (result.get("output") or "(no output)")[-1500:]
        )
    elif verdict == "UNVERIFIED":
        report["problems"] = (
            "The testbench ran but did not print the required summary. It "
            "must print \"Total tests: %0d, Failures: %0d\" and then "
            "OVERALL PASS or OVERALL FAIL before $finish.\nOutput was:\n"
            + (result.get("output") or "(no output)")[-1500:]
        )
    else:
        report["problems"] = (
            "Simulation did not finish correctly:\n"
            + (result.get("error") or "unknown error")
        )

    return report


def _ai_repair(
    module_code: str,
    testbench_code: str,
    problems: str,
    module_locked: bool,
) -> dict:
    """Ask the AI to fix the reported problems. Returns the corrected code."""

    prompt = fill_prompt(
        REPAIR_PROMPT,
        errors=problems[-4000:],
        scope_instructions=(
            REPAIR_SCOPE_TESTBENCH_ONLY if module_locked else REPAIR_SCOPE_BOTH
        ),
        response_keys=(
            REPAIR_KEYS_TESTBENCH_ONLY if module_locked else REPAIR_KEYS_BOTH
        ),
        module_code=module_code,
        testbench_code=testbench_code,
    )

    required = ["testbench_code"] if module_locked else [
        "module_code", "testbench_code"
    ]

    result = _call_model(
        system_prompt=(
            "You are a precise Verilog repair assistant. Return only valid "
            "JSON. Fix compile errors without removing tests."
        ),
        user_prompt=prompt,
        options={"temperature": 0.1, "num_ctx": OLLAMA_CONTEXT},
        timeout=240,
        required_fields=required,
        task="Verilog repair",
    )

    return {
        "module_code": (
            module_code if module_locked
            else clean_verilog(result["module_code"])
        ),
        "testbench_code": clean_verilog(result["testbench_code"]),
        "changes_made": str(result.get("changes_made", "")).strip(),
    }


def _error_signature(problems: str) -> str:
    """Problems text without line numbers, to detect a repair that stalled."""
    return re.sub(r"\d+", "#", problems.split("OFFENDING LINES")[0]).strip()


def verify_and_repair(
    module_code: str,
    testbench_code: str,
    module_locked: bool,
    regenerate: Optional[Callable[[], dict]] = None,
) -> dict:
    """
    Compile with Icarus, dry-run the testbench, and fix compile/structure
    problems up to MAX_REPAIR_ATTEMPTS times. Each attempt is an AI repair,
    or, when the previous repair made no progress and `regenerate` is
    given, a fresh generation.

    Returns the final code plus a "verification" report for the frontend.
    The returned code is always the latest version, even if it still
    has problems, so the user can see and edit it.
    """

    log = []
    attempts = 0
    check = None
    previous_signature = None
    stalled = False

    while True:
        module_code, testbench_code, notes = _separate_modules(
            module_code, testbench_code, module_locked
        )
        log.extend(notes)
        if not module_locked:
            module_code, notes = _balance_endmodule(module_code, "design")
            log.extend(notes)
            module_code, notes = _fix_assign_to_reg(module_code)
            log.extend(notes)
        testbench_code, notes = _balance_endmodule(testbench_code, "testbench")
        log.extend(notes)
        testbench_code, notes = _ensure_clock(module_code, testbench_code)
        log.extend(notes)
        testbench_code, notes = _fix_expected_widths(testbench_code)
        log.extend(notes)

        check = _find_problems(module_code, testbench_code, module_locked)
        testbench_code = check["testbench_code"]
        log.extend(check.get("notes", []))

        if not check["problems"] or check["fatal"]:
            break

        signature = _error_signature(check["problems"])
        stalled = signature == previous_signature
        previous_signature = signature

        log.append(
            ("Initial check" if attempts == 0 else f"After attempt {attempts}")
            + ": " + check["problems"].strip()[:1500]
        )

        if attempts >= MAX_REPAIR_ATTEMPTS:
            break

        attempts += 1
        try:
            if stalled and regenerate is not None:
                fresh = regenerate()
                module_code = fresh.get("module_code", module_code)
                testbench_code = fresh["testbench_code"]
                log.append(
                    f"Attempt {attempts}: previous repair made no progress, "
                    "regenerated the code from scratch."
                )
                previous_signature = None
                continue

            fixed = _ai_repair(
                module_code, testbench_code, check["problems"], module_locked
            )
        except RuntimeError as exc:
            if isinstance(exc, AIProviderError):
                raise
            log.append(f"Attempt {attempts} failed: {exc}")
            continue

        module_code = fixed["module_code"]
        testbench_code = fixed["testbench_code"]
        log.append(
            f"Attempt {attempts} (AI repair): "
            + (fixed["changes_made"] or "code updated.")
        )

    simulation = check["simulation"] or {}

    verification = {
        "compiled": check["compiled"],
        "ready": not check["problems"],
        "verdict": simulation.get("verdict") if not check["problems"] else None,
        "total_tests": simulation.get("total_tests"),
        "failures": simulation.get("failures"),
        "repair_attempts": attempts,
        "problems": check["problems"] or None,
        "log": log,
    }

    return {
        "module_code": module_code,
        "testbench_code": testbench_code,
        "verification": verification,
    }


def _with_retry(request: Callable[[float], dict]) -> dict:
    """
    Run a generation request; if the model returns malformed or incomplete
    JSON, try once more at a higher temperature. Connection failures and
    timeouts are not retried.
    """

    try:
        return request(0.2)
    except RuntimeError as exc:
        if isinstance(exc, AIProviderError):
            raise
        message = str(exc)
        if "malformed JSON" not in message and "missing a valid" not in message \
                and "empty response" not in message:
            raise
        return request(0.5)


def _generate_design_and_testbench(user_input: str, temperature: float) -> dict:
    result = _call_model(
        system_prompt=(
            "You are an expert digital hardware engineer. "
            "Return only valid JSON matching the requested structure. "
            "Do not include Markdown code fences."
        ),
        user_prompt=fill_prompt(VERILOG_PROMPT, input=user_input),
        options={"temperature": temperature, "num_ctx": OLLAMA_CONTEXT},
        timeout=180,
        required_fields=["module_code", "testbench_code", "explanation"],
        task="Verilog generation",
    )
    result["module_code"] = clean_verilog(result["module_code"])
    result["testbench_code"] = clean_verilog(result["testbench_code"])
    return result


def generate_verilog(user_input: str) -> dict:
    """
    Generate a Verilog module and testbench using local Ollama, then compile,
    dry-run and (if needed) repair them before returning.
    """

    if not user_input or not user_input.strip():
        raise ValueError("Circuit description cannot be empty.")

    user_input = user_input.strip()
    result = _with_retry(
        lambda temperature: _generate_design_and_testbench(user_input, temperature)
    )
    latest = {"explanation": result["explanation"]}

    def regenerate() -> dict:
        fresh = _generate_design_and_testbench(user_input, temperature=0.5)
        latest["explanation"] = fresh["explanation"]
        return fresh

    checked = verify_and_repair(
        result["module_code"],
        result["testbench_code"],
        module_locked=False,
        regenerate=regenerate,
    )

    return {**result, **checked, "explanation": latest["explanation"]}


def debug_verilog(
    module_code: str,
    testbench_code: str,
    error: str = "",
    output: str = ""
) -> dict:
    """Analyze failed Verilog simulation and suggest corrected code."""

    if not module_code.strip() or not testbench_code.strip():
        raise ValueError("Both module code and testbench code are required.")

    # First apply the deterministic testbench fixes (port/expected-value
    # widths, missing clock, missing endmodule, ...). They are exact, fast
    # and need no AI. The design is locked, so it is never modified here.
    pre = verify_and_repair(module_code, testbench_code, module_locked=True)
    pre_verification = pre["verification"]
    if (
        pre_verification["ready"]
        and pre_verification["verdict"] == "PASS"
        and _normalized(pre["testbench_code"]) != _normalized(testbench_code)
    ):
        fixes = [
            line for line in pre_verification["log"]
            if not line.startswith(("Initial check", "After attempt"))
        ]
        return {
            "root_cause": "The testbench, not the design, was wrong: "
                          + " ".join(fixes),
            "explanation": (
                "LogicForge found and corrected testbench errors automatically "
                "and re-ran the simulation: all "
                f"{pre_verification['total_tests']} tests now pass with the "
                "design unchanged."
            ),
            "fixed_module_code": module_code,
            "fixed_testbench_code": pre["testbench_code"],
            "changes_made": "\n".join(fixes) or "Testbench corrected.",
            "module_changed": False,
            "testbench_changed": True,
            "notes": [],
            "verification": pre_verification,
        }

    debug_prompt = f"""
You are an expert Verilog RTL engineer and hardware verification engineer.

Analyze the following Verilog design and testbench using the simulation
diagnostics provided.

SIMULATION ERROR:
{error or "No compiler error reported."}

SIMULATION OUTPUT:
{output or "No simulation output available."}

DESIGN MODULE:
{module_code}

TESTBENCH:
{testbench_code}

YOUR TASK:
1. Identify the actual root cause of the error or failed test.
2. Explain the issue in simple technical language.
3. Correct the Verilog module and/or testbench.
4. Preserve the original design functionality and module interface.
5. Do not introduce unnecessary clock, reset, or sequential logic.
6. Do not remove failing test cases merely to make the simulation pass.
7. Do not claim the fix is verified. It must be simulated again.
8. If a file is already correct, explain why in "explanation" and put that
   file's COMPLETE ORIGINAL CODE in its fixed_*_code field.
9. fixed_module_code and fixed_testbench_code must contain ONLY Verilog
   source (module ... endmodule). Never put sentences such as "The code is
   already correct" or "The corrected code is as follows:" in those fields.

Return ONLY valid JSON with exactly these string fields:
{{
    "root_cause": "The identified problem",
    "explanation": "Detailed explanation of the problem and solution",
    "fixed_module_code": "Complete corrected Verilog module",
    "fixed_testbench_code": "Complete corrected Verilog testbench",
    "changes_made": "Summary of modifications"
}}
"""

    result = _call_model(
        system_prompt=(
            "You are a precise Verilog debugging assistant. "
            "Return only valid JSON. Preserve the design interface "
            "and never remove tests to hide failures."
        ),
        user_prompt=debug_prompt,
        options={"temperature": 0.1, "num_ctx": OLLAMA_CONTEXT},
        timeout=240,
        required_fields=[
            "root_cause",
            "explanation",
            "fixed_module_code",
            "fixed_testbench_code",
            "changes_made",
        ],
        task="AI debugging",
    )

    notes = []
    fixed = {}
    for field, original, label in (
        ("fixed_module_code", module_code, "design"),
        ("fixed_testbench_code", testbench_code, "testbench"),
    ):
        candidate = clean_verilog(result[field])
        if is_verilog_source(candidate):
            fixed[field] = candidate
        else:
            # The model wrote prose (e.g. "already correct") instead of code.
            # Never let that reach the editor: keep the user's original.
            fixed[field] = original
            notes.append(
                f"The AI returned no Verilog for the {label}, so the "
                f"original {label} was kept unchanged."
            )

    module_changed = _normalized(fixed["fixed_module_code"]) != _normalized(module_code)
    testbench_changed = (
        _normalized(fixed["fixed_testbench_code"]) != _normalized(testbench_code)
    )

    if module_changed or testbench_changed:
        # Compile, dry-run and auto-repair the proposed fix like generated
        # code. An unchanged design stays locked so it is never rewritten.
        checked = verify_and_repair(
            fixed["fixed_module_code"],
            fixed["fixed_testbench_code"],
            module_locked=not module_changed,
        )
        fixed["fixed_module_code"] = checked["module_code"]
        fixed["fixed_testbench_code"] = checked["testbench_code"]
        verification = checked["verification"]
        verification["log"] = notes + verification["log"]
    else:
        verification = None

    return {
        **result,
        **fixed,
        "module_changed": _normalized(fixed["fixed_module_code"]) != _normalized(module_code),
        "testbench_changed": (
            _normalized(fixed["fixed_testbench_code"]) != _normalized(testbench_code)
        ),
        "notes": notes,
        "verification": verification,
    }


def is_verilog_source(code: str) -> bool:
    """True when the text contains at least one complete module."""

    body = blank_comments(code or "")
    return bool(
        re.search(r"\bmodule\s+[A-Za-z_][\w$]*", body)
        and re.search(r"\bendmodule\b", body)
    )


def _normalized(code: str) -> str:
    return re.sub(r"\s+", " ", (code or "").strip())


def generate_testbench(module_code: str) -> dict:
    """Generate a self-checking Verilog testbench using local Ollama."""

    if not module_code or not module_code.strip():
        raise ValueError("Verilog module code cannot be empty.")

    module_code = module_code.strip()

    # Do not spend an AI call if the user's design cannot compile.
    design_check = compile_only(module_code)
    if not design_check["success"]:
        raise ValueError(
            "The design module does not compile, so a testbench cannot be "
            "generated for it yet. Fix these errors first:\n"
            + (design_check["errors"] or "")
        )

    prompt = fill_prompt(TESTBENCH_GENERATION_PROMPT, module_code=module_code)

    def request_testbench(temperature: float) -> dict:
        result = _call_model(
            system_prompt=(
                "You are an expert hardware verification engineer. "
                "Generate concise, valid Verilog-2001 testbenches. "
                "Keep the explanation to one or two short sentences. "
                "Use exactly one initial block, no assert statements, "
                "and explicit if comparisons with failure counters. "
                "Return a single valid JSON object with exactly these "
                "string fields: testbench_code and explanation. "
                "Do not include Markdown code fences."
            ),
            user_prompt=prompt,
            options={
                "temperature": temperature,
                "num_predict": 3000,
                "num_ctx": OLLAMA_CONTEXT,
            },
            timeout=240,
            required_fields=["testbench_code", "explanation"],
            task="Testbench generation",
        )
        result["testbench_code"] = clean_verilog(result["testbench_code"])
        return result

    result = _with_retry(request_testbench)
    latest = {"explanation": result["explanation"]}

    def regenerate() -> dict:
        fresh = request_testbench(temperature=0.5)
        latest["explanation"] = fresh["explanation"]
        return {"testbench_code": fresh["testbench_code"]}

    checked = verify_and_repair(
        module_code,
        result["testbench_code"],
        module_locked=True,
        regenerate=regenerate,
    )

    return {
        "testbench_code": checked["testbench_code"],
        "explanation": latest["explanation"],
        "verification": checked["verification"],
    }
