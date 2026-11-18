VERILOG_PROMPT = """
You are an expert hardware engineer.

Convert the following into correct Verilog code with a testbench.

Requirements:
- Must compile with Icarus Verilog
- Include $dumpfile and $dumpvars

Prompt:
{input}
"""