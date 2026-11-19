
import json
import urllib.error
import urllib.request

from ai.prompts import VERILOG_PROMPT, TESTBENCH_GENERATION_PROMPT


OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5-coder:3b"


def clean_verilog(code: str) -> str:
    """Remove accidental Markdown code fences from generated Verilog."""

    code = code.strip()

    if code.startswith("```"):
        lines = code.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        code = "\n".join(lines).strip()

    return code


def generate_verilog(user_input: str) -> dict:
    """Generate Verilog module and testbench using local Ollama."""

    if not user_input or not user_input.strip():
        raise ValueError("Circuit description cannot be empty.")

    prompt = VERILOG_PROMPT.format(input=user_input.strip())

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert digital hardware engineer. "
                    "Return only valid JSON matching the requested structure. "
                    "Do not include Markdown code fences."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.2,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            response_data = json.loads(response.read().decode("utf-8"))

        content = response_data.get("message", {}).get("content")

        if not content:
            raise RuntimeError("Ollama returned an empty response.")

        result = json.loads(content)

        required_fields = [
            "module_code",
            "testbench_code",
            "explanation",
        ]

        for field in required_fields:
            if (
                not isinstance(result.get(field), str)
                or not result[field].strip()
            ):
                raise RuntimeError(
                    f"AI response is missing a valid '{field}' field."
                )

        result["module_code"] = clean_verilog(result["module_code"])
        result["testbench_code"] = clean_verilog(result["testbench_code"])

        return result

    except urllib.error.HTTPError as exc:
        error_details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Ollama HTTP error {exc.code}: {error_details}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running."
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Ollama returned an invalid JSON response."
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Ollama generation timed out. Try a simpler circuit prompt."
        ) from exc


def debug_verilog(
    module_code: str,
    testbench_code: str,
    error: str = "",
    output: str = ""
) -> dict:
    """Analyze failed Verilog simulation and suggest corrected code."""

    if not module_code.strip() or not testbench_code.strip():
        raise ValueError("Both module code and testbench code are required.")

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
8. If the original code is already correct, explain why and return it unchanged.

Return ONLY valid JSON with exactly these string fields:
{{
    "root_cause": "The identified problem",
    "explanation": "Detailed explanation of the problem and solution",
    "fixed_module_code": "Complete corrected Verilog module",
    "fixed_testbench_code": "Complete corrected Verilog testbench",
    "changes_made": "Summary of modifications"
}}
"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise Verilog debugging assistant. "
                    "Return only valid JSON. Preserve the design interface "
                    "and never remove tests to hide failures."
                ),
            },
            {
                "role": "user",
                "content": debug_prompt,
            },
        ],
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.1,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            response_data = json.loads(response.read().decode("utf-8"))

        content = response_data.get("message", {}).get("content")

        if not content:
            raise RuntimeError("Ollama returned an empty debugging response.")

        result = json.loads(content)

        required_fields = [
            "root_cause",
            "explanation",
            "fixed_module_code",
            "fixed_testbench_code",
            "changes_made",
        ]

        for field in required_fields:
            if (
                not isinstance(result.get(field), str)
                or not result[field].strip()
            ):
                raise RuntimeError(
                    f"AI debugging response is missing '{field}'."
                )

        result["fixed_module_code"] = clean_verilog(
            result["fixed_module_code"]
        )
        result["fixed_testbench_code"] = clean_verilog(
            result["fixed_testbench_code"]
        )

        return result

    except urllib.error.HTTPError as exc:
        error_details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Ollama HTTP error {exc.code}: {error_details}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running."
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Ollama returned an invalid JSON debugging response."
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "AI debugging timed out. Please try again."
        ) from exc


def generate_testbench(module_code: str) -> dict:
    """Generate a self-checking Verilog testbench using local Ollama."""

    if not module_code or not module_code.strip():
        raise ValueError("Verilog module code cannot be empty.")

    prompt = TESTBENCH_GENERATION_PROMPT.replace(
        "{module_code}",
        module_code.strip()
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert hardware verification engineer. "
                    "Generate concise, valid Verilog-2001 testbenches. "
                    "Keep the explanation to one or two short sentences. "
                    "Keep the testbench compact and avoid unnecessary comments. "
                    "Use exactly one initial block, no assert statements, "
                    "and explicit if comparisons with failure counters. "
                    "Return a single valid JSON object with exactly these "
                    "string fields: testbench_code and explanation. "
                    "Do not include Markdown code fences."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": 1800,
            "num_ctx": 8192,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            response_data = json.loads(
                response.read().decode("utf-8")
            )

        content = response_data.get("message", {}).get("content", "")
        done_reason = response_data.get("done_reason", "unknown")

        # Temporary diagnostics to inspect Ollama's actual response.
        print("\n========== OLLAMA TESTBENCH DIAGNOSTICS ==========")
        print("Model:", OLLAMA_MODEL)
        print("Response length:", len(content))
        print("Done reason:", done_reason)
        print("Raw response:")
        print(repr(content))
        print("==================================================\n")

        if not content.strip():
            raise RuntimeError(
                "Ollama returned an empty testbench response."
            )

        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Ollama returned malformed testbench JSON. "
                f"Parser detail: {exc}. "
                f"Done reason: {done_reason}. "
                f"Response length: {len(content)}."
            ) from exc

        if not isinstance(result, dict):
            raise RuntimeError(
                "Ollama testbench response must be a JSON object."
            )

        required_fields = ["testbench_code", "explanation"]

        for field in required_fields:
            if (
                not isinstance(result.get(field), str)
                or not result[field].strip()
            ):
                raise RuntimeError(
                    f"AI response is missing a valid '{field}' field."
                )

        result["testbench_code"] = clean_verilog(
            result["testbench_code"]
        )

        return result

    except urllib.error.HTTPError as exc:
        error_details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Ollama HTTP error {exc.code}: {error_details}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cannot connect to Ollama. Make sure Ollama is running."
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError(
            "Testbench generation timed out. Please try again."
        ) from exc
