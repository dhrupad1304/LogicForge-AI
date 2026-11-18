from openai import OpenAI
from ai.prompts import VERILOG_PROMPT
from config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)

def generate_verilog(user_input: str):
    prompt = VERILOG_PROMPT.format(input=user_input)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content